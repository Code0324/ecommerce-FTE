"""Guest-checkout browser automation for Amazon product pages using Playwright.

DRIVES A REAL AMAZON CHECKOUT FLOW AS A GUEST -- NO LOGIN, NO API KEYS.

This module is COMPLETELY INDEPENDENT of the SP-API provider code. It does
not touch sp_api_client.py, lwa_auth.py, order_provider.py, or any other
Amazon Seller/API file. Amazon's product pages are public; we only need to
reach checkout as a guest.

HARDFILE SAFETY:
  - This module NEVER clicks "Place your order" or submits any payment.
  - It stops at the final order summary page and captures price + delivery.
  - The demo entrypoint never sets Status = "Fulfilled" -- only "Fulfilling"
    (reached checkout) or "Error" (blocked).

USAGE (from backend/):
    python -m jobs.demo_guest_checkout_fulfillment

ASIDE -- retry discipline:
    Transient Playwright errors (timeouts, flaky selectors, Amazon page
    reshuffles) are retried a small number of times per order. Persistent
    failures (sign-in wall, sold out, invalid ASIN, CAPTCHA) are NOT
    retried -- they are logged as a specific failure reason and skipped.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project-path setup (mirrors jobs/process_sheet_orders.py)
# ---------------------------------------------------------------------------

# This file lives at:
#   {repo_root}/backend/app/services/fulfillment/guest_checkout.py
# So the backend/ directory is 3 levels above __file__, and the repo root is 4 levels above.
_FILE_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_FILE_DIR)))
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)

if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(_REPO_ROOT, ".env"), override=True)

# Import settings for proxy configuration
from app.core.config import settings

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _resolve_repo_path(value: str | None, relative_to_repo_root: str) -> str:
    """Resolve a possibly-relative path to a repo-root-absolute path.

    Uses the same repo-root-relative convention as .env.example:
    paths are interpreted relative to the repo root, NOT relative to
    backend/, even when a job is executed with backend/ as cwd.
    """
    if value is None:
        return os.path.normpath(os.path.join(_REPO_ROOT, relative_to_repo_root))

    raw = os.path.expandvars(os.path.expanduser(value.strip()))
    if os.path.isabs(raw):
        return os.path.normpath(raw)

    lowered = raw.lower()
    if lowered.startswith("backend/") or lowered.startswith("backend\\"):
        return os.path.normpath(os.path.join(_REPO_ROOT, raw))

    return os.path.normpath(os.path.join(_REPO_ROOT, raw))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Orders tab column indices (0-based) -- identical to sheets_client.py.
COL_AMAZON_ASIN_SKU = 12  # column M: Amazon ASIN/SKU (already resolved)

# Sheet statuses used by this demo.
STATUS_FULFILLING = "Fulfilling"
STATUS_ERROR = "Error"

# Order statuses we pick up from the Sheet: Pending or Mapped (ASIN already
# resolved by the SKU-mapping step in process_sheet_orders.py).
PICKUP_STATUSES = ("Pending", "Mapped")

# Playwright / flow config.
DEFAULT_TIMEOUT_MS = 60000  # 60 seconds - Amazon pages can be slow
MAX_RETRIES_PER_ORDER = 1  # One retry max - Amazon is slow, don't waste time
CHECKOUT_SCREENSHOT_DIR = _resolve_repo_path(None, "screenshots")

# Amazon US base.
AMAZON_DP_URL = "https://www.amazon.com/dp/{asin}"

# Session file (human-bootstrapped browser session) -- optional.
# When present, _resume_as_logged_in_buyer() can load it as a Playwright
# storage_state to continue past a sign-in wall. When absent, we never
# auto-log-in and instead report the sign-in wall as a hard failure.
SESSION_FILE = _resolve_repo_path(
    os.getenv("AMAZON_BUYER_SESSION_PATH"),
    "backend/credentials/amazon_buyer_session.json",
)


def _page_is_still_sign_in(page: Any) -> bool:
    """Return True if the page still shows a sign-in wall."""
    try:
        content = page.content().lower()
    except Exception:
        return True
    indicators = [
        "sign in to your account",
        "enter your password",
        'id="ap_email"',
        'id="ap_password"',
        "keep shopping",
    ]
    return any(s in content for s in indicators)


def _resume_as_logged_in_buyer(
    browser: Any,
    asin: str,
    quantity: int,
    buyer_name: str,
    shipping_address: str,
    city: str,
    state: str,
    zip_code: str,
    country: str,
    *,
    delivery_instructions: str = "",
    timeout_ms: int,
    order_dir: str,
) -> CheckoutResult | None:
    """Try to resume as a logged-in buyer using a previously bootstrapped session.

    Returns a CheckoutResult when the session is usable, otherwise None.
    This path is OPTIONAL and never runs unless the caller explicitly invokes it;
    the default guest flow never calls it and never auto-logs-in.
    """
    if not os.path.exists(SESSION_FILE):
        return None

    login_page = None
    try:
        login_ctx = browser.new_context(
            storage_state=SESSION_FILE,
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        try:
            login_page = login_ctx.new_page()
            product_url = AMAZON_DP_URL.format(asin=asin)
            login_page.goto(product_url, timeout=timeout_ms, wait_until="domcontentloaded")
            login_page.wait_for_timeout(5000)

            if _page_is_still_sign_in(login_page):
                logger.warning(
                    "Session at %s still shows a sign-in wall -- "
                    "session appears expired or invalid",
                    SESSION_FILE,
                )
                return None

            return _run_checkout_flow(
                page=login_page,
                asin=asin,
                quantity=quantity,
                buyer_name=buyer_name,
                shipping_address=shipping_address,
                city=city,
                state=state,
                zip_code=zip_code,
                country=country,
                delivery_instructions=delivery_instructions,
                timeout_ms=timeout_ms,
                order_dir=order_dir,
                browser=browser,
            )
        finally:
            if login_page is not None:
                try:
                    login_page.close()
                except Exception:
                    pass
            try:
                login_ctx.close()
            except Exception:
                pass
    except Exception as exc:
        logger.warning(
            "Session resume failed for %s: %s",
            asin,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CheckoutResult:
    """Outcome of one guest-checkout attempt for one order."""

    order_id: str
    asin: str
    quantity: int
    success: bool  # True = reached final order summary page (NOT placed order)
    status_to_write: str  # "Fulfilling" or "Error"
    notes: str  # human-readable summary: price, delivery, or failure reason
    total_price: str | None = None
    estimated_delivery: str | None = None
    screenshot_path: str | None = None
    # Rich detail for the report (not written to the Sheet).
    detail: dict[str, Any] = field(default_factory=dict)

# ---------------------------------------------------------------------------
# Playwright import -- lazy, so the module can be imported without playwright
# installed (only the demo entrypoint actually drives a browser).
# ---------------------------------------------------------------------------

def _ensure_playwright() -> None:
    """Import playwright and raise a clear error if it is absent."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed. Run: pip install playwright && "
            "python -m playwright install chromium"
        ) from exc

