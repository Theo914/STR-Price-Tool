"""Fault injection: corrupt real captured responses and require fail-closed.

The rule under test is the tool's core promise: a damaged, drifted or
inconsistent response may produce an error, but never a PRICED result with a
wrong number.
"""
import copy
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from parity.adapters.http.airbnb import AirbnbAdapter
from parity.adapters.http.travelstaytion import TravelStaytionAdapter
from parity.core.models import Outcome, PropertyRef

FIXTURES = Path(__file__).parent / "fixtures"
AIRBNB_CAPTURE = json.loads(
    (FIXTURES / "airbnb_stays_pdp_sections_all_in_total_aud.json").read_text(encoding="utf-8")
)
CHECKIN, CHECKOUT = date(2027, 2, 15), date(2027, 2, 16)

# Live TravelStaytion quote for 1069699, 15-17 Feb 2027, 2 guests (14 Oct 2026).
TS_QUOTE = {
    "error": False,
    "data": {
        "available": True,
        "currency": "AUD",
        "price": {
            "total": 932, "totalDecimal": 931.57,
            "totalNoDiscount": 847, "totalNoDiscountDecimal": 846.88,
            "discount": 0, "discountDecimal": 0,
            "cleaning": 172, "cleaningDecimal": 171.82,
        },
        "discounts": [],
        "cancellationInfo": None,
        "stay": {"checkin": "2027-02-15", "checkout": "2027-02-17", "nights": 2, "guests": 2},
    },
}


def _airbnb_price(payload, *, status=200, raw=None, checkout=CHECKOUT, currency="AUD"):
    def handler(request):
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=payload)

    adapter = AirbnbAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    ref = PropertyRef(
        site="airbnb", raw_id="1766481349907244606",
        url="https://www.airbnb.com/rooms/1766481349907244606",
        extra={"quote_currency": currency},
    )
    return adapter.get_price(ref, CHECKIN, checkout, 2)


def _ts_price(payload, *, checkout=date(2027, 2, 17), guests=2, currency="AUD"):
    adapter = TravelStaytionAdapter(client=httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ))
    ref = PropertyRef(
        site="travelstaytion", raw_id="1069699",
        url="https://www.travelstaytion.com/stay/1069699",
        extra={"quote_currency": currency},
    )
    return adapter.get_price(ref, CHECKIN, checkout, guests)


def _replace_strings(node, old, new):
    if isinstance(node, dict):
        return {key: _replace_strings(value, old, new) for key, value in node.items()}
    if isinstance(node, list):
        return [_replace_strings(value, old, new) for value in node]
    if isinstance(node, str):
        return node.replace(old, new)
    return node


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_airbnb_baseline_capture_prices():
    result = _airbnb_price(AIRBNB_CAPTURE)
    assert result.outcome == Outcome.PRICED and result.comparable_total == 594.08


def _airbnb_currency_swapped():
    return _replace_strings(AIRBNB_CAPTURE, "AUD", "USD")


def _airbnb_unknown_fee():
    payload = copy.deepcopy(AIRBNB_CAPTURE)
    for node in _walk(payload):
        if node.get("__typename") == "DisplayPriceExplanationLineGroup":
            node["items"].append({
                "__typename": "DefaultExplanationLineItem",
                "description": "Mystery levy",
                "priceString": "$50.00 AUD",
            })
    return payload


def _airbnb_line_amount_changed():
    payload = copy.deepcopy(AIRBNB_CAPTURE)
    for node in _walk(payload):
        if str(node.get("description", "")).startswith("1 night x"):
            node["priceString"] = "$500.00 AUD"
    return payload


def _airbnb_no_exact_total_and_wrong_night_headline():
    payload = copy.deepcopy(AIRBNB_CAPTURE)
    for node in _walk(payload):
        if node.get("__typename") == "BasicPriceDetail":
            node["explanationData"]["priceDetails"] = node["explanationData"]["priceDetails"][:1]
        if node.get("__typename") == "QualifiedDisplayPriceLine":
            node["qualifier"] = "for 2 nights"
    return payload


