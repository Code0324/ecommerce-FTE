"""Standalone script to bootstrap a logged-in Amazon browser session.

This is a ONE-TIME, human-assisted bootstrap. It opens a real headed browser
with a persistent Chrome profile and waits for a human to complete login
(including any 2FA/OTP) manually.

Never stores passwords. The resulting session (cookies + local storage) is
saved to backend/credentials/amazon_buyer_session.json and loaded later by
the guest-checkout flow when Amazon surfaces a sign-in wall.

IMPORTANT SECURITY NOTES
------------------------
- This script calls input() -- it blocks the terminal until a human presses
  Enter. It is NOT safe to call this from a background job or cron.
- The saved session file is sensitive (it contains live cookies). It must
  live under backend/credentials/ and be gitignored (already covered by the
  repo .gitignore entry for backend/credentials/).
- If the session file goes stale (Amazon signs out, cookies expire), the
  checkout flow will detect the sign-in page and report an Error with a
  message telling the operator to re-run this script.

Usage:
    cd backend
    python -m jobs.bootstrap_amazon_session

What it does:
    1. Launches a headed Chrome browser via Patchright with persistent profile.
    2. Navigates to Amazon homepage and detects authentication status.
    3. If not authenticated: waits for manual login + 2FA in the browser.
    4. Verifies genuine authentication (auth cookie, currency, nav elements).
    5. Saves storage_state to backend/credentials/amazon_buyer_session.json.
    6. Closes the browser cleanly.
"""

from __future__ import annotations

import logging
import os
import sys
import textwrap
import traceback
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_FILE_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_FILE_DIR)
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)

if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv
from app.core.config import settings

load_dotenv(os.path.join(_REPO_ROOT, ".env"), override=True)


def _resolve_repo_path(value: str | None, relative_to_repo_root: str) -> str:
    if value is None:
        return os.path.normpath(os.path.join(_REPO_ROOT, relative_to_repo_root))

    raw = os.path.expandvars(os.path.expanduser(value.strip()))
    if os.path.isabs(raw):
        return os.path.normpath(raw)

    lowered = raw.lower()
    if lowered.startswith("backend/") or lowered.startswith("backend\\"):
        return os.path.normpath(os.path.join(_REPO_ROOT, raw))

    return os.path.normpath(os.path.join(_REPO_ROOT, raw))


SESSION_FILE = _resolve_repo_path(
    getattr(settings, "AMAZON_BUYER_SESSION_PATH", None),
    "backend/credentials/amazon_buyer_session.json",
)
CREDENTIALS_DIR = os.path.dirname(SESSION_FILE)
SCREENSHOTS_DIR = os.path.normpath(os.path.join(_REPO_ROOT, "backend/screenshots"))
AMAZON_PROFILE_DIR = os.path.normpath(os.path.join(_REPO_ROOT, "amazon-chrome-profile"))
HOMEPAGE_URL = "https://www.amazon.com/"


# ---------------------------------------------------------------------------
# Patchright dependency check
# ---------------------------------------------------------------------------


def _ensure_patchright() -> None:
    try:
        import patchright.sync_api  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "patchright is not installed. Run: pip install patchright --break-system-packages && "
            "python -m patchright install chrome"
        ) from exc


# ---------------------------------------------------------------------------
# Strong Session Verification
# ---------------------------------------------------------------------------


def _is_signin_form_visible(page: Any) -> bool:
    """Return True if an ACTUAL signin form is visible (not just HTML existing)."""
    try:
        email_field = page.locator(
            'input[id="ap_email"], input[id="ap_email_login"], input[name="email"]'
        ).first
        if email_field.count() > 0:
            if email_field.is_visible(timeout=2000):
                logger.info("✗ SIGNIN FORM DETECTED: Email field is visible")
                return True
    except Exception:
        pass

    try:
        content = page.content().lower()
        signin_phrases = [
            "sign in to your account",
            "enter your password",
            'id="ap_email"',
            'id="ap_password"',
        ]
        count = sum(1 for phrase in signin_phrases if phrase in content)
        if count >= 2:
            logger.warning("⚠ Signin indicators detected in page HTML (but form may not be visible)")
            return True
    except Exception:
        pass

    return False


def _has_authenticated_nav_elements(page: Any) -> bool:
    """Check for nav elements that ONLY appear when authenticated."""
    try:
        content = page.content()
        content_lower = content.lower()

        authenticated_indicators = [
            'hello,',
            'account & lists',
            'returns & orders',
            'id="nav-account-flyout-trigger"',
            'id="nav-link-orders"',
            'id="nav-link-your-account"',
        ]

        found = sum(1 for ind in authenticated_indicators if ind in content_lower)
        if found >= 1:
            logger.info("✓ Found authenticated nav elements in page HTML")
            return True

        logger.warning("✗ No authenticated nav elements found")
        return False
    except Exception as e:
        logger.error(f"Could not check nav elements: {e}")
        return False