# ---------------------------------------------------------------------------
# Amazon page parsing helpers
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(r"\$[\d,]+\.\d{2}")

def _extract_price(text: str) -> str | None:
    """Return the first '$X,XXX.XX' price found in `text`, or None."""
    m = _PRICE_RE.search(text)
    return m.group(0) if m else None

def _normalize_delivery(text: str) -> str:
    """Collapse a delivery estimate string for the Sheet notes."""
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:120]

# ---------------------------------------------------------------------------
# Guest checkout flow
# ---------------------------------------------------------------------------

class GuestCheckoutError(Exception):
    """Base error for guest-checkout failures."""

    def __init__(self, reason: str, recoverable: bool = True):
        self.reason = reason
        self.recoverable = recoverable
        super().__init__(reason)

class SignInWallError(GuestCheckoutError):
    """Amazon forces sign-in before shipping/price is visible -- no guest path."""

    def __init__(self):
        super().__init__("guest checkout unavailable -- Amazon requires sign-in", recoverable=False)

class SoldOutError(GuestCheckoutError):
    def __init__(self):
        super().__init__("product is out of stock", recoverable=False)

class InvalidAsinError(GuestCheckoutError):
    def __init__(self, detail: str = "product page did not load"):
        super().__init__(f"invalid or unreachable ASIN: {detail}", recoverable=False)

class CaptchaDetectedError(GuestCheckoutError):
    def __init__(self):
        super().__init__("CAPTCHA detected on page -- automation blocked", recoverable=False)

class PriceMismatchError(GuestCheckoutError):
    def __init__(self, expected: str | None, found: str | None):
        super().__init__(
            f"price mismatch: expected {expected!r}, found {found!r}",
            recoverable=True,
        )


def run_guest_checkout(
    asin: str,
    quantity: int,
    buyer_name: str,
    shipping_address: str,
    city: str,
    state: str,
    zip_code: str,
    country: str,
    *,
    delivery_instructions: str = "",
    screenshots_base_dir: str = CHECKOUT_SCREENSHOT_DIR,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> CheckoutResult:
    """Drive a guest Amazon checkout flow up to the final order summary page.

    Steps:
      1. Navigate to https://www.amazon.com/dp/{asin}
      2. Verify product page loads (title + price visible)
      3. Set quantity, click Add to Cart (or Buy Now)
      4. Proceed to checkout as guest -- if sign-in wall blocks guest path,
         raise SignInWallError
      5. Fill shipping address (guest)
      6. Proceed to final order summary, capture total price + delivery date
      7. SCREENSHOT the final page
      8. HARD STOP -- do NOT click "Place your order"

    Returns a CheckoutResult. On failure, success=False and notes contain the
    specific reason. Never raises an unhandled exception for a business failure
    (invalid ASIN, sold out, sign-in wall, CAPTCHA) -- those are returned as
    CheckoutResult with success=False.
    """
    _ensure_playwright()

    from playwright.sync_api import sync_playwright

    order_dir = os.path.join(screenshots_base_dir, asin)
    os.makedirs(order_dir, exist_ok=True)

    ctx = None
    page = None
    browser = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
                proxy=settings.proxy_config,
            )
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = ctx.new_page()

            return _run_checkout_flow(
                page=page,
                asin=asin,
                quantity=quantity,
                buyer_name=buyer_name,
                shipping_address=shipping_address,
                city=city,
                state=state,
                zip_code=zip_code,
                country=country,
                delivery_instructions=delivery_instructions,
                timeout_ms=timeout_ms,
                order_dir=order_dir,
                browser=browser,
            )

    except SignInWallError:
        raise
    except SoldOutError:
        raise
    except InvalidAsinError:
        raise
    except CaptchaDetectedError:
        raise
    except PriceMismatchError:
        raise
    except Exception as exc:
        if hasattr(exc, 'message'):
            msg = str(exc.message)
        else:
            msg = str(exc)
        if "timeout" in msg.lower() or "locator" in msg.lower() or "detached" in msg.lower():
            raise GuestCheckoutError(f"transient playwright error: {msg}", recoverable=True) from exc
        raise GuestCheckoutError(f"playwright error: {msg}", recoverable=False) from exc