def _airbnb_unavailable():
    payload = copy.deepcopy(AIRBNB_CAPTURE)
    for node in _walk(payload):
        if "structuredDisplayPrice" in node:
            node["structuredDisplayPrice"] = None
    payload["data"]["isAvailable"] = False
    return payload


@pytest.mark.parametrize(
    "mutate",
    [
        _airbnb_currency_swapped,
        _airbnb_unknown_fee,
        _airbnb_line_amount_changed,
        _airbnb_no_exact_total_and_wrong_night_headline,
        _airbnb_unavailable,
        lambda: {},
        lambda: {"data": None},
        lambda: {"errors": [{"message": "PersistedQueryNotFound"}]},
    ],
    ids=[
        "currency-swapped", "unknown-fee", "line-amount-changed",
        "no-exact-total", "unavailable", "empty-json", "null-data", "graphql-error",
    ],
)
def test_airbnb_corrupted_capture_is_never_priced(mutate):
    result = _airbnb_price(mutate())
    assert result.outcome != Outcome.PRICED, result.notes


@pytest.mark.parametrize(
    ("status", "raw"),
    [(200, b"<html>not json</html>"), (200, b'{"data": {"presentation"'), (429, b"{}"), (503, b"")],
    ids=["html", "truncated-json", "rate-limited", "server-error"],
)
def test_airbnb_bad_transport_responses_fail_closed(status, raw):
    result = _airbnb_price(None, status=status, raw=raw)
    assert result.outcome != Outcome.PRICED


def test_airbnb_requested_currency_must_match_display():
    result = _airbnb_price(AIRBNB_CAPTURE, currency="EUR")
    assert result.outcome != Outcome.PRICED


def test_ts_baseline_quote_prices():
    result = _ts_price(TS_QUOTE)
    assert result.outcome == Outcome.PRICED
    assert result.headline_total == 931.57
    assert result.currency == "AUD"


def _ts(mutator):
    payload = copy.deepcopy(TS_QUOTE)
    mutator(payload["data"])
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        _ts(lambda d: d.pop("currency")),
        _ts(lambda d: d.update(currency="EUR")),
        _ts(lambda d: d["stay"].update(guests=4)),
        _ts(lambda d: d["stay"].update(checkout="2027-02-18")),
        _ts(lambda d: d["price"].update(totalDecimal="931.57")),
        _ts(lambda d: d["price"].pop("totalNoDiscountDecimal")),
        _ts(lambda d: d["price"].update(totalDecimal=700.00)),
        _ts(lambda d: d.update(price=None, available=False, availabilityError="not available")),
        _ts(lambda d: d.update(price=[1, 2])),
        {"error": True, "message": "boom"},
    ],
    ids=[
        "currency-missing", "currency-different", "guests-changed", "dates-changed",
        "string-total", "no-base-total", "total-below-base", "unavailable",
        "price-not-object", "error-envelope",
    ],
)
def test_ts_corrupted_quote_is_never_priced(payload):
    result = _ts_price(payload)
    assert result.outcome != Outcome.PRICED, result.notes


def test_ts_unavailable_for_minimum_stay_is_classified():
    payload = _ts(lambda d: d.update(
        available=False, price=None, minStay=3,
        availabilityError="Minimum stay is 3 nights",
    ))
    result = _ts_price(payload)
    assert result.outcome == Outcome.MIN_STAY
    assert result.minimum_stay_nights == 3


# ---- identity attacks -------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from parity.core import orchestrator  # noqa: E402
from parity.core.matching import (  # noqa: E402
    PHash,
    common_registration_ids,
    conflicting_registration_ids,
    registration_tokens,
)
from parity.core.models import IdentityDecision  # noqa: E402


def test_licence_inside_a_longer_code_is_not_a_match():
    tokens = registration_tokens("Ref STRA6056KHBVDM4IX and STRA6056KHBVDM4 here")
    assert common_registration_ids(["STRA6056KHBVDM4I"], [], tokens) == set()