def _verify_auth_cookie_present(context: Any) -> bool:
    """MANDATORY: Verify sst-main cookie is present and non-empty."""
    try:
        cookies = context.cookies()
        for cookie in cookies:
            if cookie.get("name") == "sst-main":
                value = cookie.get("value", "").strip()
                if value and len(value) > 10:
                    logger.info("✓ Auth cookie found: sst-main (valid)")
                    return True
                logger.warning(f"✗ Auth cookie sst-main is empty or too short: {len(value)} chars")
                return False

        logger.error("✗ CRITICAL: Auth cookie sst-main NOT FOUND")
        return False
    except Exception as e:
        logger.error(f"Error checking auth cookie: {e}")
        return False


def _verify_currency_is_usd(context: Any) -> bool:
    """Verify i18n-prefs is USD or absent (NOT PKR)."""
    try:
        cookies = context.cookies()
        for cookie in cookies:
            if cookie.get("name") == "i18n-prefs":
                value = cookie.get("value", "")
                logger.info(f"[DEBUG] i18n-prefs cookie: {value}")

                if "PKR" in value:
                    logger.error("✗ FAILED: Pakistan region detected!")
                    logger.error("Proxy may have disconnected during signin.")
                    return False

                if "USD" in value or "en_US" in value:
                    logger.info("✓ Currency is USD")
                    return True

                logger.warning(f"⚠ Unexpected currency value: {value}")
                return True

        logger.info("✓ No i18n-prefs cookie (defaults to US)")
        return True
    except Exception as e:
        logger.error(f"Error checking currency: {e}")
        return False


def _verify_logged_in(context: Any, page: Any) -> bool:
    """Verify genuine authentication with all checks."""
    logger.info("\n" + "=" * 72)
    logger.info("VERIFICATION: Checking all authentication criteria")
    logger.info("=" * 72)

    if _is_signin_form_visible(page):
        logger.error("✗ FAILED: Signin form still visible on page")
        return False

    if not _verify_auth_cookie_present(context):
        logger.error("✗ FAILED: Auth cookie missing (CRITICAL)")
        return False

    if not _verify_currency_is_usd(context):
        logger.error("✗ FAILED: Currency check failed")
        return False

    if not _has_authenticated_nav_elements(page):
        logger.error("✗ FAILED: No authenticated nav elements")
        return False

    logger.info("✓✓✓ VERIFICATION PASSED: All checks successful")
    return True


def _take_screenshot(page: Any, filename: str) -> None:
    """Capture a screenshot and save it to backend/screenshots/."""
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    filepath = os.path.join(SCREENSHOTS_DIR, filename)
    try:
        page.screenshot(path=filepath)
        logger.info(f"✓ Screenshot saved: {filepath}")
    except Exception as e:
        logger.warning(f"✗ Failed to take screenshot: {e}")


# ---------------------------------------------------------------------------
# Main bootstrap
# ---------------------------------------------------------------------------


