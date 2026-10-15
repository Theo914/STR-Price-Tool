"""Regression tests for the 5 HTTP adapters, using synthetic responses that
mirror the exact shapes captured live in parity-price-verification-2026-09-22.md.

These do NOT hit real sites (this dev sandbox has no egress to them) — they
pin down that each adapter's parsing/normalization logic produces the
comparable totals we manually verified during research, so a future
refactor can't silently break the math. Live-site verification still has
to happen on a real run.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from parity.adapters.http.avantio import AvantioAdapter
from parity.adapters.http.airbnb import AirbnbAdapter, _STAYS_PDP_OPERATION_HASH
from parity.adapters.http.hostaway import HostawayAdapter
from parity.adapters.http.lodgify import LodgifyAdapter
from parity.adapters.http.travelstaytion import TravelStaytionAdapter
from parity.core.models import Outcome, PropertyRef


def _client_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_travelstaytion_comparable_total_matches_verified_figure():
    # Real figures from the doc: totalDecimal 3881.01, totalNoDiscountDecimal
    # 3839.01 (the 42 EUR gap is tourist tax, 2 guests x 3 EUR x 7 nights).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "available": True,
            "currency": "EUR",
            "price": {
                "totalDecimal": 3881.01,
                "totalNoDiscountDecimal": 3839.01,
                "cleaning": 420.0,
                "securityDeposit": 500.0,
            },
            "cancellationInfo": {"type": "strict_non_refundable"},
        })

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    ref = PropertyRef(site="travelstaytion", raw_id="210890", url="https://www.travelstaytion.com/stay/210890")
    result = adapter.get_price(ref, date(2026, 10, 4), date(2026, 10, 11), 2)

    assert result.outcome == Outcome.PRICED
    assert result.comparable_total == 3839.01
    assert result.breakdown.cleaning == 420.0
    assert result.breakdown.tourist_tax == 42.0
    assert result.refundable is False


def test_travelstaytion_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"available": False, "reason": "Sold out"})

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    ref = PropertyRef(site="travelstaytion", raw_id="210890", url="https://www.travelstaytion.com/stay/210890")
    result = adapter.get_price(ref, date(2026, 10, 4), date(2026, 10, 11), 2)

    assert result.outcome == Outcome.UNAVAILABLE
    assert result.comparable_total is None


def test_travelstaytion_unavailable_preserves_live_availability_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "error": False,
            "data": {
                "available": False,
                "availabilityError": "The property is not available, please try different dates",
                "price": None,
            },
        })

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    ref = PropertyRef(
        site="travelstaytion",
        raw_id="742313",
        url="https://www.travelstaytion.com/stay/742313",
    )
    result = adapter.get_price(ref, date(2027, 5, 10), date(2027, 5, 17), 2)

    assert result.outcome == Outcome.UNAVAILABLE
    assert result.notes == "The property is not available, please try different dates"


def test_travelstaytion_live_minimum_stay_error_is_not_sold_out():
    """Regression for Brighton diagnostic captured live on 12 Oct 2026."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "error": False,
            "data": {
                "available": False,
                "availabilityError": (
                    "There is a minimum stay limitation, please try different period"
                ),
                "currency": "EUR",
                "price": None,
                "stay": {
                    "checkin": "2027-05-10",
                    "checkout": "2027-05-13",
                    "guests": 2,
                    "nights": 3,
                },
            },
        })

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    ref = PropertyRef(
        site="travelstaytion",
        raw_id="1068655",
        url="https://www.travelstaytion.com/stay/1068655",
    )
    result = adapter.get_price(ref, date(2027, 5, 10), date(2027, 5, 13), 2)

    assert result.outcome == Outcome.MIN_STAY
    assert result.notes == "There is a minimum stay limitation, please try different period"
    assert result.minimum_stay_nights is None


