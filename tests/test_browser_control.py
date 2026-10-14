from __future__ import annotations

import json

import pytest

from parity.browser_control import BrowserSnapshotStore
from parity.adapters.browser import browser_session
from parity.adapters.browser.browser_session import BrowserSession, preferred_browser_channels
from parity.http_control import FixtureCorruptError, FixtureStaleError


URL = "https://www.booking.com/hotel/es/villa-sa-planeta.en-gb.html"


def test_windows_prefers_signed_installed_browsers_without_bundled_fallback():
    assert preferred_browser_channels("Windows") == ("msedge", "chrome")


def test_non_windows_retains_bundled_chromium_path():
    assert preferred_browser_channels("Linux") == (None,)


def test_windows_launch_falls_back_from_edge_to_signed_chrome(monkeypatch, tmp_path):
    calls = []
    context = object()

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            calls.append(kwargs)
            if kwargs["channel"] == "msedge":
                raise RuntimeError("Edge launch failed")
            return context

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            raise AssertionError("successful session should stop in __exit__, not __enter__")

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    monkeypatch.setattr(browser_session.platform, "system", lambda: "Windows")
    monkeypatch.setattr(browser_session, "PROFILE_ROOT", tmp_path)
    monkeypatch.setattr(browser_session, "sync_playwright", lambda: FakeStarter())

    session = BrowserSession().__enter__()

    assert session.browser_channel == "chrome"
    assert session._context is context
    assert [call["channel"] for call in calls] == ["msedge", "chrome"]
    assert calls[0]["user_data_dir"] == str(tmp_path / "msedge")
    assert calls[1]["user_data_dir"] == str(tmp_path / "chrome")


def test_browser_snapshot_round_trip_retains_only_structured_payload(tmp_path):
    store = BrowserSnapshotStore(tmp_path, ttl_days=30)
    payload = {
        "snapshot_version": 1,
        "canonical_url": URL,
        "title": "Villa Sa Planeta",
        "gallery": ["https://cf.bstatic.com/xdata/images/hotel/max1024x768/1.jpg"],
    }

    store.save("booking", "page", URL, payload)

    assert store.load("booking", "page", URL) == payload
    manifest_path = next(tmp_path.glob("*.browser.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "structured public evidence only" in manifest["privacy"]
    assert "cookie" not in json.dumps(payload).casefold()


def test_browser_snapshot_rejects_checksum_failure(tmp_path):
    store = BrowserSnapshotStore(tmp_path)
    store.save("booking", "page", URL, {"snapshot_version": 1})
    body_path = next(tmp_path.glob("*.browser.body"))
    body_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(FixtureCorruptError, match="byte count|checksum"):
        store.load("booking", "page", URL)


def test_browser_snapshot_rejects_expired_fixture(tmp_path):
    store = BrowserSnapshotStore(tmp_path)
    store.save("booking", "page", URL, {"snapshot_version": 1})
    manifest_path = next(tmp_path.glob("*.browser.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["expires_at"] = "2000-01-01T00:00:00+00:00"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(FixtureStaleError, match="expired"):
        store.load("booking", "page", URL)
