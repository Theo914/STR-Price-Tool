"""Regressions captured from the 14 Oct 2026 live run for TravelStaytion
1069699 (Heritage Haven, Woodbridge WA, licence STRA6056KHBVDM4I)."""
import json
from datetime import date
from pathlib import Path

import httpx

from parity.adapters.http.airbnb import AirbnbAdapter, _structural_from_meta
from parity.core.matching import (
    common_registration_ids,
    find_licence_numbers,
    normalise_registration,
    registration_tokens,
)
from parity.core.models import Outcome, PropertyRef
from parity.discovery.providers import BookingSearchProvider

FIXTURES = Path(__file__).parent / "fixtures"


def test_western_australia_stra_number_is_a_registration_id():
    assert "STRA6056KHBVDM4I" in find_licence_numbers("STRA NUMBER: STRA6056KHBVDM4I")
    assert "PID-STRA-12345" in find_licence_numbers("Registration PID-STRA-12345 shown")


def test_registration_comparison_ignores_case_and_separators():
    assert normalise_registration("hutb-012345") == "HUTB012345"
    assert common_registration_ids(["HUTB-012345"], ["HUTB 012345"]) == {"HUTB-012345"}
    # Generic or short values never prove identity.
    assert common_registration_ids(["N/A"], ["N/A"]) == set()
    assert common_registration_ids(["12"], ["12"]) == set()


def test_registration_tokens_find_unknown_formats_and_spaced_ids():
    tokens = registration_tokens("Licence: ABC 123456 · ref XY9-88-77Z")
    assert "ABC123456" in tokens
    assert "XY98877Z" in tokens
    assert common_registration_ids(["XY9 88 77Z"], [], tokens) == {"XY9 88 77Z"}


def test_registration_tokens_scan_whole_large_page():
    filler = " ".join(f"word{i}" for i in range(80000))
    assert "STRA6056KHBVDM4I" in registration_tokens(filler + " STRA6056KHBVDM4I")


def test_airbnb_public_page_exposes_licence_and_meta_structure():
    html = (FIXTURES / "airbnb_room_registration_details_stra.html").read_text(encoding="utf-8")
    ref = AirbnbAdapter._identify_from_public_html(
        html, "1766481349907244606", "https://www.airbnb.com/rooms/1766481349907244606"
    )
    assert ref is not None
    assert "STRA6056KHBVDM4I" in ref.registration_ids
    assert (ref.title, ref.bedrooms, ref.bathrooms, ref.capacity) == (
        "Heritage Haven - A Sepal Stay", 1, 1.5, 2,
    )
    assert len(ref.gallery) == 2
    assert common_registration_ids(
        ["STRA6056KHBVDM4I"], ref.registration_ids, ref.extra["registration_tokens"]
    ) == {"STRA6056KHBVDM4I"}


def test_airbnb_legacy_gallery_urls_without_hosting_marker_are_accepted():
    html = (
        '<html><head><meta property="og:title" content="Home in Midland · 3 bedrooms · 2 baths"/>'
        '<meta property="og:description" content="Modern full home"/></head><body>'
        "https://a0.muscache.com/im/pictures/76dcaebd-a11c-4f1b-acab-5706960947ff.jpg "
        "https://a0.muscache.com/im/pictures/fd46fd81-ab91-4ac5-ba18-1a8d36070f53.jpg "
        "https://a0.muscache.com/im/pictures/user/User-1/original/aa.jpg</body></html>"
    )
    ref = AirbnbAdapter._identify_from_public_html(html, "18009857", "https://www.airbnb.com/rooms/18009857")
    assert ref is not None
    assert len(ref.gallery) == 2  # the user avatar is excluded
    assert ref.bedrooms == 3 and ref.title == "Modern full home"


def test_meta_parser_ignores_missing_fields():
    assert _structural_from_meta("<html></html>") == {}


def test_airbnb_all_in_total_display_prices_from_price_after_discount():
    captured = json.loads(
        (FIXTURES / "airbnb_stays_pdp_sections_all_in_total_aud.json").read_text(encoding="utf-8")
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=captured)))
    adapter = AirbnbAdapter(client=client)
    ref = PropertyRef(
        site="airbnb",
        raw_id="1766481349907244606",
        url="https://www.airbnb.com/rooms/1766481349907244606",
        extra={"quote_currency": "AUD"},
    )
    result = adapter.get_price(ref, date(2027, 2, 15), date(2027, 2, 16), 2)
    assert result.outcome == Outcome.PRICED, result.notes
    assert result.comparable_total == 594.08
    assert result.reconciliation_difference == 0.0