def test_travelstaytion_calendar_exposes_and_caches_exact_date_minimum():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text="""
            <script>
            window.calendar = JSON.parse('{"checkins":[
              {"date":"2027-05-10","price":100,"minStay":14,"canCheckin":true},
              {"date":"2027-05-17","price":100,"minStay":null,"canCheckin":true},
              {"date":"2027-05-24","price":0,"minStay":null,"canCheckin":false}
            ],"defaultMinStay":3}');
            </script>
        """)

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    ref = PropertyRef(
        site="travelstaytion",
        raw_id="1068655",
        url="https://www.travelstaytion.com/stay/1068655",
    )

    assert adapter.minimum_stay_for_checkin(ref, date(2027, 5, 10)) == 14
    assert adapter.minimum_stay_for_checkin(ref, date(2027, 5, 17)) == 3
    assert adapter.minimum_stay_for_checkin(ref, date(2027, 5, 24)) is None
    assert len(requests) == 1


def test_travelstaytion_priced_quote_retains_explicit_minimum():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "error": False,
            "data": {
                "available": True,
                "minimumStay": 4,
                "currency": "EUR",
                "price": {
                    "totalDecimal": 400.0,
                    "totalNoDiscountDecimal": 400.0,
                },
                "cancellationInfo": {},
            },
        })

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    ref = PropertyRef(
        site="travelstaytion",
        raw_id="1",
        url="https://www.travelstaytion.com/stay/1",
    )
    result = adapter.get_price(ref, date(2027, 5, 10), date(2027, 5, 14), 2)

    assert result.outcome == Outcome.PRICED
    assert result.minimum_stay_nights == 4


def test_travelstaytion_sends_requested_currency_and_rejects_mismatch():
    seen_currency = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_currency
        seen_currency = request.url.params["currency"]
        return httpx.Response(200, json={
            "error": False,
            "data": {
                "available": True,
                "currency": "EUR",
                "price": {"totalNoDiscountDecimal": 100.0},
            },
        })

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    ref = PropertyRef(
        site="travelstaytion",
        raw_id="1068655",
        url="https://www.travelstaytion.com/stay/1068655",
        extra={"quote_currency": "GBP"},
    )
    result = adapter.get_price(ref, date(2027, 5, 10), date(2027, 5, 17), 2)

    assert seen_currency == "GBP"
    assert result.outcome == Outcome.INCOMPLETE_EVIDENCE
    assert result.notes == (
        "TravelStaytion quote currency mismatch: requested GBP, received EUR"
    )