def test_short_numeric_licence_needs_licence_context_not_any_page_number():
    tokens = registration_tokens("Total 123456 · listing 123456")
    assert common_registration_ids(["123456"], [], tokens) == set()
    assert common_registration_ids(["123456"], ["123456"], tokens) == {"123456"}
    # Long numeric ids (e.g. Paris 13-digit) are specific enough on their own.
    assert common_registration_ids(["7510603329593"], [], registration_tokens("n° 7510603329593")) == {
        "7510603329593"
    }


def test_placeholder_licences_never_match():
    for value in ("Exempt", "N/A", "pending", "0", "12"):
        assert common_registration_ids([value], [value], {value.upper()}) == set()


def test_same_scheme_different_registration_is_a_conflict_but_noise_is_not():
    assert conflicting_registration_ids(["STRA6056KHBVDM4I"], ["STRA60568EX3H1MX"]) == {"STRA60568EX3H1MX"}
    # A 13-digit timestamp is not the same scheme as a Paris licence.
    assert conflicting_registration_ids(["7510603329593"], ["1791972836281"]) == set()
    assert conflicting_registration_ids(["STRA6056KHBVDM4I"], ["STRA6056KHBVDM4I"]) == set()


def test_sibling_unit_with_shared_photos_is_never_verified(monkeypatch):
    class Adapter:
        def identify(self, url):
            if "travelstaytion" in url:
                return PropertyRef(
                    site="travelstaytion", raw_id="1", url=url, bedrooms=1, capacity=2,
                    registration_ids=["STRA6056KHBVDM4I"], licence_number="STRA6056KHBVDM4I",
                    gallery=["https://img.example/s1.jpg", "https://img.example/s2.jpg"],
                )
            return PropertyRef(
                site="airbnb", raw_id="2", url=url, bedrooms=1, capacity=2,
                registration_ids=["STRA60568EX3H1MX"], licence_number="STRA60568EX3H1MX",
                gallery=["https://img.example/c1.jpg", "https://img.example/c2.jpg"],
            )

    shared = [PHash(full=0, crop=0), PHash(full=2**64 - 1, crop=2**64 - 1)]
    monkeypatch.setattr(
        orchestrator, "resolve",
        lambda url: SimpleNamespace(
            site="travelstaytion" if "travelstaytion" in url else "airbnb", needs_browser=False,
        ),
    )
    monkeypatch.setattr(orchestrator, "get_http_adapter", lambda site, client=None: Adapter())
    monkeypatch.setattr(orchestrator, "_throttle", lambda site: None)
    monkeypatch.setattr(orchestrator, "_fetch_gallery_hashes", lambda *a, **k: list(shared))
    monkeypatch.setattr(orchestrator, "keypoint_matching_available", lambda: False)

    _, matches = orchestrator.identify_and_match(
        client=object(),
        subject_url="https://www.travelstaytion.com/stay/1",
        candidate_urls=["https://www.airbnb.com/rooms/2"],
    )
    assert len(matches) == 1
    assert matches[0].decision == IdentityDecision.PROBABLE_REVIEW_REQUIRED
    assert "sibling" in matches[0].evidence[0].detail


