"""Shared, persistent, logged-out browser session for the browser-rendered
adapters (Vrbo/Expedia, Booking.com, Guesty). One session is reused across
all of them in a single command, so we launch the browser once, not three
times. Windows uses an installed, signed Edge (or Chrome fallback) because
enterprise code-integrity policy may reject Patchright's bundled Chromium.
"""
from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Optional

from patchright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from parity.browser_control import BrowserSnapshotStore
from parity.http_control import OfflineFixtureError, RequestClass, RequestGovernor

PROFILE_ROOT = Path(__file__).resolve().parent.parent.parent.parent / ".browser-profiles"

# Text that shows up in cookie-consent banners across these sites. Declining
# non-essential cookies is an explicit constraint (see the system prompt's
# privacy rules and Theo's own instruction), so every page load tries this
# before reading anything.
_DECLINE_BUTTON_TEXTS = [
    "Decline", "Reject all", "Reject non-essential", "Only necessary",
    "Necessary cookies only", "Refuse", "Rifiuta",
]


def preferred_browser_channels(system_name: str | None = None) -> tuple[str | None, ...]:
    """Return safe browser launch choices for the current operating system.

    The channel names ask Patchright to use signed, system-installed browsers.
    On Windows we deliberately do not fall back to bundled Chromium: if Windows
    code integrity rejected that executable once, retrying it would only hide
    the real launch failure and cannot produce useful OTA evidence.
    """
    current_system = system_name or platform.system()
    if current_system == "Windows":
        return ("msedge", "chrome")
    return (None,)


class BrowserSession:
    """Wraps one persistent Patchright context. Use as a context manager:

        with BrowserSession() as session:
            page = session.new_page()
            ...
    """

    def __init__(
        self,
        headless: bool = True,
        profile_dir: Path | None = None,
        *,
        snapshot_store: BrowserSnapshotStore | None = None,
        governor: RequestGovernor | None = None,
        offline: bool = False,
        record_fixtures: bool = False,
    ):
        if offline and record_fixtures:
            raise ValueError("--offline and --record-fixtures cannot be used together")
        self._headless = headless
        self._profile_dir = profile_dir
        self.snapshot_store = snapshot_store
        self.governor = governor
        self.offline = offline
        self.record_fixtures = record_fixtures
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self.browser_channel: str | None = None

    @property
    def headless(self) -> bool:
        return self._headless

    def _channel_profile_dir(self, channel: str | None) -> Path:
        if self._profile_dir is not None:
            return self._profile_dir
        return PROFILE_ROOT / (channel or "chromium")

    def __enter__(self) -> "BrowserSession":
        if self.offline:
            return self
        self._playwright = sync_playwright().start()
        launch_errors: list[tuple[str, Exception]] = []
        for channel in preferred_browser_channels():
            label = channel or "chromium"
            kwargs = {
                "user_data_dir": str(self._channel_profile_dir(channel)),
                "headless": self._headless,
                "viewport": {"width": 1280, "height": 900},
                "locale": "en-GB",
            }
            if channel is not None:
                kwargs["channel"] = channel
            elif os.environ.get("PARITY_BROWSER_EXECUTABLE"):
                # Non-Windows development hosts with a preinstalled Chromium.
                kwargs["executable_path"] = os.environ["PARITY_BROWSER_EXECUTABLE"]
            try:
                self._context = self._playwright.chromium.launch_persistent_context(**kwargs)
                self.browser_channel = label
                return self
            except Exception as exc:
                launch_errors.append((label, exc))

        self._playwright.stop()
        self._playwright = None
        attempted = ", ".join(label for label, _ in launch_errors)
        last_error = launch_errors[-1][1]
        raise RuntimeError(
            f"Could not launch a supported browser ({attempted}). On Windows, "
            "install or update Microsoft Edge; do not disable Windows security "
            f"controls. Last launch error: {last_error}"
        ) from last_error

    def __exit__(self, *exc_info) -> None:
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()

    def new_page(self) -> Page:
        if self.offline:
            raise OfflineFixtureError(
                "OFFLINE: live browser page access is disabled; a structured snapshot is required"
            )
        if self._context is None:
            raise RuntimeError("BrowserSession must be used as a context manager (with BrowserSession() as s:)")
        return self._context.new_page()

    def load_snapshot(self, platform: str, kind: str, request_url: str) -> dict:
        if self.snapshot_store is None:
            raise OfflineFixtureError("OFFLINE: browser snapshot store is not configured")
        payload = self.snapshot_store.load(platform, kind, request_url)
        if self.governor is not None:
            self.governor.record_fixture_hit(RequestClass(platform, kind))
        return payload

    def save_snapshot(self, platform: str, kind: str, request_url: str, payload: dict) -> None:
        if self.record_fixtures and self.snapshot_store is not None:
            self.snapshot_store.save(platform, kind, request_url, payload)

    def before_navigation(self, platform: str, kind: str) -> None:
        if self.governor is not None:
            self.governor.before_network_request(RequestClass(platform, kind))

    def record_navigation(
        self,
        platform: str,
        kind: str,
        latency_ms: float,
        *,
        outcome: str = "browser_ok",
        challenge: bool = False,
    ) -> None:
        if self.governor is not None:
            self.governor.record_browser_navigation(
                RequestClass(platform, kind),
                latency_ms,
                outcome=outcome,
                challenge=challenge,
            )

    # Ground truth (22 Sep): Vrbo/Expedia Group served a real "Bot or Not?"
    # challenge page after repeated automated requests to the same listing
    # in one session -- distinct from Booking.com's own "unusual traffic"
    # wording. Shared here so every browser adapter can check for it with
    # one call instead of re-implementing its own marker list.
    _BOT_CHALLENGE_MARKERS = (
        "bot or not", "verify you're human", "verify you are human",
        "unusual traffic", "access denied", "press & hold",
    )

    @staticmethod
    def is_bot_challenge(text: str) -> bool:
        low = text.lower()
        return any(marker in low for marker in BrowserSession._BOT_CHALLENGE_MARKERS)

    @staticmethod
    def decline_cookies(page: Page, timeout_ms: int = 3000) -> bool:
        """Best-effort: click a decline/reject button if a cookie banner is
        showing. Returns True if something was clicked. Never raises --
        a banner not appearing (already handled by a persistent profile
        remembering the choice) is the normal case after the first run."""
        for text in _DECLINE_BUTTON_TEXTS:
            try:
                button = page.get_by_role("button", name=text, exact=False)
                if button.count() > 0:
                    button.first.click(timeout=timeout_ms)
                    return True
            except Exception:
                continue
        return False