def test_travelstaytion_real_envelope_shape():
    # The ACTUAL live response (captured 22 Sep against Casa Tatano,
    # TS 210890) wraps everything one level deeper than the doc's original
    # example: {"error": false, "data": {...}}. This caught a real bug on
    # first live run -- keep this fixture verbatim, don't "clean it up".
    real_response = {
        "error": False,
        "data": {
            "available": True, "availabilityError": None, "currency": "EUR",
            "price": {
                "total": 3882, "totalDecimal": 3881.01,
                "totalNoDiscount": 3840, "totalNoDiscountDecimal": 3839.01,
                "discount": 0, "discountDecimal": 0,
                "cleaning": 420, "cleaningDecimal": 420,
                "securityDeposit": 300, "securityDepositDecimal": 300,
                "perDay": 555, "perDayDecimal": 554.43,
                "perDayNoDiscount": 549, "perDayNoDiscountDecimal": 548.43,
            },
            "discounts": [], "tPayments": [], "tSharedPayments": [], "activeCoupon": None,
            "cancellationInfo": {
                "key": "strict", "title": "Strict",
                "policyDescription": "This reservation is non-refundable.<br>",
            },
            "stay": {"checkin": "2026-10-04", "checkout": "2026-10-11", "nights": 7, "guests": 2},
            "requestToBook": False,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=real_response)

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    ref = PropertyRef(site="travelstaytion", raw_id="210890", url="https://www.travelstaytion.com/stay/210890")
    result = adapter.get_price(ref, date(2026, 10, 4), date(2026, 10, 11), 2)

    assert result.outcome == Outcome.PRICED
    assert result.comparable_total == 3839.01
    assert result.breakdown.tourist_tax == 42.0
    assert result.refundable is False


def test_travelstaytion_live_long_stay_discount_reduces_comparable_total():
    live_response = {
        "error": False,
        "data": {
            "available": True,
            "availabilityError": None,
            "currency": "EUR",
            "price": {
                "total": 1481,
                "totalDecimal": 1480.58,
                "totalNoDiscount": 1553,
                "totalNoDiscountDecimal": 1552.18,
                "discount": 72,
                "discountDecimal": 71.61,
                "cleaning": 120,
                "cleaningDecimal": 120,
            },
            "discounts": [{
                "name": "Long stay offer",
                "slug": "long_stay",
                "amountDecimal": 71.61,
                "percentDecimal": 5,
            }],
            "cancellationInfo": {
                "key": "super_flexible",
                "title": "Super Flexible",
                "policyDescription": "Free cancellation until 14:00 on 3 May 2027.",
            },
            "stay": {
                "checkin": "2027-05-10",
                "checkout": "2027-05-17",
                "nights": 7,
                "guests": 2,
            },
            "requestToBook": False,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=live_response)

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    ref = PropertyRef(
        site="travelstaytion",
        raw_id="1065762",
        url="https://www.travelstaytion.com/stay/1065762",
    )
    result = adapter.get_price(ref, date(2027, 5, 10), date(2027, 5, 17), 2)

    assert result.outcome == Outcome.PRICED
    assert result.headline_total == 1480.58
    assert result.comparable_total == 1480.57
    assert result.breakdown.discount == 71.61
    assert result.breakdown.cleaning == 120
    assert result.breakdown.tourist_tax is None
    assert result.reconciliation_difference == -0.01
    assert [(item.category, item.amount) for item in result.line_items] == [
        ("accommodation", 1432.18),
        ("discount", -71.61),
        ("cleaning", 120),
    ]


def test_travelstaytion_identity_preserves_all_registration_and_structure_fields():
    property_payload = {
        "id": 1057462,
        "title": "Cloud Swing Room 1 by Wonderful Italy",
        "capacity": 2,
        "bedrooms": 1,
        "bathrooms": 1,
        "country": "Italy",
        "city": "Bari",
        "postcode": "70126",
        "lat": 41.1,
        "lng": 16.8,
        "licenseNumber": "BA07200691000048283",
        "nationalIdentificationCode": "IT072006B400093253",
        "propertyType": "Studio",
        "gallery": [{"url": "https://img.example/1.jpg"}],
        "amenities": [{"title": "Free Wi-Fi"}, {"name": "Elevator"}],
    }
    html = f"<script>window.property = {json.dumps(property_payload)};</script>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    ref = adapter.identify("https://www.travelstaytion.com/stay/1057462")

    assert ref.registration_ids == ["BA07200691000048283", "IT072006B400093253"]
    assert ref.licence_number == "BA07200691000048283"
    assert ref.bathrooms == 1
    assert ref.property_type == "Studio"
    assert ref.country == "Italy"
    assert ref.city == "Bari"
    assert ref.amenities == ["Free Wi-Fi", "Elevator"]


def test_travelstaytion_identity_fails_closed_when_payload_is_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Listing unavailable</body></html>")

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    with pytest.raises(ValueError, match="no embedded window.property"):
        adapter.identify("https://www.travelstaytion.com/stay/1057462")


def test_travelstaytion_identity_rejects_payload_for_different_listing():
    html = '<script>window.property = {"id": 999, "title": "Wrong listing"};</script>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    adapter = TravelStaytionAdapter(client=_client_with(handler))
    with pytest.raises(ValueError, match="did not match URL id"):
        adapter.identify("https://www.travelstaytion.com/stay/1057462")


def test_airbnb_identity_falls_back_to_public_hyperloop_html():
    room_id = "1099791623724969319"
    html = f"""
    <!doctype html><html data-is-hyperloop="true"><head><title>Villa Sa Planeta</title></head>
    <body>
      <h1>Villa Sa Planeta</h1>
      <div>Entire villa in Balearic Islands, Spain</div>
      <div>6 guests · 3 bedrooms · 4 beds · 2 bathrooms</div>
      <div>Ibiza – Regional registration number ETV-2071-E</div>
      <img src="https://a0.muscache.com/im/pictures/prohost-api/Hosting-{room_id}/original/one.jpeg?im_w=720">
      <img src="https:\\u002F\\u002Fa0.muscache.com\\u002Fim\\u002Fpictures\\u002Fprohost-api\\u002FHosting-{room_id}\\u002Foriginal\\u002Ftwo.jpeg?im_w=480">
      <img src="https://a0.muscache.com/im/pictures/another-listing.jpg">
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    adapter = AirbnbAdapter(client=_client_with(handler))
    ref = adapter.identify(f"https://www.airbnb.com.au/rooms/{room_id}")

    assert ref.title == "Villa Sa Planeta"
    assert ref.capacity == 6
    assert ref.bedrooms == 3
    assert ref.bathrooms == 2
    assert ref.property_type == "Villa"
    assert ref.registration_ids == ["ETV-2071-E"]
    assert len(ref.gallery) == 2
    assert all(f"Hosting-{room_id}" in item for item in ref.gallery)
    assert ref.extra["identity_source"] == "public_server_rendered_html"


def test_airbnb_identity_ignores_punctuation_before_bathroom_label():
    r"""A live Hoxton page contained ``. bathrooms`` before the real count.

    The old ``[\d.]+`` expression accepted the punctuation as a number and
    crashed while converting ``float('.')``.  Parsing must skip it and retain
    the later numeric bathroom value.
    """
    room_id = "1506734032830000497"
    html = f"""
    <!doctype html><html data-is-hyperloop="true"><head><title>Hoxton</title></head>
    <body>
      <h1>Family friendly and Modern 2BR Hoxton Shoreditch</h1>
      <div>Explore apartments. bathrooms and amenities appear below.</div>
      <div>4 guests · 2 bedrooms · 2 beds · 1 bathroom</div>
      <img src="https://a0.muscache.com/im/pictures/prohost-api/Hosting-{room_id}/original/one.jpeg?im_w=720">
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    adapter = AirbnbAdapter(client=_client_with(handler))
    ref = adapter.identify(f"https://www.airbnb.com.au/rooms/{room_id}")

    assert ref.capacity == 4
    assert ref.bedrooms == 2
    assert ref.bathrooms == 1


def _airbnb_checkout_html(items, displayed_total: str | None = "€760.00 total") -> str:
    structured = {
        "explanationData": {"priceDetails": [{"items": items}]},
    }
    if displayed_total is not None:
        structured["secondaryLine"] = {"price": displayed_total}
    state = {
        "presentation": {
            "stayProductDetailPage": {
                "sections": {"structuredDisplayPrice": structured},
            }
        }
    }
    return f'<script id="data-deferred-state-0">{json.dumps(state)}</script>'


def _airbnb_price(items, displayed_total: str | None = "€760.00 total"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_airbnb_checkout_html(items, displayed_total))

    adapter = AirbnbAdapter(client=_client_with(handler))
    ref = PropertyRef(
        site="airbnb",
        raw_id="123",
        url="https://www.airbnb.com/rooms/123",
        extra={"quote_currency": "EUR"},
    )
    return adapter.get_price(ref, date(2026, 10, 4), date(2026, 10, 11), 2)


def _airbnb_state_price(state, currency="EUR"):
    def handler(request: httpx.Request) -> httpx.Response:
        body = f'<script id="data-deferred-state-0">{json.dumps(state)}</script>'
        return httpx.Response(200, text=body)

    adapter = AirbnbAdapter(client=_client_with(handler))
    ref = PropertyRef(
        site="airbnb",
        raw_id="123",
        url="https://www.airbnb.com/rooms/123",
        extra={"quote_currency": currency},
    )
    return adapter.get_price(ref, date(2026, 10, 4), date(2026, 10, 11), 2)


def test_airbnb_checkout_itemization_reconciles_and_preserves_components():
    result = _airbnb_price([
        {"description": "€100 x 6 nights", "total": {"amount": 600}},
        {"description": "Weekly discount", "total": {"amount": 60}},
        {"description": "Cleaning fee", "total": {"amount": 80}},
        {"description": "Airbnb service fee", "total": {"amountMicros": 90_000_000}},
        {"description": "Occupancy taxes", "total": {"amount": 50}},
    ])

    assert result.outcome == Outcome.PRICED
    assert result.comparable_total == 710.0
    assert result.headline_total == 760.0
    assert result.breakdown.discount == 60.0
    assert result.breakdown.cleaning == 80.0
    assert result.breakdown.service_fee == 90.0
    assert result.breakdown.tourist_tax == 50.0
    assert result.reconciliation_difference == 0.0
    assert [item.amount for item in result.line_items] == [600.0, -60.0, 80.0, 90.0, 50.0]
    assert [item.currency for item in result.line_items] == ["EUR"] * 5
    assert all(item.included_in_displayed_total is True for item in result.line_items)


def test_airbnb_prefers_precise_checkout_total_over_rounded_aud_headline():
    accommodation = {
        "__typename": "DefaultExplanationLineItem",
        "description": "3 nights x $528.06 AUD",
        "priceString": "$1,584.17 AUD",
    }
    state = {
        "presentation": {
            "structuredDisplayPrice": {
                "primaryLine": {
                    "qualifier": "total",
                    "price": "$1,585 AUD",
                    "accessibilityLabel": "$1,585 AUD total",
                },
                "explanationData": {
                    "priceDetails": [{"items": [accommodation]}],
                },
            },
            "productItemDetail": {
                "__typename": "BasicPriceDetail",
                "explanationData": {
                    "priceDetails": [{"items": [
                        accommodation,
                        {
                            "__typename": "HighlightExplanationLineItem",
                            "description": "Total",
                            "priceString": "$1,584.17 AUD",
                        },
                    ]}],
                },
            },
        },
    }

    result = _airbnb_state_price(state, currency="AUD")

    assert result.outcome == Outcome.PRICED
    assert result.headline_total == 1584.17
    assert result.comparable_total == 1584.17
    assert result.reconciliation_difference == 0.0


def test_airbnb_checkout_rejects_a_displayed_currency_mismatch():
    result = _airbnb_price([
        {"description": "£200 x 3 nights", "total": {"amount": 600}},
    ], "£600.00 total")

    assert result.outcome == Outcome.INCOMPLETE_EVIDENCE
    assert result.headline_total is None
    assert "requested currency EUR, displayed GBP" in result.notes


def test_airbnb_live_hyperloop_api_capture_matches_manual_checkout():
    captured = json.loads(
        (Path(__file__).parent / "fixtures" / "airbnb_stays_pdp_sections_hoxton.json")
        .read_text(encoding="utf-8-sig")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.host == "www.airbnb.com.au"
        assert request.url.path.endswith(_STAYS_PDP_OPERATION_HASH)
        assert request.url.params["operationName"] == "StaysPdpSections"
        assert request.url.params["locale"] == "en-AU"
        assert request.url.params["currency"] == "EUR"
        assert request.headers["x-airbnb-api-key"] == "d306zoyjsyarp7ifhu67rjxn52tv0t20"
        payload = json.loads(request.content)
        variables = payload["variables"]
        assert variables["pdpSectionsRequest"]["checkIn"] == "2027-02-01"
        assert variables["pdpSectionsRequest"]["checkOut"] == "2027-02-09"
        assert variables["pdpSectionsRequest"]["sectionIds"] == [
            "BOOK_IT_CALENDAR_SHEET",
            "POLICIES_DEFAULT",
            "BOOK_IT_SIDEBAR",
            "BOOK_IT_NAV",
            "BOOK_IT_FLOATING_FOOTER",
            "CANCELLATION_POLICY_PICKER_MODAL",
        ]
        assert variables["guestCounts"] == {"numberOfAdults": 2}
        assert variables["includeGpBookItFragment"] is True
        assert variables["includeGpPoliciesFragment"] is True
        assert variables["includePdpMigrationOnlyOnBookItFragment"] is False
        assert variables["id"] == "U3RheUxpc3Rpbmc6MTUwNjczNDAzMjgzMDAwMDQ5Nw=="
        return httpx.Response(200, json=captured)

    adapter = AirbnbAdapter(client=_client_with(handler))
    ref = PropertyRef(
        site="airbnb",
        raw_id="1506734032830000497",
        url="https://www.airbnb.com.au/rooms/1506734032830000497",
        extra={"quote_currency": "EUR"},
    )
    result = adapter.get_price(ref, date(2027, 2, 1), date(2027, 2, 9), 2)

    assert result.outcome == Outcome.PRICED
    assert result.breakdown.nightly_total == 2091.27
    assert result.breakdown.discount == 59.68
    assert result.comparable_total == 2031.59
    assert result.headline_total == 2031.59
    assert result.reconciliation_difference == 0.0
    assert [(item.category, item.amount) for item in result.line_items] == [
        ("accommodation", 2091.27),
        ("discount", -59.68),
    ]


@pytest.mark.parametrize(
    ("items", "displayed_total", "message"),
    [
        (
            [
                {"description": "€100 x 6 nights", "total": {"amount": 600}},
                {"description": "Mystery charge", "total": {"amount": 160}},
            ],
            "€760.00 total",
            "unrecognized monetary line",
        ),
        (
            [{"description": "€100 x 6 nights", "total": {"amount": 600}}],
            None,
            "displayed total is missing",
        ),
        (
            [
                {"description": "€100 x 6 nights", "total": {"amount": 600}},
                {"description": "Cleaning fee", "total": {"amount": 80}},
            ],
            "€760.00 total",
            "does not reconcile",
        ),
    ],
)
def test_airbnb_checkout_fails_closed(items, displayed_total, message):
    result = _airbnb_price(items, displayed_total)

    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None
    assert message in result.notes


def test_airbnb_checkout_rejects_malformed_amount():
    result = _airbnb_price([
        {"description": "€100 x 6 nights", "total": {"amount": "not-a-number"}},
    ], "€600.00 total")

    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None
    assert "no explicit accommodation" in result.notes


def test_airbnb_checkout_reports_unavailable_without_guessing_a_price():
    result = _airbnb_state_price({"pdp": {"isAvailable": False}})

    assert result.outcome == Outcome.UNAVAILABLE
    assert result.comparable_total is None


def test_airbnb_checkout_reports_explicit_minimum_stay():
    result = _airbnb_state_price({
        "pdp": {"isAvailable": False, "minimumStay": 14},
    })

    assert result.outcome == Outcome.MIN_STAY
    assert result.comparable_total is None
    assert result.minimum_stay_nights == 14
    assert "14 nights" in result.notes


def test_lodgify_comparable_total_matches_villa_bracchi():
    # Real figures: 4550 (totalWithPromotions) + 600 (fees.total) = 5150,
    # 49 EUR local tax excluded.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "isAvailable": True,
            "currencyCode": "EUR",
            "rentalPrice": {"totalWithPromotions": 4550.0},
            "fees": {"total": 600.0, "details": [{"name": "Cleaning", "amount": 600.0}]},
            "localTaxes": 49.0,
            "totalPrice": {"total": 5199.0},
        })

    adapter = LodgifyAdapter(client=_client_with(handler))
    ref = PropertyRef(site="lodgify", raw_id="99", url="https://checkout.lodgify.com/villabracchi/99/reservation",
                       extra={"slug": "villabracchi", "property_id": "99"})
    result = adapter.get_price(ref, date(2026, 10, 4), date(2026, 10, 11), 2)

    assert result.outcome == Outcome.PRICED
    assert result.comparable_total == 5150.0
    assert result.headline_total == 5199.0


def test_hostaway_strips_damage_deposit():
    # Real figures: headline 3550 included a 750 damage deposit; true
    # comparable = 2450 accommodation + 350 cleaning = 2800.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/calendar"):
            return httpx.Response(200, json={"days": [
                {"date": "2026-10-17", "isAvailable": True, "minimumStay": 3},
            ]})
        return httpx.Response(200, json={
            "currency": "EUR",
            "totalPrice": 3550.0,
            "components": [
                {"type": "accommodation", "name": "Accommodation", "total": 2450.0},
                {"type": "fee", "name": "Cleaning fee", "total": 350.0},
                {"type": "damageDeposit", "name": "Damage deposit", "total": 750.0},
            ],
        })

    adapter = HostawayAdapter(client=_client_with(handler))
    ref = PropertyRef(site="hostaway", raw_id="55", url="https://book.example.com/listings/55",
                       extra={"slug": "colinadelmar"})
    result = adapter.get_price(ref, date(2026, 10, 17), date(2026, 10, 24), 2)

    assert result.outcome == Outcome.PRICED
    assert result.comparable_total == 2800.0
    assert result.headline_total == 3550.0
    assert result.breakdown.deposit == 750.0


def test_hostaway_min_stay():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"days": [
            {"date": "2026-10-17", "isAvailable": True, "minimumStay": 7},
        ]})

    adapter = HostawayAdapter(client=_client_with(handler))
    ref = PropertyRef(site="hostaway", raw_id="55", url="https://book.example.com/listings/55",
                       extra={"slug": "colinadelmar"})
    result = adapter.get_price(ref, date(2026, 10, 17), date(2026, 10, 19), 2)  # 2 nights, needs 7

    assert result.outcome == Outcome.MIN_STAY