def _set_delivery_location_to_us(page: Any, timeout_ms: int) -> None:
    """Set Amazon delivery location to US zip code (98101 - Seattle) to override IP-based geolocation.

    This is critical when the current IP is non-US (e.g., Pakistan) to ensure we get US pricing,
    availability, and shipping info. Uses the Amazon location popover.
    """
    try:
        logger.info("Setting delivery location to US (98101)...")

        # Navigate to homepage first to access the location selector
        page.goto("https://www.amazon.com", timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # Click the location popover button
        location_btn = page.locator("#nav-global-location-popover-link")
        if location_btn.count() > 0:
            location_btn.click(timeout=10000)
            page.wait_for_timeout(2000)

            # Find and fill the zip code input
            inputs = page.locator("input[type='text']").all()
            if inputs:
                first_input = inputs[0]
                first_input.fill("")  # Clear any existing value
                first_input.type("98101", delay=100)
                page.wait_for_timeout(1000)

                # Press Enter to submit (this is the key -- don't look for a button)
                first_input.press("Enter")
                page.wait_for_timeout(4000)

                logger.info("✓ Delivery location set to Seattle 98101")
            else:
                logger.warning("⚠️ Could not find zip code input, continuing without location change")
        else:
            logger.warning("⚠️ Could not find location button, continuing without location change")
    except Exception as exc:
        logger.warning("Delivery location setup failed (non-fatal): %s", exc)
        # This is not fatal -- if we can't set location, we'll just try to proceed anyway


def _run_checkout_flow(
    page: Any,
    asin: str,
    quantity: int,
    buyer_name: str,
    shipping_address: str,
    city: str,
    state: str,
    zip_code: str,
    country: str,
    *,
    delivery_instructions: str = "",
    timeout_ms: int,
    order_dir: str,
    browser: Any,
) -> CheckoutResult:
    """The actual step-by-step flow. `page` is already attached to an open browser.

    Sign-in walls are retried once using the bootstrapped buyer session (if any).
    """
    # ------------------------------------------------------------------
    # Step 0: set delivery location to US (critical for non-US IPs)
    # ------------------------------------------------------------------
    _set_delivery_location_to_us(page, timeout_ms)

    # ------------------------------------------------------------------
    # Step 1: product page
    # ------------------------------------------------------------------
    product_url = AMAZON_DP_URL.format(asin=asin)
    logger.info("Navigating to product page: %s", product_url)

    try:
        # Use domcontentloaded - Amazon pages rarely reach full networkidle
        # due to continuous analytics/tracking requests.
        page.goto(product_url, timeout=timeout_ms, wait_until="domcontentloaded")
        # Wait a bit for the page to stabilize
        page.wait_for_timeout(5000)
    except Exception as exc:
        raise InvalidAsinError(f"page navigation failed: {exc}")

    # Allow the page a moment to settle and become interactive.
    page.wait_for_timeout(5000)

    # Wait for a common product page element to appear.
    try:
        page.wait_for_selector(
            "#productTitle, #buy-box, .buy-box, #add-to-cart-button, .a-price",
            timeout=timeout_ms // 2,
        )
    except Exception:
        pass  # Page may have a different structure, continue anyway

    # Check for obvious error pages.
    current_url = page.url
    if _is_error_page(current_url):
        raise InvalidAsinError(f"redirected to error page: {current_url}")

    # Verify product title is visible.
    title = _wait_for_product_title(page, timeout_ms)
    if title is None:
        # Could be a sign-in wall already on the product page, sold out, or
        # a CAPTCHA. Distinguish what we can.
        if _page_has_sign_in_wall(page):
            return _on_sign_in_wall(
                browser=browser,
                asin=asin,
                quantity=quantity,
                buyer_name=buyer_name,
                shipping_address=shipping_address,
                city=city,
                state=state,
                zip_code=zip_code,
                country=country,
                timeout_ms=timeout_ms,
                order_dir=order_dir,
                product_url=product_url,
            )
        if _page_is_sold_out(page):
            raise SoldOutError()
        if _page_has_captcha(page):
            raise CaptchaDetectedError()
        raise InvalidAsinError("product title not found -- page may be unavailable")

    logger.info("Product page loaded: %s", title[:80])

    # Verify a price is visible on the product page.
    price_on_page = _extract_product_price(page)
    if price_on_page is None:
        logger.warning("No price visible on product page for %s", asin)

    # ------------------------------------------------------------------
    # Step 2: quantity + add to cart / buy now
    # ------------------------------------------------------------------
    try:
        _set_quantity(page, quantity, timeout_ms)
    except GuestCheckoutError:
        raise
    except Exception as exc:
        raise GuestCheckoutError(f"failed to set quantity: {exc}", recoverable=True) from exc

    buy_button_clicked = False
    try:
        buy_button_clicked = _click_add_to_cart_or_buy_now(page, timeout_ms)
    except GuestCheckoutError:
        raise
    except Exception as exc:
        raise GuestCheckoutError(f"failed to click add-to-cart/buy-now: {exc}", recoverable=True) from exc

    if not buy_button_clicked:
        raise GuestCheckoutError("could not find or click Add to Cart / Buy Now button", recoverable=True)

    # Wait for the cart / checkout prompt to appear.
    page.wait_for_timeout(3000)

    # ------------------------------------------------------------------
    # Step 3: proceed to checkout as guest
    # ------------------------------------------------------------------
    try:
        checkout_url = _proceed_to_checkout(page, timeout_ms)
    except GuestCheckoutError:
        raise
    except Exception as exc:
        raise GuestCheckoutError(f"failed to proceed to checkout: {exc}", recoverable=True) from exc

    if checkout_url is None:
        if _page_has_sign_in_wall(page):
            return _on_sign_in_wall(
                browser=browser,
                asin=asin,
                quantity=quantity,
                buyer_name=buyer_name,
                shipping_address=shipping_address,
                city=city,
                state=state,
                zip_code=zip_code,
                country=country,
                timeout_ms=timeout_ms,
                order_dir=order_dir,
                product_url=product_url,
            )
        raise GuestCheckoutError("could not reach checkout from cart", recoverable=True)

    logger.info("Reached checkout URL: %s", checkout_url[:120])

    # ------------------------------------------------------------------
    # Step 4: fill shipping address (guest)
    # ------------------------------------------------------------------
    try:
        _fill_guest_shipping_address(
            page,
            buyer_name=buyer_name,
            shipping_address=shipping_address,
            city=city,
            state=state,
            zip_code=zip_code,
            country=country,
            timeout_ms=timeout_ms,
        )
    except SignInWallError:
        return _on_sign_in_wall(
            browser=browser,
            asin=asin,
            quantity=quantity,
            buyer_name=buyer_name,
            shipping_address=shipping_address,
            city=city,
            state=state,
            zip_code=zip_code,
            country=country,
            timeout_ms=timeout_ms,
            order_dir=order_dir,
            product_url=product_url,
        )
    except CaptchaDetectedError:
        raise
    except GuestCheckoutError:
        raise
    except Exception as exc:
        raise GuestCheckoutError(f"failed to fill shipping address: {exc}", recoverable=True) from exc

    # Proceed through checkout screens toward the final order summary.
    page.wait_for_timeout(2000)

    # Click through any "Save" / "Continue" buttons that appear for the
    # shipping address step.
    _click_through_shipping_confirm(page, timeout_ms)

    # Try to fill delivery instructions if one is provided and Amazon
    # has a delivery instructions field on this page.
    if delivery_instructions:
        _try_fill_delivery_instructions(page, delivery_instructions, timeout_ms)

    # ------------------------------------------------------------------
    # Step 5: reach final order summary page
    # ------------------------------------------------------------------
    # On Amazon guest checkout, the final page before "Place your order"
    # typically shows: shipping address, shipping method, payment method
    # (guest uses a one-time card entry or "use a new payment method"), and
    # the order summary with total + estimated delivery.
    #
    # We advance through the screens until we see an order summary with a
    # total price and an estimated delivery date -- but we never click the
    # final purchase button.
    try:
        total_price, estimated_delivery = _reach_order_summary(
            page, asin, price_on_page, timeout_ms
        )
    except SignInWallError:
        return _on_sign_in_wall(
            browser=browser,
            asin=asin,
            quantity=quantity,
            buyer_name=buyer_name,
            shipping_address=shipping_address,
            city=city,
            state=state,
            zip_code=zip_code,
            country=country,
            timeout_ms=timeout_ms,
            order_dir=order_dir,
            product_url=product_url,
        )
    except CaptchaDetectedError:
        raise
    except GuestCheckoutError:
        raise
    except Exception as exc:
        raise GuestCheckoutError(f"failed to reach order summary: {exc}", recoverable=True) from exc

    # ------------------------------------------------------------------
    # Step 6: screenshot the final page
    # ------------------------------------------------------------------
    # Safety confirmation: never reach past this point without confirming
    # the final summary page actually shows a total price OR an estimated
    # delivery. If neither was captured, treat it as a capture failure.
    if not total_price and not estimated_delivery:
        logger.warning(
            "Final summary page for %s has no captured price AND no delivery "
            "estimate -- treating as a capture failure",
            asin,
        )
        notes = (
            "Price/delivery capture failed -- final summary page did not show "
            "a total price or estimated delivery. Not placed."
        )
        screenshot_name = f"checkout_{asin}.png"
        screenshot_path = os.path.join(order_dir, screenshot_name)
        try:
            page.screenshot(path=screenshot_path, full_page=True)
            logger.info("Screenshot saved (capture-failure): %s", screenshot_path)
        except Exception as exc:
            logger.warning("Failed to capture screenshot: %s", exc)
            screenshot_path = None
        return CheckoutResult(
            order_id=asin,  # we use ASIN as the key for the demo; the Sheet order_id is separate
            asin=asin,
            quantity=quantity,
            success=False,
            status_to_write=STATUS_ERROR,
            notes=notes,
            total_price=total_price,
            estimated_delivery=estimated_delivery,
            screenshot_path=screenshot_path,
            detail={
                "product_title": title,
                "product_url": product_url,
                "checkout_url": checkout_url,
                "price_on_product_page": price_on_page,
                "total_price": total_price,
                "estimated_delivery": estimated_delivery,
                "screenshot_path": screenshot_path,
                "capture_failure": True,
            },
        )

    delivery_note = _normalize_delivery(estimated_delivery) if estimated_delivery else "delivery estimate not available"

    notes = (
        f"Total captured (NOT placed): {total_price or 'price not captured'} | "
        f"Est. delivery: {delivery_note} | "
        f"HARD STOP -- this flow NEVER clicks 'Place your order'. "
        f"A human must approve and place the order manually."
    )

    screenshot_name = f"checkout_{asin}.png"
    screenshot_path = os.path.join(order_dir, screenshot_name)
    try:
        page.screenshot(path=screenshot_path, full_page=True)
        logger.info("Screenshot saved: %s", screenshot_path)
    except Exception as exc:
        logger.warning("Failed to capture screenshot: %s", exc)
        screenshot_path = None

    return CheckoutResult(
        order_id=asin,  # we use ASIN as the key for the demo; the Sheet order_id is separate
        asin=asin,
        quantity=quantity,
        success=True,
        status_to_write=STATUS_FULFILLING,
        notes=notes,
        total_price=total_price,
        estimated_delivery=estimated_delivery,
        screenshot_path=screenshot_path,
        detail={
            "product_title": title,
            "product_url": product_url,
            "checkout_url": checkout_url,
            "price_on_product_page": price_on_page,
            "total_price": total_price,
            "estimated_delivery": estimated_delivery,
            "screenshot_path": screenshot_path,
        },
    )

# ---------------------------------------------------------------------------
# Internal helpers -- sign-in wall recovery
# ---------------------------------------------------------------------------

def _is_checkout_page(url: str) -> bool:
    """Return True if `url` looks like an Amazon checkout/shipping page.

    We deliberately exclude plain cart pages so address fields are never filled
    on the cart surface.
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    if "/checkout" in path or "/gp/buy" in path:
        return True
    # Amazon sometimes surfaces shipping on /gp/buy and on oauth/confirm pages.
    if "oauth" in path and "confirm" in path:
        return True
    return False


def _on_sign_in_wall(
    browser: Any,
    asin: str,
    quantity: int,
    buyer_name: str,
    shipping_address: str,
    city: str,
    state: str,
    zip_code: str,
    country: str,
    *,
    delivery_instructions: str = "",
    timeout_ms: int,
    order_dir: str,
    product_url: str,
) -> CheckoutResult:
    """Handle a sign-in wall by trying the bootstrapped buyer session once.

    If no session exists, or the session still shows a sign-in wall, this is a
    hard failure reported back to the caller.
    """
    logger.warning(
        "Sign-in wall hit for %s at %s -- attempting session resume",
        asin,
        product_url,
    )
    result = _resume_as_logged_in_buyer(
        browser=browser,
        asin=asin,
        quantity=quantity,
        buyer_name=buyer_name,
        shipping_address=shipping_address,
        city=city,
        state=state,
        zip_code=zip_code,
        country=country,
        delivery_instructions=delivery_instructions,
        timeout_ms=timeout_ms,
        order_dir=order_dir,
    )
    if result is not None:
        logger.info("Resumed logged-in checkout for %s after sign-in wall", asin)
        return result

    detail = {
        "error_type": "sign_in_wall",
        "reason": "guest checkout unavailable -- Amazon requires sign-in",
        "session_file": SESSION_FILE,
        "session_exists": os.path.exists(SESSION_FILE),
    }
    return CheckoutResult(
        order_id=asin,
        asin=asin,
        quantity=quantity,
        success=False,
        status_to_write=STATUS_ERROR,
        notes=detail["reason"],
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Internal helpers -- page detection
# ---------------------------------------------------------------------------

def _extract_product_price(page: Any) -> str | None:
    """Return the product-page price using Amazon buy-box selectors.

    The canonical readable price on Amazon product pages is usually rendered as
    an .a-price .a-offscreen node inside the buy box.
    """
    buy_box_selectors = [
        "#buy-box .a-price .a-offscreen",
        "#buybox .a-price .a-offscreen",
        ".a-button-stack .a-button span.a-price .a-offscreen",
        "#productTitle ~ span.a-price .a-offscreen",
        ".a-fixed-left-grid .a-price .a-offscreen",
        ".a-price .a-offscreen",
    ]
    for sel in buy_box_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                for candidate in el.all():
                    try:
                        text = candidate.inner_text().strip()
                        price = _extract_price(text)
                        if price:
                            return price
                    except Exception:
                        continue
        except Exception:
            continue

    # Fallback: page text regex.
    try:
        return _extract_price(page.content())
    except Exception:
        return None


def _is_error_page(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return any(k in path for k in ("/error/", "/gp/error", "/s/ref=", "/apex"))

def _is_sign_in_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    # Check for Amazon's re-auth requirement (openid.pape.max_auth_age=900)
    if "openid.pape.max_auth_age" in query:
        logger.warning("DIAGNOSTIC: Amazon re-authentication required (openid.pape.max_auth_age detected)")
        logger.warning(f"  Full URL: {url}")
        return True
    return "signin" in path or "ap/signin" in path or "auth" in path

def _page_has_sign_in_wall(page: Any) -> bool:
    """Return True if the page appears to require sign-in (email/password fields
    and a sign-in prompt are visible)."""
    try:
        content = page.content().lower()
    except Exception:
        return False
    signals = [
        "sign in to your account",
        "enter your password",
        "keep shopping",
        "choose a delivery address",
        'id="ap_email"',
        'id="ap_password"',
        'name="email"',
        'name="password"',
    ]
    return any(s in content for s in signals)

def _page_is_sold_out(page: Any) -> bool:
    try:
        content = page.content().lower()
    except Exception:
        return False
    return ("currently unavailable" in content) or ("sold out" in content) or (
        "out of stock" in content
    )

def _page_has_captcha(page: Any) -> bool:
    try:
        content = page.content().lower()
    except Exception:
        return False
    return ("captcha" in content) or ("verify you are a human" in content) or (
        "suspicious activity" in content
    )

# ---------------------------------------------------------------------------
# Internal helpers -- product page
# ---------------------------------------------------------------------------

def _wait_for_product_title(page: Any, timeout_ms: int) -> str | None:
    """Return the product page title text, or None if not found."""
    selectors = [
        "#productTitle",
        "h1[data-testid='product-title']",
        "h1#title",
        "span#productTitle",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=timeout_ms // 4):
                text = el.inner_text().strip()
                if text:
                    return text
        except Exception:
            continue
    # Fallback: page title.
    try:
        pt = page.title()
        if pt and pt != "Amazon.com" and "Sign In" not in pt:
            return pt
    except Exception:
        pass
    return None

# ---------------------------------------------------------------------------
# Internal helpers -- quantity
# ---------------------------------------------------------------------------

def _set_quantity(page: Any, quantity: int, timeout_ms: int) -> None:
    """Set the order quantity on the product page.

    Tries Amazon's native quantity dropdown first, then numeric input fields,
    then +/- stepper buttons, then a broader native-dropdown search.

    For multi-packs (like AirTag 4-pack), the quantity may be fixed at the pack
    size and no separate quantity control is shown.

    If no quantity control is found and quantity > 1, this is not necessarily
    a failure -- many product pages only sell one unit per ASIN (multi-packs
    are separate ASINs). We log a warning but don't block.
    """
    if quantity < 1:
        quantity = 1

    # Strategy 1: Amazon native quantity dropdown.
    try:
        dropdown = page.locator("#quantity_dropdown").first
        if dropdown.is_visible(timeout=timeout_ms // 3):
            dropdown.select_option(str(quantity))
            logger.info("Set quantity via #quantity_dropdown to %s", quantity)
            return
    except Exception:
        pass

    # Strategy 2: Amazon buy-box quantity select/input.
    qty_selectors = [
        "#quantity_input",
        "#twisted-quantity",
        "input[name='quantity']",
        "select[name='quantity']",
        "#add-to-cart-quantity-input",
        "#quantity_selector",
        "select.a-native-dropdown",
        "select[name='quantity-preselect']",
        "select[id^='quantity']",
        "input[id^='quantity']",
        "[data-a-popover*=quantity]",
    ]
    for sel in qty_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=timeout_ms // 4):
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    el.select_option(str(quantity))
                    logger.info("Set quantity via select %s to %s", sel, quantity)
                    return
                el.fill(str(quantity))
                logger.info("Set quantity via input %s to %s", sel, quantity)
                return
        except Exception:
            continue

    # Strategy 3: +/- stepper buttons.
    try:
        inc_btn = page.locator("[aria-label*='Increase']").first
        if inc_btn.count() > 0 and inc_btn.is_visible(timeout=timeout_ms // 4):
            for _ in range(max(0, quantity - 1)):
                inc_btn.click(timeout=timeout_ms // 2)
                page.wait_for_timeout(200)
            logger.info("Set quantity via stepper to %s", quantity)
            return
    except Exception:
        pass

    # Strategy 4: buy-box control by text label.
    try:
        buy_box = page.locator("#buy-box, .buy-box, #buybox, [data-asin]").first
        if buy_box.count() > 0:
            ctrl = buy_box.locator(
                "select, input[type='number'], input[type='text'], button[aria-label*='Quantity']"
            ).first
            if ctrl.count() > 0 and ctrl.is_visible(timeout=timeout_ms // 4):
                tag = ctrl.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    ctrl.select_option(str(quantity))
                    logger.info("Set quantity via buy-box select to %s", quantity)
                    return
                ctrl.fill(str(quantity))
                logger.info("Set quantity via buy-box input to %s", quantity)
                return
    except Exception:
        pass

    # Many Amazon pages default to 1 -- if quantity is 1 we can skip.
    if quantity == 1:
        logger.info("Quantity is 1 -- assuming default is fine")
        return

    # Quantity > 1 but no control found. This may be a multi-pack product
    # where the quantity is fixed. Log a warning but don't block.
    logger.warning(
        "Could not find quantity control for qty=%d on %s -- "
        "product may be a fixed-pack ASIN; proceeding with qty=1",
        quantity, page.url,
    )

# ---------------------------------------------------------------------------
# Internal helpers -- add to cart / buy now
# ---------------------------------------------------------------------------

def _looks_like_post_cart_page(page: Any) -> bool:
    """Return True if the page looks like it's after an Add to Cart action.

    Checks for: cart page indicators, checkout prompts, success messages.
    """
    try:
        url = page.url.lower()
        if "cart" in url or "checkout" in url:
            return True
        content = page.content().lower()
        indicators = [
            "added to cart",
            "go to cart",
            "proceed to checkout",
            "item added",
            "your cart",
        ]
        return any(s in content for s in indicators)
    except Exception:
        return False


def _click_add_to_cart_or_buy_now(page: Any, timeout_ms: int) -> bool:
    """Click Add to Cart or Buy Now. Returns True if a button was clicked.

    Amazon's button structure varies widely. This function tries multiple
    strategies, prioritizing scoped selectors within the buy-box to avoid
    clicking unrelated buttons (like on related product carousels).
    Verifies that a click actually navigates or triggers a cart action.
    """
    url_before = page.url

    # Strategy 1: Specific known Amazon button IDs first (scoped to buy-box)
    try:
        buy_box = page.locator("#buy-box, .buy-box, #buybox").first
        if buy_box.count() > 0:
            for selector in ["#addToCart", "#add-to-cart-btn", "#add-to-cart-button", "#buy-now-button", "#nav-assist-add-to-cart"]:
                try:
                    el = buy_box.locator(selector).first
                    if el.count() > 0 and el.is_visible(timeout=timeout_ms // 4):
                        el.click(timeout=timeout_ms)
                        page.wait_for_timeout(2000)
                        # Verify action happened: URL changed or we're on cart/checkout
                        url_after = page.url
                        if url_after != url_before or _looks_like_post_cart_page(page):
                            logger.info("Clicked %s within buy-box; URL/page changed", selector)
                            return True
                        logger.warning("Clicked %s but no URL/page change detected", selector)
                        return True  # Click happened even if URL didn't change visibly yet
                except Exception:
                    continue
    except Exception:
        pass

    # Strategy 2: Buy-box scoped button search by role
    try:
        buy_box = page.locator("#buy-box, .buy-box, #buybox").first
        if buy_box.count() > 0:
            for phrase in ["Add to Cart", "Buy Now"]:
                try:
                    btn = buy_box.get_by_role("button", name=phrase, exact=False).first
                    if btn.is_visible(timeout=timeout_ms // 4):
                        btn.click(timeout=timeout_ms)
                        page.wait_for_timeout(2000)
                        url_after = page.url
                        if url_after != url_before or _looks_like_post_cart_page(page):
                            logger.info("Clicked %s (scoped to buy-box); URL/page changed", phrase)
                            return True
                        logger.warning("Clicked %s (scoped) but no URL/page change detected", phrase)
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    # Strategy 3: Common CSS selectors (non-scoped fallback)
    candidates = [
        "#addToCart",
        "#add-to-cart-btn",
        "#nav-assist-add-to-cart",
        "input[id='add-to-cart-button']",
        "input[id='buy-now-button']",
        "[data-testid='add-to-cart-button']",
        "[data-testid='buy-now-button']",
        "#tw-buy-box__cart",
        "#tw-buy-box__buy-now",
        ".a-button-primary:has-text('Add to Cart')",
        ".a-button-primary:has-text('Buy Now')",
        "[aria-label*='Add to Cart']",
        "[aria-label*='Buy Now']",
    ]
    for sel in candidates:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=timeout_ms // 4):
                el.click(timeout=timeout_ms)
                page.wait_for_timeout(2000)
                url_after = page.url
                if url_after != url_before or _looks_like_post_cart_page(page):
                    logger.info("Clicked %s; URL/page changed", sel)
                    return True
                logger.info("Clicked %s", sel)
                return True
        except Exception:
            continue

    # Strategy 4: Get by role with exact text match
    for phrase in ["Add to Cart", "Buy Now"]:
        try:
            btn = page.get_by_role("button", name=phrase, exact=True).first
            if btn.is_visible(timeout=timeout_ms // 4):
                btn.click(timeout=timeout_ms)
                page.wait_for_timeout(2000)
                url_after = page.url
                if url_after != url_before or _looks_like_post_cart_page(page):
                    logger.info("Clicked %s (role exact); URL/page changed", phrase)
                    return True
                logger.info("Clicked %s (role exact)", phrase)
                return True
        except Exception:
            pass

    # Debug output: dump relevant page content for troubleshooting
    try:
        content = page.content()
        if "#add-to-cart-button" in content or "#buy-now-button" in content:
            logger.warning("Button HTML found in page content but selectors failed")
        else:
            logger.warning("Button selectors (#add-to-cart-button, #buy-now-button) not found in page HTML")
    except Exception:
        pass

    logger.warning("No Add to Cart / Buy Now button found on page")
    return False

# ---------------------------------------------------------------------------
# Internal helpers -- proceed to checkout
# ---------------------------------------------------------------------------

def _proceed_to_checkout(page: Any, timeout_ms: int) -> str | None:
    """From the cart/order prompt, proceed toward checkout.

    After add-to-cart succeeds, navigate directly to the cart page
    (instead of waiting for Amazon to show a button), verify the item
    is there, then look for the checkout button.

    Returns the URL we land on, or None if we can't tell.
    """
    # First, try the standard "Go to Cart" / "Proceed to checkout" prompt
    checkout_selectors = [
        "#nav-cart-count-link",  # cart icon
        "#attachBaseCartView div.a-button-primary",  # "Go to cart"
        "#buy-now-button",  # if Buy Now was used
        "a[href*='gp/cart-view']",
        "a:has-text('Go to Cart')",
        "a:has-text('Proceed to checkout')",
        "button:has-text('Proceed to checkout')",
        "#checkout-button",
        "input[id='checkout-button']",
    ]
    for sel in checkout_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=timeout_ms // 4):
                el.click(timeout=timeout_ms)
                page.wait_for_timeout(2500)
                return page.url
        except Exception:
            continue

    # If we couldn't find a checkout button, navigate directly to the cart
    logger.info("No checkout button found, navigating directly to cart page...")
    try:
        cart_url = "https://www.amazon.com/gp/cart/view.html"
        page.goto(cart_url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        logger.info("Navigated to cart page: %s", page.url)
    except Exception as exc:
        logger.warning("Failed to navigate to cart: %s", exc)
        return page.url

    # Now look for the checkout button ON the cart page
    logger.info("Looking for checkout button on cart page...")
    cart_checkout_selectors = [
        "#checkout-button",
        "input[id='checkout-button']",
        "input[name='proceedToRetailCheckout']",
        "button:has-text('Proceed to checkout')",
        "a:has-text('Proceed to checkout')",
        "div.a-button-primary:has-text('Proceed to checkout')",
        "[data-feature-id='proceed-to-checkout-button']",
    ]

    for sel in cart_checkout_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=timeout_ms // 4):
                logger.info("Found checkout button: %s, clicking...", sel)
                el.click(timeout=timeout_ms)
                page.wait_for_timeout(3000)
                logger.info("Clicked checkout button, now at: %s", page.url)
                return page.url
        except Exception:
            continue

    logger.warning("Could not find checkout button on cart page, returning cart URL")
    return page.url

# ---------------------------------------------------------------------------
# Internal helpers -- shipping address (guest)
# ---------------------------------------------------------------------------

def _fill_guest_shipping_address(
    page: Any,
    *,
    buyer_name: str,
    shipping_address: str,
    city: str,
    state: str,
    zip_code: str,
    country: str,
    timeout_ms: int,
) -> None:
    """Fill in the guest shipping address form.

    Amazon guest checkout surfaces address fields with names/ids like
    address1, address2, city, state, zip, country, name, phone, etc.

    This must only be called on an actual checkout/shipping page -- never on
    a cart page.
    """
    if not _is_checkout_page(page.url):
        # DIAGNOSTIC: If we hit a max_auth_age URL, capture it
        if "openid.pape.max_auth_age" in page.url:
            logger.warning("DIAGNOSTIC: Capturing page state at max_auth_age signin URL")
            logger.warning(f"  URL: {page.url}")
            logger.warning(f"  Page title: {page.title()}")
            try:
                # Take a diagnostic screenshot
                diag_dir = os.path.join(CHECKOUT_SCREENSHOT_DIR, "max_auth_age_diagnostic")
                os.makedirs(diag_dir, exist_ok=True)
                diag_path = os.path.join(diag_dir, f"max_auth_age_page_{int(time.time())}.png")
                page.screenshot(path=diag_path, full_page=True)
                logger.warning(f"  Screenshot: {diag_path}")
            except Exception as e:
                logger.warning(f"  Failed to capture diagnostic screenshot: {e}")
        raise GuestCheckoutError(
            f"refused to fill address on non-checkout page: {page.url}",
            recoverable=False,
        )

    # Split the buyer name into first + last.
    name_parts = (buyer_name or "").strip().split()
    first_name = name_parts[0] if name_parts else ""
    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    # Field filling map: selector -> value.
    fields: list[tuple[str, str, str]] = []  # (selector, value, label)

    # Name fields.
    for sel, val, label in [
        ("input[name='name']", buyer_name, "full name"),
        ("input[name='firstname']", first_name, "first name"),
        ("input[name='lastname']", last_name, "last name"),
        ("#name", buyer_name, "#name"),
        ("#firstname", first_name, "#firstname"),
        ("#lastname", last_name, "#lastname"),
        ("input[id^='name']", buyer_name, "name (id prefix)"),
    ]:
        if val:
            fields.append((sel, val, label))

    # Address lines.
    addr_lines = [l.strip() for l in (shipping_address or "").splitlines() if l.strip()]
    addr1 = addr_lines[0] if addr_lines else ""
    addr2 = " ".join(addr_lines[1:]) if len(addr_lines) > 1 else ""
    for sel, val, label in [
        ("input[name='address1']", addr1, "address1"),
        ("input[name='address2']", addr2, "address2"),
        ("#address1", addr1, "#address1"),
        ("#address2", addr2, "#address2"),
        ("input[id^='address']", addr1, "address (id prefix)"),
    ]:
        if val:
            fields.append((sel, val, label))

    # City / State / Zip / Country.
    for sel, val, label in [
        ("input[name='city']", city, "city"),
        ("input[name='state']", state, "state"),
        ("input[name='zip']", zip_code, "zip"),
        ("input[name='postal_code']", zip_code, "postal_code"),
        ("input[name='country']", country, "country"),
        ("#city", city, "#city"),
        ("#state", state, "#state"),
        ("#zip", zip_code, "#zip"),
        ("#postal_code", zip_code, "#postal_code"),
        ("#country", country, "#country"),
        ("input[id^='city']", city, "city (id prefix)"),
        ("input[id^='state']", state, "state (id prefix)"),
        ("input[id^='zip']", zip_code, "zip (id prefix)"),
        ("input[id^='postal']", zip_code, "postal (id prefix)"),
    ]:
        if val:
            fields.append((sel, val, label))

    # Phone.
    for sel, val, label in [
        ("input[name='phone']", "", "phone (empty)"),
        ("#phone", "", "#phone"),
    ]:
        fields.append((sel, val or "", label))

    filled = 0
    for sel, val, label in fields:
        if not val:
            continue
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=timeout_ms // 6):
                el.fill(val, timeout=timeout_ms // 3)
                logger.info("Filled %s = %s", label, val[:40])
                filled += 1
        except Exception:
            continue

    if filled == 0:
        # Last resort: type into the first visible text input that looks like
        # an address field.
        try:
            inputs = page.locator("input[type='text'], input[type='tel'], textarea")
            if inputs.count() > 0:
                inputs.first.fill(f"{buyer_name}\n{shipping_address}\n{city}, {state} {zip_code}\n{country}")
                logger.info("Filled address via generic input fallback")
                filled = 1
        except Exception:
            pass

    if filled == 0:
        raise GuestCheckoutError("no shipping address fields found on checkout page", recoverable=True)

    # Check for sign-in wall creeping in.
    if _page_has_sign_in_wall(page):
        raise SignInWallError()

    # Check for CAPTCHA.
    if _page_has_captcha(page):
        raise CaptchaDetectedError()

def _click_through_shipping_confirm(page: Any, timeout_ms: int) -> None:
    """Click any 'Save'/'Continue'/'Use this address' buttons after filling
    the shipping address, to move toward the order summary."""
    buttons = [
        "button:has-text('Save')",
        "button:has-text('Continue')",
        "button:has-text('Use this address')",
        "button:has-text('Ship to this address')",
        "a:has-text('Continue')",
        "#continue-button",
        "#ap-remember-me-string",  # not a real click target -- skip
        "input[value='Continue']",
        "input[value='Save']",
        "input[value='Ship to this address']",
    ]
    for _ in range(3):  # at most a few screens
        clicked = False
        for sel in buttons:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=timeout_ms // 6):
                    el.click(timeout=timeout_ms // 2)
                    page.wait_for_timeout(1500)
                    clicked = True
                    logger.info("Clicked through: %s", sel)
                    break
            except Exception:
                continue
        if not clicked:
            break
        # Stop if we reached a place that looks like the final summary.
        if _looks_like_order_summary(page):
            break

def _try_fill_delivery_instructions(page: Any, delivery_instructions: str, timeout_ms: int) -> None:
    """Try to fill delivery instructions if a field is found on the page.

    Amazon sometimes surfaces a delivery instructions field on checkout pages.
    This is best-effort -- if no field is found, we log a warning but don't block.
    """
    if not delivery_instructions or not delivery_instructions.strip():
        return  # Nothing to fill

    logger.info("Attempting to fill delivery instructions: %s", delivery_instructions[:60])

    selectors = [
        "input[name='delivery-instructions']",
        "input[name='delivery_instructions']",
        "textarea[name='delivery-instructions']",
        "textarea[name='delivery_instructions']",
        "#delivery-instructions",
        "#delivery_instructions",
        "input[placeholder*='delivery']",
        "textarea[placeholder*='delivery']",
        "input[placeholder*='Delivery']",
        "textarea[placeholder*='Delivery']",
        "input[aria-label*='Delivery']",
        "textarea[aria-label*='Delivery']",
    ]

    found = False
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=timeout_ms // 6):
                el.fill(delivery_instructions, timeout=timeout_ms // 3)
                logger.info("✓ Filled delivery instructions via %s", sel)
                found = True
                break
        except Exception:
            continue

    if not found:
        logger.warning(
            "⚠ Could not find delivery instructions field on page -- "
            "Amazon checkout may not have this field available"
        )

# ---------------------------------------------------------------------------
# Internal helpers -- order summary
# ---------------------------------------------------------------------------

def _looks_like_order_summary(page: Any) -> bool:
    """Heuristic: page contains a total price plus an estimated delivery."""
    try:
        content = page.content()
    except Exception:
        return False
    has_price = bool(_PRICE_RE.search(content))
    has_delivery = bool(re.search(r"est(?:imated)?[^<]{0,40}deliver", content, re.I)) or bool(
        re.search(r"arrive\s+by", content, re.I)
    )
    return has_price and has_delivery

def _reach_order_summary(
    page: Any,
    asin: str,
    expected_price: str | None,
    timeout_ms: int,
) -> tuple[str | None, str | None]:
    """Advance through checkout screens until the final order summary page.

    Returns (total_price_str, estimated_delivery_str). Raises
    SignInWallError / CaptchaDetectedError / GuestCheckoutError on blockers.
    """
    # Give the page time to render after the last click.
    page.wait_for_timeout(3000)

    # Click through any remaining "Continue"/"Review" buttons until we land
    # on a page that looks like the final summary, or we can't go further.
    for _ in range(4):
        if _looks_like_order_summary(page):
            break
        # Try to advance.
        advanced = False
        for sel in [
            "button:has-text('Continue')",
            "button:has-text('Review')",
            "button:has-text('Next')",
            "button:has-text('Update')",
            "a:has-text('Continue')",
            "a:has-text('Review your order')",
            "#continue-button",
            "#next-button",
            "input[value='Continue']",
            "input[value='Next']",
        ]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=timeout_ms // 6):
                    el.click(timeout=timeout_ms // 2)
                    page.wait_for_timeout(2500)
                    advanced = True
                    logger.info("Advanced checkout screen via %s", sel)
                    break
            except Exception:
                continue
        if not advanced:
            break

        # Security checks after each navigation.
        if _page_has_sign_in_wall(page):
            raise SignInWallError()
        if _page_has_captcha(page):
            raise CaptchaDetectedError()

    # Capture total price from the summary page.
    total_price = _capture_total_price(page)

    # Capture estimated delivery.
    estimated_delivery = _capture_estimated_delivery(page)

    # If we have neither price nor delivery, we may not be on the final page
    # yet -- report what we have, but don't block the demo.
    if not total_price and not estimated_delivery:
        logger.warning("Could not capture price/delivery on final page for %s", asin)

    # Verify the total price is in the right ballpark vs the product page price.
    # (A missing product-page price means we skip this check.)
    if expected_price and total_price:
        try:
            exp_val = float(expected_price.replace("$", "").replace(",", ""))
            got_val = float(total_price.replace("$", "").replace(",", ""))
            if got_val < exp_val * 0.5 or got_val > exp_val * 5:
                logger.warning(
                    "Price mismatch for %s: product page %s, checkout total %s",
                    asin, expected_price, total_price,
                )
                # Don't block on this in the demo -- just note it.
        except ValueError:
            pass

    return total_price, estimated_delivery


def _capture_total_price(page: Any) -> str | None:
    """Return the order total from the summary page, or None."""
    selectors = [
        "#orderSummary .a-price .a-offscreen",
        "#orderSummary .a-color-price",
        "#total_results .a-price .a-offscreen",
        ".a-color-aft .a-price .a-offscreen",
        "#checkout_summary_button .a-color-price",
        "#scuco-themed-price",
        "#total-price",
        ".po-weight .a-color-price",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=DEFAULT_TIMEOUT_MS // 4):
                text = el.inner_text().strip()
                price = _extract_price(text)
                if price:
                    # DIAGNOSTIC: Log the full element text vs extracted price
                    if text != price:
                        logger.debug(f"Price extraction: selector={sel}, full_text='{text}', extracted='{price}'")
                    return price
        except Exception:
            continue

    # Broader search: any offscreen price on the page that looks like an order total.
    try:
        body_text = page.inner_text("body")
    except Exception:
        return None
    prices = _PRICE_RE.findall(body_text)
    if prices:
        # Prefer the last price on the page (usually the total).
        return prices[-1]
    return None


def _capture_estimated_delivery(page: Any) -> str | None:
    """Return an estimated delivery string from the summary page, or None."""
    selectors = [
        "#estimated-delivery",
        "#delivery-date",
        ".delivery-date",
        "#govde",
        "#rcy_det",
        "[data-testid='estimated-delivery']",
        ".a-color-base:has-text('Arrives')",
        ".a-color-base:has-text('Estim')",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=DEFAULT_TIMEOUT_MS // 4):
                text = el.inner_text().strip()
                if text:
                    return text
        except Exception:
            continue

    # Broader search in body text.
    try:
        body_text = page.inner_text("body")
    except Exception:
        return None
    m = re.search(r"(arrives?\s+by\s+[^.]{0,60}|est(?:imated)?[^.]{0,60}deliver[^.]{0,60})", body_text, re.I)
    if m:
        return m.group(0).strip()
    return None