def test_bathroom_only_difference_lets_photos_decide(monkeypatch):
    class Adapter:
        def identify(self, url):
            if "travelstaytion" in url:
                return PropertyRef(
                    site="travelstaytion", raw_id="1", url=url, bedrooms=5, capacity=10, bathrooms=4,
                    gallery=["https://img.example/s1.jpg", "https://img.example/s2.jpg"],
                )
            return PropertyRef(
                site="airbnb", raw_id="2", url=url, bedrooms=5, capacity=10, bathrooms=5.5,
                gallery=["https://img.example/c1.jpg", "https://img.example/c2.jpg"],
            )

    same = [PHash(full=0, crop=0), PHash(full=2**64 - 1, crop=2**64 - 1)]
    monkeypatch.setattr(
        orchestrator, "resolve",
        lambda url: SimpleNamespace(
            site="travelstaytion" if "travelstaytion" in url else "airbnb", needs_browser=False,
        ),
    )
    monkeypatch.setattr(orchestrator, "get_http_adapter", lambda site, client=None: Adapter())
    monkeypatch.setattr(orchestrator, "_throttle", lambda site: None)
    monkeypatch.setattr(orchestrator, "_fetch_gallery_hashes", lambda *a, **k: list(same))
    monkeypatch.setattr(orchestrator, "keypoint_matching_available", lambda: False)

    _, matches = orchestrator.identify_and_match(
        client=object(),
        subject_url="https://www.travelstaytion.com/stay/1",
        candidate_urls=["https://www.airbnb.com/rooms/2"],
    )
    assert matches[0].decision == IdentityDecision.VERIFIED
    assert any("bathrooms conflict" in item.detail for item in matches[0].evidence)


def test_map_search_box_covers_the_radius_east_west_at_high_latitude():
    from urllib.parse import parse_qs, urlsplit

    from parity.core.neighbours import _iter_search_results

    seen = []

    def handler(request):
        seen.append(parse_qs(urlsplit(str(request.url)).query))
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):  # the stub page has no results; only the request matters
        list(_iter_search_results(client, 51.5, -0.2, 2, max_pages=1, max_distance_km=3.0))
    params = seen[0]
    lat_span_km = (float(params["ne_lat"][0]) - float(params["sw_lat"][0])) * 111.0
    lng_span_km = (float(params["ne_lng"][0]) - float(params["sw_lng"][0])) * 111.0 * 0.6225
    assert abs(lat_span_km - 6.0) < 0.1
    assert abs(lng_span_km - 6.0) < 0.2


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Total €1.234", 1234.0), ("€ 12.500", 12500.0), ("€3.640,00", 3640.0),
     ("$1,200.50", 1200.5), ("€12.50", 12.5), ("549 €", 549.0)],
)
def test_money_text_handles_eu_thousands_grouping(text, expected):
    from parity.core.textparse import parse_money

    assert parse_money(text) == expected


def _noise_jpeg(seed: int) -> bytes:
    import io

    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(seed)
    pixels = (rng.random((48, 64)) * 255).astype("uint8")
    image = Image.fromarray(pixels).resize((640, 480), Image.NEAREST)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def test_card_photo_prescreen_lifts_the_true_listing_above_closer_lookalikes():
    from parity.discovery.engine import score_candidate
    from parity.discovery.providers import AirbnbMapProvider

    images = {f"https://img.example/{seed}.jpg": _noise_jpeg(seed) for seed in range(1, 7)}
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=images[str(request.url)])
    ))
    subject = PropertyRef(
        site="travelstaytion", raw_id="1", url="https://www.travelstaytion.com/stay/1",
        lat=31.6, lng=-8.0, gallery=[f"https://img.example/{seed}.jpg" for seed in (1, 2, 3)],
    )
    true_listing = PropertyRef(
        site="airbnb", raw_id="far", url="https://www.airbnb.com/rooms/far", lat=31.62, lng=-8.0,
        extra={"card_photos": ["https://img.example/2.jpg", "https://img.example/3.jpg"]},
    )
    lookalike = PropertyRef(
        site="airbnb", raw_id="near", url="https://www.airbnb.com/rooms/near", lat=31.6001, lng=-8.0,
        extra={"card_photos": ["https://img.example/5.jpg", "https://img.example/6.jpg"]},
    )
    provider = AirbnbMapProvider(client)
    summary = provider._photo_prescreen(subject, [true_listing, lookalike])

    assert true_listing.extra["card_photo_matches"] == 2
    assert lookalike.extra["card_photo_matches"] == 0
    assert "1 with matching card photos" in summary
    assert score_candidate(subject, true_listing)[0] > score_candidate(subject, lookalike)[0]
