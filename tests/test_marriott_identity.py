from __future__ import annotations

import json
from datetime import date

import pytest

from parity.adapters.browser.marriott import MarriottAdapter
from parity.adapters.browser.marriott_identity import (
    MARRIOTT_IDENTITY_SNAPSHOT_VERSION,
    build_marriott_identity_snapshot,
    canonical_marriott_url,
    property_from_marriott_snapshot,
)
from parity.browser_control import BrowserSnapshotStore
from parity.core.models import Outcome, PropertyRef
from parity.http_control import RequestClass, RequestDeferred, RequestGovernor, classify_request
from parity.storage import EvidenceStore


DIRTY_URL = (
    "https://homes-and-villas.marriott.com/en/properties/"
    "78114516-captivating-idyllic-house?tracking=secret"
)
CANONICAL_URL = "https://homes-and-villas.marriott.com/en/properties/78114516"
LICENCE = "15063044EXT0379"
IMAGE_1 = "https://cache.marriott.com/is/image/marriotts7prod/house-1"
IMAGE_2 = "https://cache.marriott.com/is/image/marriotts7prod/house-2"


def _snapshot() -> dict:
    structured = {
        "@context": "https://schema.org",
        "@type": "VacationRental",
        "name": "Captivating Idyllic House with a Stunning Sea View",
        "address": {
            "addressLocality": "Massa Lubrense",
            "addressCountry": "Italy",
            "postalCode": "80061",
        },
        "geo": {"latitude": 40.61, "longitude": 14.34},
        "image": [IMAGE_1, IMAGE_2],
    }
    return build_marriott_identity_snapshot(
        canonical_url=CANONICAL_URL,
        page_title="Captivating Idyllic House | Homes & Villas by Marriott",
        headings=["Captivating Idyllic House with a Stunning Sea View"],
        body_text=(
            f"3 bedrooms 2 bathrooms Sleeps 6. Property Registration Number: {LICENCE}"
        ),
        json_ld_texts=[json.dumps(structured)],
        dom_image_values=["https://tracking.example/pixel.gif"],
        meta_image_values=[],
    )


def test_marriott_url_is_canonicalised_without_slug_or_tracking():
    assert canonical_marriott_url(DIRTY_URL) == CANONICAL_URL


def test_marriott_snapshot_extracts_allow_listed_identity():
    snapshot = _snapshot()
    ref = property_from_marriott_snapshot(snapshot)

    assert snapshot["snapshot_version"] == MARRIOTT_IDENTITY_SNAPSHOT_VERSION
    assert ref.raw_id == "78114516"
    assert ref.title == "Captivating Idyllic House with a Stunning Sea View"
    assert ref.registration_ids == [LICENCE]
    assert ref.bedrooms == 3
    assert ref.bathrooms == 2
    assert ref.capacity == 6
    assert ref.city == "Massa Lubrense"
    assert ref.gallery == [IMAGE_1, IMAGE_2]
    saved = json.dumps(snapshot)
    assert "tracking.example" not in saved
    assert "secret" not in saved


def test_marriott_identity_accepts_exact_registration_without_gallery():
    snapshot = _snapshot()
    snapshot["gallery"] = []
    ref = property_from_marriott_snapshot(snapshot)
    assert ref.registration_ids == [LICENCE]


def test_marriott_identity_fails_closed_without_registration_or_gallery():
    snapshot = _snapshot()
    snapshot["registration_ids"] = []
    snapshot["gallery"] = snapshot["gallery"][:1]
    with pytest.raises(ValueError, match="no registration identifier"):
        property_from_marriott_snapshot(snapshot)


def test_marriott_adapter_replays_snapshot_without_browser(tmp_path):
    store = BrowserSnapshotStore(tmp_path)
    store.save("marriott", "page", CANONICAL_URL, _snapshot())

    class OfflineSession:
        offline = True

        def load_snapshot(self, platform, kind, url):
            assert (platform, kind, url) == ("marriott", "page", CANONICAL_URL)
            return store.load(platform, kind, url)

    ref = MarriottAdapter(OfflineSession()).identify(DIRTY_URL)
    assert ref.url == CANONICAL_URL
    assert ref.registration_ids == [LICENCE]


def test_marriott_pricing_is_locked():
    class Session:
        offline = False

    ref = PropertyRef(site="marriott", raw_id="78114516", url=CANONICAL_URL)
    result = MarriottAdapter(Session()).get_price(
        ref, date(2027, 2, 1), date(2027, 2, 9), 2
    )
    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None
    assert "pricing is locked" in result.notes


def test_marriott_images_use_the_bounded_image_class():
    assert classify_request(IMAGE_1) == RequestClass("marriott", "image")


def test_marriott_has_one_page_navigation_budget(tmp_path):
    with EvidenceStore(tmp_path / "governor.db") as store:
        governor = RequestGovernor(store)
        request_class = RequestClass("marriott", "page")
        governor.before_network_request(request_class)
        with pytest.raises(RequestDeferred, match="budget exhausted"):
            governor.before_network_request(request_class)
