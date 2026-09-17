"""Demo entrypoint: drive guest Amazon checkout for pending/mapped Sheet orders.

Entry point:
    python -m jobs.demo_guest_checkout_fulfillment

Workflow per order:
  1. Fetch rows from the Orders tab with Status = "Pending" or "Mapped" and a
     resolved Amazon ASIN/SKU (column M).
  2. Run the guest-checkout Playwright flow (app.services.fulfillment.guest_checkout)
     for each order -- up to but NOT including "Place your order".
  3. Update the Sheet per order:
       - Success reaching checkout: Status = "Fulfilling", Notes = total price +
         estimated delivery + "Awaiting manual approval to place order."
       - Failure (sign-in wall, sold out, invalid ASIN, CAPTCHA, etc.):
         Status = "Error", Notes = the specific reason.
     Never set Status = "Fulfilled" -- no real purchase happens.
  4. Save a screenshot per order at backend/screenshots/{asin}/checkout.png.

Batch execution:
    - Orders are processed sequentially.
    - Transient Playwright errors are retried up to MAX_RETRIES_PER_ORDER times.
    - Persistent failures (sign-in wall, sold out, CAPTCHA, invalid ASIN) are
      NOT retried; they are logged and skipped.
    - A failure on one order never blocks the rest.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Path setup (mirrors process_sheet_orders.py)
# ---------------------------------------------------------------------------

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(_BACKEND_DIR), ".env"), override=True)

from app.services.fulfillment.guest_checkout import (
    CHECKOUT_SCREENSHOT_DIR,
    PICKUP_STATUSES,
    STATUS_ERROR,
    STATUS_FULFILLING,
    CheckoutResult,
    GuestCheckoutError,
    SignInWallError,
    SoldOutError,
    InvalidAsinError,
    CaptchaDetectedError,
    PriceMismatchError,
    run_guest_checkout,
)
from mcp_servers.google_sheets.sheets_client import (
    COL_AMAZON_ORDER_ID,
    COL_BUYER_NAME,
    COL_CITY,
    COL_COUNTRY,
    COL_ORDER_ID,
    COL_PHONE,
    COL_QTY,
    COL_SHIPPING_ADDRESS,
    COL_STATE,
    COL_STATUS,
    COL_ZIP,
    sheets_client,
)

logger = logging.getLogger(__name__)

# The spreadsheet ID -- read from SHEET_ID in the repo-root .env. The
# process_sheet_orders job uses the same env var.
SHEET_ID = os.getenv("SHEET_ID")
if not SHEET_ID:
    raise RuntimeError(
        "SHEET_ID environment variable is not set -- cannot run without a "
        "target spreadsheet. Set it in the repo-root .env."
    )

# Sheet statuses this demo picks up: Pending or Mapped (ASIN already resolved).
# Status = "Mapped" rows come from the SKU-mapping step in
# process_sheet_orders.py; Status = "Pending" rows may already have an ASIN
# filled in column M by a prior manual step.
PICKUP_STATUSES_SHEET = ("Pending", "Mapped")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_cell(row: list, idx: int, default: str = "") -> str:
    """Out-of-range-safe cell read for raw sheet rows."""
    if idx < len(row):
        val = row[idx]
        return str(val).strip() if val else default
    return default


def _parse_qty(v: str) -> int:
    try:
        return int(v)
    except (ValueError, TypeError):
        return 1


def _fetch_eligible_orders(sheet_id: str) -> list[dict[str, Any]]:
    """Return rows where Price (O) and Delivery Date (P) are BOTH empty.

    Real schema (A-P):
      A: Order ID, B: Date, C: SKU (Amazon ASIN), D: Product Name, E: Variation,
      F: Qty, G: Recipient, H: Phone, I: Address 1, J: [blank], K: Delivery instructions,
      L: City, M: State, N: Zipcode, O: Price, P: Delivery Date

    Eligible orders = rows where both O (Price) and P (Delivery Date) are empty.
    """
    if not sheets_client.is_configured:
        logger.error("Google Sheets not configured -- GOOGLE_SHEETS_CREDENTIALS_PATH is unset or invalid")
        return []

    logger.info("Fetching orders where Price and Delivery Date are empty from sheet %s...", sheet_id)
    all_rows = sheets_client.read_rows(sheet_id, "Sheet1!A1:P200")
    if not all_rows:
        logger.info("Sheet is empty")
        return []

    # Column indices (0-based)
    COL_ORDER_ID = 0      # A
    COL_DATE = 1          # B
    COL_SKU = 2           # C (Amazon ASIN)
    COL_PRODUCT_NAME = 3  # D
    COL_VARIATION = 4     # E
    COL_QTY = 5           # F
    COL_RECIPIENT = 6     # G
    COL_PHONE = 7         # H
    COL_ADDRESS = 8       # I
    # COL_BLANK = 9       # J (skip)
    COL_DELIVERY_INSTR = 10  # K
    COL_CITY = 11         # L
    COL_STATE = 12        # M
    COL_ZIPCODE = 13      # N
    COL_PRICE = 14        # O
    COL_DELIVERY_DATE = 15  # P

    eligible = []
    for idx, row in enumerate(all_rows[1:], start=2):  # 1-indexed, skip header
        if not row or not row[0].strip():
            continue

        # Check eligibility: Price (O) and Delivery Date (P) must BOTH be empty
        price = _safe_cell(row, COL_PRICE)
        delivery_date = _safe_cell(row, COL_DELIVERY_DATE)

        if price.strip() or delivery_date.strip():
            # Already has price or delivery date - skip it
            continue

        # Has both Price and Delivery Date empty - eligible for processing
        asin = _safe_cell(row, COL_SKU)
        if not asin:
            logger.info("Skipping row %d (order %s): no ASIN in column C", idx, _safe_cell(row, COL_ORDER_ID))
            continue

        eligible.append({
            "row_number": idx,
            "order_id": _safe_cell(row, COL_ORDER_ID),
            "date": _safe_cell(row, COL_DATE),
            "asin": asin,
            "product_name": _safe_cell(row, COL_PRODUCT_NAME),
            "variation": _safe_cell(row, COL_VARIATION),
            "qty": _safe_cell(row, COL_QTY, "1"),
            "buyer_name": _safe_cell(row, COL_RECIPIENT),
            "phone": _safe_cell(row, COL_PHONE),
            "shipping_address": _safe_cell(row, COL_ADDRESS),
            "delivery_instructions": _safe_cell(row, COL_DELIVERY_INSTR),
            "city": _safe_cell(row, COL_CITY),
            "state": _safe_cell(row, COL_STATE),
            "zip": _safe_cell(row, COL_ZIPCODE),
            "country": "US",  # Default to US
            "raw": row,
        })

    logger.info("Found %d eligible order(s) (Price and Delivery Date both empty)", len(eligible))
    return eligible


def _update_sheet(
    sheet_id: str,
    row_number: int,
    status: str,
    notes: str = "",
    amazon_asin_sku: str = "",
) -> None:
    """Write status + notes back to the Sheet row. Best-effort."""
    try:
        sheets_client.update_order_status(
            sheet_id,
            row_number,
            status,
            amazon_asin_sku=amazon_asin_sku,
            notes=notes,
        )
        logger.info("Sheet row %d updated: status=%s", row_number, status)
    except Exception as exc:
        logger.error("Failed to update sheet row %d: %s", row_number, exc)


def _report_result(result: CheckoutResult, order_row: dict[str, Any]) -> dict[str, Any]:
    """Build a report entry for one order."""
    return {
        "order_id": order_row.get("order_id", result.order_id),
        "asin": result.asin,
        "qty": result.quantity,
        "success": result.success,
        "status_written": result.status_to_write,
        "notes": result.notes,
        "total_price": result.total_price,
        "estimated_delivery": result.estimated_delivery,
        "screenshot": result.screenshot_path,
        "detail": result.detail,
    }


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def run_demo(sheet_id: str, max_retries: int = 2) -> dict[str, Any]:
    """Run the guest-checkout demo against all eligible orders in the Sheet."""
    orders = _fetch_eligible_orders(sheet_id)
    if not orders:
        return {"processed": 0, "failed": 0, "results": [], "message": "No eligible orders"}

    results: list[dict[str, Any]] = []
    processed = 0
    failed = 0

    for order in orders:
        order_id = order.get("order_id", "unknown")
        row_number = order.get("row_number")
        asin = (order.get("asin") or "").strip()
        qty = _parse_qty(order.get("qty") or "1")
        buyer_name = order.get("buyer_name", "").strip()
        shipping_address = order.get("shipping_address", "").strip()
        city = order.get("city", "").strip()
        state = order.get("state", "").strip()
        zip_code = order.get("zip", "").strip()
        country = order.get("country", "US").strip() or "US"

        logger.info(
            "=== Processing order %s (row %d, ASIN=%s, qty=%d) ===",
            order_id,
            row_number,
            asin,
            qty,
        )

        last_result: CheckoutResult | None = None
        order_success = False

        for attempt in range(1, max_retries + 2):  # 1..max_retries+1
            try:
                result = run_guest_checkout(
                    asin=asin,
                    quantity=qty,
                    buyer_name=buyer_name,
                    shipping_address=shipping_address,
                    city=city,
                    state=state,
                    zip_code=zip_code,
                    country=country,
                    screenshots_base_dir=CHECKOUT_SCREENSHOT_DIR,
                )
                last_result = result

                # Non-retryable failure: emit immediately.
                if not result.success and not _is_retryable(result):
                    logger.info(
                        "Order %s -- non-retryable failure: %s",
                        order_id,
                        result.notes,
                    )
                    order_success = False
                    break

                # Sign-in wall that we could not recover from with a logged-in
                # session: the operator needs to run bootstrap_amazon_session.py
                # before re-running the demo.
                if (
                    not result.success
                    and isinstance(result.detail.get("error_type"), str)
                    and "sign_in" in result.detail["error_type"].lower()
                ):
                    logger.warning(
                        "Order %s hit an unrecoverable sign-in wall. Set status "
                        "to Error with an operator-facing message.",
                        order_id,
                    )
                    order_success = False
                    break

                # Success: we reached the final summary page.
                if result.success:
                    logger.info("Order %s -- reached checkout summary page", order_id)
                    order_success = True
                    break

                # Retryable failure -- continue unless we're out of attempts.
                logger.warning(
                    "Order %s -- attempt %d/%d failed (retryable): %s",
                    order_id,
                    attempt,
                    max_retries + 1,
                    result.notes,
                )
                if attempt > max_retries + 1:
                    break
                time.sleep(2)

            except (SignInWallError, SoldOutError, InvalidAsinError, CaptchaDetectedError) as exc:
                # Non-retryable, specific failure.
                logger.info("Order %s -- %s: %s", order_id, type(exc).__name__, exc.reason)
                sign_in_wall = isinstance(exc, SignInWallError)
                last_result = CheckoutResult(
                    order_id=order_id,
                    asin=asin,
                    quantity=qty,
                    success=False,
                    status_to_write=STATUS_ERROR,
                    notes=exc.reason,
                    detail={"error_type": type(exc).__name__, "reason": exc.reason},
                )
                order_success = False

                if sign_in_wall:
                    # Do not retrya sign-in wall -- the operator must bootstrap
                    # a fresh session and re-run the demo.
                    logger.warning(
                        "Order %s hit a sign-in wall -- marked as non-retryable "
                        "Error. Run bootstrap_amazon_session.py to refresh the "
                        "Amazon buyer session, then re-run the demo.",
                        order_id,
                    )
                    break

                raise

            except GuestCheckoutError as exc:
                # Retryable transient error (e.g. Playwright timeout).
                logger.warning(
                    "Order %s -- attempt %d failed (retryable): %s",
                    order_id,
                    attempt,
                    exc.reason,
                )
                last_result = CheckoutResult(
                    order_id=order_id,
                    asin=asin,
                    quantity=qty,
                    success=False,
                    status_to_write=STATUS_ERROR,
                    notes=exc.reason,
                    detail={"error_type": type(exc).__name__, "reason": exc.reason},
                )
                if attempt > max_retries + 1:
                    order_success = False
                    break
                time.sleep(2)

            except Exception as exc:
                logger.exception("Order %s -- unexpected error: %s", order_id, exc)
                last_result = CheckoutResult(
                    order_id=asin,
                    asin=asin,
                    quantity=qty,
                    success=False,
                    status_to_write=STATUS_ERROR,
                    notes=f"unexpected error: {type(exc).__name__}: {exc}",
                    detail={"error_type": type(exc).__name__, "reason": str(exc)},
                )
                order_success = False
                break

        if last_result is None:
            last_result = CheckoutResult(
                order_id=asin,
                asin=asin,
                quantity=qty,
                success=False,
                status_to_write=STATUS_ERROR,
                notes="no result produced",
                detail={"error_type": "no_result", "reason": "no result produced"},
            )

        # Write back to the Sheet.
        if last_result.success:
            # Status = "Fulfilling", notes = captured price + delivery + approval reminder.
            _update_sheet(
                sheet_id,
                row_number,
                last_result.status_to_write,
                notes=last_result.notes,
                amazon_asin_sku=asin,
            )
            processed += 1
        else:
            # Status = "Error", notes = specific failure reason.
            _update_sheet(
                sheet_id,
                row_number,
                last_result.status_to_write,
                notes=last_result.notes,
                amazon_asin_sku=asin,
            )
            failed += 1

        results.append(_report_result(last_result, order))

        # Small pause between orders to avoid hammering Amazon.
        time.sleep(1.5)

    return {
        "processed": processed,
        "failed": failed,
        "results": results,
        "message": f"Processed {processed} order(s), {failed} failed.",
    }


def _is_retryable(result: CheckoutResult) -> bool:
    """Return True if a failed CheckoutResult is worth retrying."""
    # Hard failures that should NOT be retried.
    non_retryable_notes = [
        "guest checkout unavailable",
        "out of stock",
        "sold out",
        "invalid or unreachable ASIN",
        "CAPTCHA detected",
    ]
    note_lower = (result.notes or "").lower()
    if any(k in note_lower for k in non_retryable_notes):
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Demo: drive guest Amazon checkout for pending/mapped Sheet orders."
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Max retries per order for transient errors (default: 2).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger.info("Starting guest-checkout demo against sheet %s", SHEET_ID)
    summary = run_demo(SHEET_ID, max_retries=args.max_retries)

    print("\n" + "=" * 72)
    print("GUEST CHECKOUT DEMO -- RESULTS")
    print("=" * 72)
    print(f"Sheet: {SHEET_ID}")
    print(f"Processed: {summary['processed']}")
    print(f"Failed:    {summary['failed']}")
    print(f"Message:   {summary['message']}")
    print("-" * 72)

    for r in summary.get("results", []):
        print(f"\nOrder: {r['order_id']}  (ASIN: {r['asin']}, qty: {r['qty']})")
        print(f"  Status written to Sheet: {r['status_written']}")
        print(f"  Notes: {r['notes']}")
        if r.get("total_price"):
            print(f"  Total price: {r['total_price']}")
        if r.get("estimated_delivery"):
            print(f"  Est. delivery: {r['estimated_delivery']}")
        if r.get("screenshot"):
            print(f"  Screenshot: {r['screenshot']}")
        if r.get("detail"):
            err_type = r["detail"].get("error_type")
            if err_type:
                print(f"  Error type: {err_type}")
    print("\n" + "=" * 72)
    print("REMINDER: No real purchase was placed. Status 'Fulfilled' was NOT set.")
    print("Screenshots saved under: backend/screenshots/")
    print("=" * 72)

    # Exit code: 0 if all succeeded, 1 if any failed.
    if summary["failed"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
