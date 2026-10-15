from __future__ import annotations

import json
from datetime import date

import pytest

from parity.adapters.browser.booking import (
    BookingAdapter,
    _build_identity_snapshot,
    _property_from_snapshot,
    canonical_booking_url,
)
from parity.browser_control import BrowserSnapshotStore
from parity.core.models import Outcome, PropertyRef


CANONICAL = "https://www.booking.com/hotel/es/villa-sa-planeta.en-gb.html"
IMAGE_1 = "https://cf.bstatic.com/xdata/images/hotel/max1024x768/101.jpg"
IMAGE_2 = "https://cf.bstatic.com/xdata/images/hotel/max1024x768/102.jpg"


def _snapshot() -> dict:
    structured = {
        "@context": "https://schema.org",
        "@type": "Villa",
        "name": "Villa Sa Planeta",
        "address": {
            "addressLocality": "Sant Josep de sa Talaia",
            "addressCountry": "Spain",
            "postalCode": "07830",
        },
        "geo": {"latitude": 38.9, "longitude": 1.3},
        "image": [IMAGE_1 + "?aid=tracker", IMAGE_2],
        "amenityFeature": [{"@type": "LocationFeatureSpecification", "name": "Pool"}],
    }
    return _build_identity_snapshot(
        canonical_url=CANONICAL,
        page_title="Villa Sa Planeta – Booking.com",
        headings=["Villa Sa Planeta"],
        body_text=(
            "Villa Sa Planeta. 3 bedrooms. 2 bathrooms. Sleeps 6. "
            "Registration number ETV-2071-E"
        ),
        html="<html></html>",
        json_ld_texts=[json.dumps(structured)],
        dom_image_values=[],
    )


def test_booking_url_is_canonicalised_before_capture():
    dirty = CANONICAL + "?aid=356980&sid=secret&label=tracking"
    assert canonical_booking_url(dirty) == CANONICAL


def test_booking_image_keeps_public_cdn_signature_but_drops_tracking():
    structured = {
        "@type": "Villa",
        "name": "Villa Sa Planeta",
        "image": [
            IMAGE_1 + "?k=public-signature&o=resize&aid=tracker",
            IMAGE_2,
        ],
    }
    snapshot = _build_identity_snapshot(
        canonical_url=CANONICAL,
        page_title="Villa Sa Planeta – Booking.com",
        headings=[],
        body_text="3 bedrooms. 2 bathrooms. Sleeps 6.",
        html="",
        json_ld_texts=[json.dumps(structured)],
        dom_image_values=[],
    )

    assert snapshot["gallery"][0] == IMAGE_1 + "?k=public-signature&o=resize"
    assert "aid=" not in json.dumps(snapshot)


def test_booking_snapshot_extracts_identity_without_tracking_data():
    snapshot = _snapshot()
    ref = _property_from_snapshot(snapshot)

    assert ref.raw_id == "villa-sa-planeta"
    assert ref.title == "Villa Sa Planeta"
    assert ref.registration_ids == ["ETV-2071-E"]
    assert ref.bedrooms == 3
    assert ref.bathrooms == 2
    assert ref.capacity == 6
    assert ref.city == "Sant Josep de sa Talaia"
    assert ref.country == "Spain"
    assert ref.gallery == [IMAGE_1, IMAGE_2]
    assert "sid=" not in json.dumps(snapshot)


def test_booking_identity_fails_closed_without_two_gallery_images():
    snapshot = _snapshot()
    snapshot["gallery"] = snapshot["gallery"][:1]

    with pytest.raises(ValueError, match="fewer than two"):
        _property_from_snapshot(snapshot)


def test_booking_adapter_replays_structured_snapshot_without_browser(tmp_path):
    store = BrowserSnapshotStore(tmp_path)
    store.save("booking", "page", CANONICAL, _snapshot())

    class OfflineSession:
        offline = True

        def load_snapshot(self, platform, kind, url):
            return store.load(platform, kind, url)

    ref = BookingAdapter(OfflineSession()).identify(CANONICAL + "?sid=not-saved")

    assert ref.title == "Villa Sa Planeta"
    assert ref.url == CANONICAL
    assert len(ref.gallery) == 2


def test_booking_live_pricing_stays_locked_without_explicit_capture_permission():
    class Session:
        offline = False

    adapter = BookingAdapter(Session())
    ref = PropertyRef(site="booking", raw_id="villa-sa-planeta", url=CANONICAL)
    result = adapter.get_price(ref, date(2027, 2, 1), date(2027, 2, 9), 2)

    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None
    assert "locked until explicitly authorised" in result.notes


def test_booking_identity_retries_server_errors_but_not_client_errors(monkeypatch):
    # Live regression (965991): Booking answered the property page with HTTP 500.
    import parity.adapters.browser.booking as booking_module

    monkeypatch.setattr(booking_module, "_IDENTIFY_RETRY_PAUSE_SECONDS", 0)
    calls = []
    outcomes = [booking_module._BookingServerError("HTTP 500"), "ref"]

    def fake_once(self, url):
        calls.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(BookingAdapter, "_identify_once", fake_once)
    assert BookingAdapter(object()).identify(CANONICAL) == "ref"
    assert len(calls) == 2

    def not_found(self, url):
        calls.append(url)
        raise ValueError("Booking.com property page returned HTTP 404")

    calls.clear()
    monkeypatch.setattr(BookingAdapter, "_identify_once", not_found)
    with pytest.raises(ValueError, match="HTTP 404"):
        BookingAdapter(object()).identify(CANONICAL)
    assert len(calls) == 1