def test_booking_unresolved_search_index_page_is_not_a_candidate():
    refs = BookingSearchProvider._snapshot_refs({
        "candidates": [
            {"url": "https://www.booking.com/hotel/index.en-gb.html", "title": "Hotels"},
            {"url": "https://www.booking.com/hotel/au/villa-woody.en-gb.html", "title": "Villa Woody"},
        ]
    })
    assert [ref.url for ref in refs] == ["https://www.booking.com/hotel/au/villa-woody.en-gb.html"]


def _price_capture(name, currency="GBP"):
    captured = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    seen = {}

    def handler(request):
        seen["country"] = request.headers.get("x-airbnb-country")
        return httpx.Response(200, json=captured)

    adapter = AirbnbAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    ref = PropertyRef(
        site="airbnb", raw_id="34141360", url="https://www.airbnb.com/rooms/34141360",
        extra={"quote_currency": currency},
    )
    return adapter.get_price(ref, date(2026, 11, 30), date(2026, 12, 3), 2), seen


def test_airbnb_guest_total_includes_taxes_like_the_guest_sees():
    """Operator screenshot, 30 Nov-3 Dec 2026: GBP 1,087.28 = 3 x 428.50
    (1,285.49) - early booking discount 241.05 + taxes 42.84."""
    result, seen = _price_capture("airbnb_stays_pdp_sections_34141360_gbp_with_taxes.json")
    assert seen["country"] == "GB"
    assert result.outcome == Outcome.PRICED, result.notes
    assert result.headline_total == 1087.28
    assert [(item.category, item.amount) for item in result.line_items] == [
        ("accommodation", 1285.49), ("discount", -241.05), ("tax", 42.84),
    ]


def test_airbnb_total_before_taxes_is_never_compared():
    result, _ = _price_capture("airbnb_stays_pdp_sections_34141360_gbp_before_taxes.json")
    assert result.outcome == Outcome.INCOMPLETE_EVIDENCE
    assert "before taxes" in result.notes


def test_airbnb_bare_dollar_confirms_only_a_requested_dollar_currency():
    # Live regression (707947, USD): Airbnb showed "$8,123" with no code.
    from parity.adapters.http.airbnb import _observed_currency

    price = {"primaryLine": {"price": "$8,123.45", "qualifier": "for 3 nights"}}
    assert _observed_currency(price, "USD") == "USD"
    assert _observed_currency(price, "AUD") == "AUD"
    assert _observed_currency(price, "GBP") is None
    assert _observed_currency(price) is None
    assert _observed_currency({"price": "US$8,123"}, "AUD") == "USD"
    assert _observed_currency({"price": "$8,123 AUD"}, "USD") == "AUD"
    assert _observed_currency({"price": "MX$8,123"}, "USD") is None
    assert _observed_currency({"price": "$8,123 + £20"}, "USD") is None


def test_airbnb_page_timestamp_is_not_read_as_a_french_licence():
    # Live regression (814642 candidate): PageGenerateAtToken looked like a licence.
    from parity.adapters.http.airbnb import _identity_text
    from parity.core.matching import find_licence_numbers

    html = (
        '["PageGenerateAtToken",1792064261429],{"createdAt":1792064261429}'
        '<div>Registration number: 7510609703582</div>'
    )
    assert list(find_licence_numbers(_identity_text(html))) == ["7510609703582"]


def test_airbnb_hydrated_page_reads_counts_and_name_without_og_tags():
    # Live (1529026680963371371): the hydrated variant has a generic og:title.
    from parity.adapters.http.airbnb import _structural_from_meta

    html = (
        '<meta property="og:title" content="Airbnb: Vacation Rentals">'
        '<script type="application/ld+json">{"@context":"https://schema.org",'
        '"@type":"VacationRental","identifier":"abc=","name":"Superhost Villa Monica",'
        '"description":"x"}</script>'
        '"sharingConfig":{"__typename":"PdpSharingConfig","title":"Villa in Marrakesh '
        '\\u00b7 4 bedrooms \\u00b7 5 beds \\u00b7 6.5 baths","propertyType":"Entire villa",'
        '"personCapacity":8,'
    )
    out = _structural_from_meta(html)
    assert out["title"] == "Superhost Villa Monica"
    assert out["bedrooms"] == 4
    assert out["bathrooms"] == 6.5
