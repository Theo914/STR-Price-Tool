from __future__ import annotations

import json
from datetime import date

import pytest

from parity.adapters.browser.expedia import ExpediaAdapter
from parity.adapters.browser.expedia_group_identity import (
    build_identity_snapshot,
    canonical_expedia_url,
    canonical_vrbo_url,
    property_from_snapshot,
)
from parity.adapters.browser.vrbo_expedia import VrboExpediaAdapter
from parity.browser_control import BrowserSnapshotStore
from parity.core.models import Outcome, PropertyRef


VRBO_DIRTY = "https://www.vrbo.com/11180096ha?adults=2&tracking=secret"
VRBO_CANONICAL = "https://www.vrbo.com/11180096ha"
EXPEDIA_DIRTY = (
    "https://www.expedia.it/Palermo-Hotel.h89624086.Informazioni-Hotel"
    "?chkin=2026-10-08&sid=secret"
)
EXPEDIA_CANONICAL = "https://www.expedia.com/x.h89624086.Hotel-Information"
IMAGE_1 = "https://images.trvl-media.com/lodging/1000000/100.jpg?impolicy=resizecrop"
IMAGE_2 = "https://images.trvl-media.com/lodging/1000000/101.jpg"
LICENCE = "IT082053C2UUYPDYCD"


def _snapshot(platform: str, canonical_url: str) -> dict:
    structured = {
        "@context": "https://schema.org",
        "@type": "Apartment",
        "name": "Casetta Borsa by Wonderful Italy",
        "address": {
            "addressLocality": "Palermo",
            "addressCountry": "Italy",
            "postalCode": "90133",
        },
        "geo": {"latitude": 38.1162, "longitude": 13.3654},
        "image": [IMAGE_1, IMAGE_2],
    }
    return build_identity_snapshot(
        platform=platform,
        canonical_url=canonical_url,
        page_title="Casetta Borsa | Expedia Group",
        headings=["Casetta Borsa by Wonderful Italy"],
        body_text=f"1 bedroom. 1 bathroom. Sleeps 2. Registration number {LICENCE}",
        json_ld_texts=[json.dumps(structured)],
        dom_image_values=["https://tracking.example/pixel.gif"],
        meta_image_values=[],
    )


def test_vrbo_and_expedia_urls_are_canonicalised_without_tracking():
    assert canonical_vrbo_url(VRBO_DIRTY) == VRBO_CANONICAL
    assert canonical_vrbo_url(
        "https://www.vrbo.com/it-it/affitto-vacanze/p11180096"
    ) == VRBO_CANONICAL
    assert canonical_expedia_url(EXPEDIA_DIRTY) == EXPEDIA_CANONICAL


def test_expedia_group_snapshot_extracts_allow_listed_identity():
    snapshot = _snapshot("vrbo_expedia", VRBO_CANONICAL)
    ref = property_from_snapshot(snapshot, platform="vrbo_expedia")

    assert ref.raw_id == "11180096"
    assert ref.title == "Casetta Borsa by Wonderful Italy"
    assert ref.registration_ids == [LICENCE]
    assert ref.bedrooms == 1
    assert ref.bathrooms == 1
    assert ref.capacity == 2
    assert ref.city == "Palermo"
    assert ref.gallery == [IMAGE_1.split("?")[0], IMAGE_2]
    saved = json.dumps(snapshot)
    assert "tracking.example" not in saved
    assert "secret" not in saved


def test_expedia_group_identity_accepts_registration_without_gallery():
    snapshot = _snapshot("expedia", EXPEDIA_CANONICAL)
    snapshot["gallery"] = []
    ref = property_from_snapshot(snapshot, platform="expedia")
    assert ref.registration_ids == [LICENCE]


def test_expedia_group_identity_fails_closed_without_registration_or_gallery():
    snapshot = _snapshot("expedia", EXPEDIA_CANONICAL)
    snapshot["registration_ids"] = []
    snapshot["gallery"] = snapshot["gallery"][:1]
    with pytest.raises(ValueError, match="no registration identifier"):
        property_from_snapshot(snapshot, platform="expedia")


def test_saved_vrbo_snapshot_drops_non_image_vrbo_page_url_on_replay():
    snapshot = _snapshot("vrbo_expedia", VRBO_CANONICAL)
    snapshot["gallery"].append("https://www.vrbo.com/11180096ha")
    ref = property_from_snapshot(snapshot, platform="vrbo_expedia")
    assert ref.gallery == [IMAGE_1.split("?")[0], IMAGE_2]


def test_expedia_replay_keeps_only_images_scoped_to_the_property_id():
    snapshot = _snapshot("expedia", EXPEDIA_CANONICAL)
    property_image = (
        "https://images.trvl-media.com/lodging/90000000/89624086/property.jpg"
    )
    recommendation_image = (
        "https://images.trvl-media.com/lodging/90000000/12345678/recommendation.jpg"
    )
    snapshot["gallery"] = [property_image, recommendation_image]
    ref = property_from_snapshot(snapshot, platform="expedia")
    assert ref.gallery == [property_image]


@pytest.mark.parametrize(
    ("adapter_cls", "platform", "dirty_url", "canonical_url"),
    [
        (VrboExpediaAdapter, "vrbo_expedia", VRBO_DIRTY, VRBO_CANONICAL),
        (ExpediaAdapter, "expedia", EXPEDIA_DIRTY, EXPEDIA_CANONICAL),
    ],
)
def test_expedia_group_adapter_replays_snapshot_without_browser(
    tmp_path, adapter_cls, platform, dirty_url, canonical_url
):
    store = BrowserSnapshotStore(tmp_path)
    store.save(platform, "page", canonical_url, _snapshot(platform, canonical_url))

    class OfflineSession:
        offline = True

        def load_snapshot(self, platform_name, kind, url):
            return store.load(platform_name, kind, url)

    ref = adapter_cls(OfflineSession()).identify(dirty_url)
    assert ref.url == canonical_url
    assert ref.registration_ids == [LICENCE]


@pytest.mark.parametrize(
    ("adapter_cls", "platform"),
    [(VrboExpediaAdapter, "vrbo_expedia"), (ExpediaAdapter, "expedia")],
)
def test_expedia_group_pricing_is_locked(adapter_cls, platform):
    class Session:
        offline = False

    adapter = adapter_cls(Session())
    ref = PropertyRef(site=platform, raw_id="12345", url="https://example.test")
    result = adapter.get_price(ref, date(2027, 2, 1), date(2027, 2, 9), 2)
    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None
    assert "pricing is locked" in result.notes
