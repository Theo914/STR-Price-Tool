"""Harmless browser-launch smoke test for the browser-rendered adapters.

This does NOT log into anything, NOT touch any OTA yet -- it launches a
profile stored locally (reused across runs, so it looks like a normal
returning visitor rather than a fresh session every time) and loads one
harmless page to confirm the whole chain works: install -> launch ->
navigate -> read content -> close cleanly.

Run: python tools/test_browser.py
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from parity.adapters.browser.browser_session import BrowserSession
except ImportError:
    print("patchright isn't installed. Run: pip install -r requirements.txt")
    sys.exit(1)


def main() -> None:
    print("Launching a signed installed browser on Windows (visible window)...")

    with BrowserSession(headless=False) as session:
        print(f"Browser channel: {session.browser_channel}")
        page = session.new_page()
        page.goto("https://example.com", wait_until="domcontentloaded", timeout=20000)
        title = page.title()
        text = page.inner_text("body")
        print(f"\nPage title: {title!r}")
        print(f"Body text (first 200 chars): {text[:200]!r}")

        webdriver_flag = page.evaluate("navigator.webdriver")
        print(f"navigator.webdriver: {webdriver_flag!r} (should be False or undefined, NOT True)")

        page.wait_for_timeout(2000)

    print("\nDone -- if you saw a browser window open, load example.com, and close")
    print("cleanly with no errors above, the signed-browser path works.")


if __name__ == "__main__":
    main()