def run_bootstrap() -> Path:
    """Run the interactive Amazon session bootstrap with persistent profile.

    Returns the path to the saved storage_state file.
    """
    _ensure_patchright()
    os.makedirs(CREDENTIALS_DIR, exist_ok=True)
    os.makedirs(AMAZON_PROFILE_DIR, exist_ok=True)

    from patchright.sync_api import sync_playwright

    context = None

    try:
        with sync_playwright() as pw:
            logger.info("\n" + "=" * 72)
            logger.info("LAUNCHING BROWSER WITH PERSISTENT PROFILE")
            logger.info("=" * 72)

            try:
                proxy_config = settings.proxy_config
                if proxy_config:
                    logger.info(f"✓ Proxy configured: {proxy_config.get('server')}")
                else:
                    logger.warning("⚠ No proxy configured (will use direct connection)")

                # Launch with persistent profile and proxy
                logger.info(f"Profile directory: {AMAZON_PROFILE_DIR}")
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=AMAZON_PROFILE_DIR,
                    headless=False,
                    channel="chrome",
                    args=["--disable-blink-features=AutomationControlled"],
                    proxy=proxy_config,
                )
                logger.info("✓ Browser launched with persistent profile")
            except Exception as e:
                logger.error(f"\n✗ FATAL: Failed to launch browser: {e}")
                raise

            try:
                # Get or create a page
                if context.pages:
                    page = context.pages[0]
                    logger.info("✓ Using existing page")
                else:
                    page = context.new_page()
                    logger.info("✓ Page created")

                # STEP 1: Navigate to Amazon homepage
                logger.info("\n[STEP 1] Navigating to Amazon homepage...")
                page.goto(HOMEPAGE_URL, timeout=60000, wait_until="domcontentloaded")
                logger.info("✓ Homepage loaded")

                # STEP 2: Wait for page to settle
                logger.info("[STEP 2] Waiting for page to settle...")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                    logger.info("✓ Page settled (network idle)")
                except Exception:
                    logger.warning("⚠ Page didn't reach network idle (continuing)")
                    page.wait_for_timeout(3000)

                # STEP 3: Check authentication status
                logger.info("[STEP 3] Checking authentication status...")
                is_authenticated = not _is_signin_form_visible(page)
                if is_authenticated:
                    logger.info("✓ Already authenticated (no signin form visible)")
                else:
                    logger.warning("⚠ Not authenticated - signin form is visible")

                logger.info(f"Current URL: {page.url}")
                _take_screenshot(page, "bootstrap_01_status.png")

            except Exception as e:
                logger.error(f"\n✗ FATAL: Failed to navigate to Amazon: {e}")
                raise

            # MAIN FLOW: Verify authentication or wait for manual login
            if not is_authenticated:
                logger.info("\n" + "=" * 72)
                logger.info("MANUAL LOGIN REQUIRED")
                logger.info("=" * 72)
                logger.info(
                    textwrap.dedent(
                        """
                        The browser is OPEN showing Amazon's sign-in page.

                        INSTRUCTIONS:
                        1. In the browser window, log in with your Amazon email + password
                        2. Complete 2FA/OTP if Amazon prompts
                        3. Wait for redirect to the Amazon homepage
                        4. When logged in, return to this terminal
                        5. Press Enter to verify login
                        """
                    ).strip()
                )

                _take_screenshot(page, "bootstrap_02_signin_required.png")

                # Wait for user to complete login
                try:
                    logger.info("\nPress Enter when logged in on the Amazon homepage: ")
                    input("")
                except EOFError:
                    logger.info("Non-interactive mode - waiting 300 seconds for login...")
                    for i in range(60):
                        page.wait_for_timeout(5000)
                        if "signin" not in page.url.lower():
                            logger.info("✓ Navigation away from signin detected")
                            break

                # Wait for page to settle after login
                logger.info("Waiting for page to settle after login...")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                    logger.info("✓ Page settled (network idle)")
                except Exception:
                    logger.warning("⚠ Page didn't reach network idle (continuing)")
                    page.wait_for_timeout(3000)

                _take_screenshot(page, "bootstrap_03_after_login.png")

            # VERIFICATION: Verify authentication
            try:
                if _verify_logged_in(context, page):
                    logger.info("\n" + "=" * 72)
                    logger.info("SAVING SESSION")
                    logger.info("=" * 72)
                    logger.info(f"Saving to: {SESSION_FILE}")

                    context.storage_state(path=SESSION_FILE)
                    logger.info("✓ Session saved successfully")
                    logger.info("Checkout automation can now use this session.")

                    _take_screenshot(page, "bootstrap_04_success_homepage.png")
                    return Path(SESSION_FILE)
                else:
                    logger.error("✗ Authentication verification failed")
                    logger.error("The browser session does not show a valid authenticated state.")
                    logger.error("Please verify you completed login and try again.")
                    sys.exit(1)

            except Exception as e:
                logger.error(f"\n✗ ERROR during verification: {e}")
                logger.error("Full traceback:")
                traceback.print_exc()
                sys.exit(1)

    except Exception as e:
        logger.error(f"\n✗ FATAL ERROR: {type(e).__name__}: {e}")
        logger.error("Full traceback:")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if context:
            try:
                context.close()
                logger.info("Browser closed")
            except Exception:
                pass


def main() -> None:
    print()
    print("=" * 72)
    print("AMAZON BUYER SESSION BOOTSTRAP")
    print("=" * 72)
    print()
    print(
        "This script opens a REAL browser window with a persistent Chrome profile "
        "and waits for YOU to log in to Amazon manually."
    )
    print("No password is stored anywhere. Only the resulting browser session")
    print("(cookies + local storage) is saved to disk.")
    print()
    print(f"Session will be saved to: {SESSION_FILE}")
    print(f"Chrome profile: {AMAZON_PROFILE_DIR}")
    print()
    print("=" * 72)
    print()

    path = run_bootstrap()
    print()
    print("Bootstrap complete.")
    print(f"Session saved to: {path}")
    print()
    print("Next step: re-run the checkout demo:")
    print("    cd backend")
    print("    python -m jobs.demo_guest_checkout_fulfillment")
    print()


if __name__ == "__main__":
    main()