def test_avantio_parses_total_and_non_refundable():
    html = """
    <html><body>
    <h1>Villa Test</h1>
    <div>Price breakdown</div>
    <div>Total: &euro;3.640,00</div>
    <div>Not included in the price (payable in cash on site): &euro;0</div>
    <div>Cancellation policy: non-refundable</div>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    adapter = AvantioAdapter(client=_client_with(handler))
    ref = PropertyRef(site="avantio", raw_id="https://example-host.com/book", url="https://example-host.com/book")
    result = adapter.get_price(ref, date(2026, 10, 4), date(2026, 10, 11), 2)

    assert result.outcome == Outcome.PRICED
    assert result.comparable_total == 3640.0
    assert result.cancellation_type == "non-refundable"
    assert result.refundable is False


def test_airbnb_identity_retries_thin_shell_before_settling(monkeypatch):
    # Live regression (726363 / 34307275): a shell page carried 2 of 61 photos.
    import parity.adapters.http.airbnb as airbnb_module

    monkeypatch.setattr(airbnb_module, "_IDENTIFY_RETRY_PAUSE_SECONDS", 0)
    room_id = "34307275"
    shell = (
        '<html data-is-hyperloop="true"><body><h1>Superhost Villa DKZ</h1>'
        f'<img src="https://a0.muscache.com/im/pictures/hosting/Hosting-{room_id}/original/a.jpeg">'
        "</body></html>"
    )
    photos = [
        {"baseUrl": f"https://a0.muscache.com/im/pictures/hosting/Hosting-{room_id}/original/{n}.jpeg"}
        for n in range(12)
    ]
    state = {"niobeClientData": [["x", {"data": {"title": "Villa", "lat": 31.6, "lng": -7.9,
                                                  "photos": photos}}]]}
    hydrated = f'<script id="data-deferred-state-0">{json.dumps(state)}</script>'
    pages = [shell, hydrated]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=pages.pop(0) if pages else hydrated)

    ref = AirbnbAdapter(client=_client_with(handler)).identify(
        f"https://www.airbnb.com/rooms/{room_id}"
    )
    assert len(ref.gallery) == 12


def test_airbnb_identity_sends_browser_navigation_headers():
    from parity.adapters.http.airbnb import AIRBNB_NAVIGATION_HEADERS

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, text='<html><body><h1>x</h1><div>Registration number: ETV-2071-E</div></body></html>')

    AirbnbAdapter(client=_client_with(handler)).identify("https://www.airbnb.com/rooms/34307275")
    assert seen["ua"] == AIRBNB_NAVIGATION_HEADERS["User-Agent"]
