from __future__ import annotations

import json
from datetime import date

import pytest

from parity.adapters.browser.booking import (
    BOOKING_PRICING_SNAPSHOT_VERSION,
    _capture_booking_pricing_snapshot,
    booking_quote_url,
    extract_booking_source_pricing,
    parse_booking_pricing_snapshot,
    price_booking_source,
)
from parity.core.models import Outcome, PropertyRef


BOOKING_URL = "https://www.booking.com/hotel/es/villa-sa-planeta.en-gb.html"
CHECKIN = date(2027, 2, 1)
CHECKOUT = date(2027, 2, 9)
DIAGNOSTIC_CHECKIN = date(2026, 10, 19)
DIAGNOSTIC_CHECKOUT = date(2026, 10, 25)


@pytest.fixture
def ref() -> PropertyRef:
    return PropertyRef(site="booking", raw_id="villa-sa-planeta", url=BOOKING_URL)


def _snapshot(ref: PropertyRef) -> dict:
    return {
        "snapshot_version": BOOKING_PRICING_SNAPSHOT_VERSION,
        "canonical_url": BOOKING_URL,
        "quote_url": booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR"),
        "checkin": CHECKIN.isoformat(),
        "checkout": CHECKOUT.isoformat(),
        "guests": 2,
        "currency": "EUR",
        "outcome": "available",
        "displayed_total": 2150.0,
        "line_items": [
            {
                "label": "8 nights accommodation",
                "amount": 2100.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "Genius discount",
                "amount": -100.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "Cleaning fee",
                "amount": 150.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "City tax",
                "amount": 60.0,
                "currency": "EUR",
                "included_in_displayed_total": False,
            },
            {
                "label": "Refundable damage deposit",
                "amount": 500.0,
                "currency": "EUR",
                "included_in_displayed_total": False,
                "refundable": True,
            },
        ],
        "refundable": True,
        "cancellation_type": "Free cancellation before 1 January",
    }


def _parse(ref: PropertyRef, snapshot: dict):
    return parse_booking_pricing_snapshot(
        ref, snapshot, CHECKIN, CHECKOUT, 2, "EUR"
    )


def _room_table_diagnostic(ref: PropertyRef) -> dict:
    snapshot = _snapshot(ref)
    snapshot.update({
        "quote_url": booking_quote_url(
            ref, DIAGNOSTIC_CHECKIN, DIAGNOSTIC_CHECKOUT, 2, "EUR"
        ),
        "checkin": DIAGNOSTIC_CHECKIN.isoformat(),
        "checkout": DIAGNOSTIC_CHECKOUT.isoformat(),
        "outcome": "unknown",
        "displayed_total": None,
        "line_items": [],
        "diagnostics": {
            "capture_version": 2,
            "scoped_price_texts": [{
                "class_name": "bui-panel bui-u-hidden prco-price-area-popover",
                "testid": "",
                "text": (
                    "€ 392.03 × 6 nights € 2,352.16 Booking.com pays - € 150.96 "
                    "You’ll get a reduced rate when you pay online. Total € 2,201.20"
                ),
                "attributes": {},
            }],
            "availability_rows": [{
                "text": (
                    "Three-Bedroom Villa Includes taxes and charges "
                    "Non-refundable Pay online"
                )
            }],
            "tax_charge_popovers": [],
        },
    })
    return snapshot


def _parse_room_table_diagnostic(ref: PropertyRef, snapshot: dict):
    return parse_booking_pricing_snapshot(
        ref,
        snapshot,
        DIAGNOSTIC_CHECKIN,
        DIAGNOSTIC_CHECKOUT,
        2,
        "EUR",
    )


def test_priced_snapshot_requires_exact_itemised_reconciliation(ref):
    result = _parse(ref, _snapshot(ref))

    assert result.outcome == Outcome.PRICED
    assert result.comparable_total == 2150.0
    assert result.headline_total == 2150.0
    assert result.reconciliation_difference == 0.0
    assert result.breakdown.nightly_total == 2100.0
    assert result.breakdown.discount == 100.0
    assert result.breakdown.cleaning == 150.0
    assert result.breakdown.tourist_tax == 60.0
    assert result.breakdown.deposit == 500.0
    assert [item.currency for item in result.line_items] == ["EUR"] * 5
    assert [item.included_in_displayed_total for item in result.line_items] == [
        True, True, True, False, False
    ]


def test_included_air_conditioning_fee_is_a_mandatory_fee(ref):
    snapshot = _snapshot(ref)
    snapshot["displayed_total"] += 59.0
    snapshot["line_items"].append({
        "label": "Air conditioning fee",
        "amount": 59.0,
        "currency": "EUR",
        "included_in_displayed_total": True,
    })

    result = _parse(ref, snapshot)

    assert result.outcome == Outcome.PRICED
    assert result.headline_total == 2209.0
    assert result.comparable_total == 2209.0
    assert result.breakdown.other_mandatory_fees == 59.0
    assert result.reconciliation_difference == 0.0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["line_items"].__setitem__(0, {
            "label": "Mystery charge",
            "amount": 2100.0,
            "currency": "EUR",
            "included_in_displayed_total": True,
        }), "unknown monetary line"),
        (lambda value: value.__setitem__("displayed_total", 2149.0), "does not reconcile"),
        (lambda value: value["line_items"][0].pop("included_in_displayed_total"),
         "does not state whether"),
        (lambda value: value["line_items"][0].pop("currency"), "no explicit currency"),
        (lambda value: value["line_items"][1].__setitem__("amount", 100.0),
         "not signed negative"),
        (lambda value: value["line_items"][4].pop("refundable"),
         "not explicitly marked refundable"),
    ],
)
def test_ambiguous_or_inconsistent_price_evidence_is_rejected(ref, mutation, message):
    snapshot = _snapshot(ref)
    mutation(snapshot)

    result = _parse(ref, snapshot)

    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None
    assert message in result.notes


def test_unavailable_snapshot_returns_no_price(ref):
    snapshot = _snapshot(ref)
    snapshot.update({"outcome": "unavailable", "outcome_detail": "No rooms for these dates"})
    snapshot["displayed_total"] = None
    snapshot["line_items"] = []

    result = _parse(ref, snapshot)

    assert result.outcome == Outcome.UNAVAILABLE
    assert result.comparable_total is None
    assert result.notes == "No rooms for these dates"


def test_valid_minimum_stay_snapshot_returns_no_price(ref):
    snapshot = _snapshot(ref)
    snapshot.update({
        "outcome": "min_stay",
        "minimum_stay_nights": 90,
        "displayed_total": None,
        "line_items": [],
    })

    result = _parse(ref, snapshot)

    assert result.outcome == Outcome.MIN_STAY
    assert result.comparable_total is None
    assert result.minimum_stay_nights == 90
    assert "90 nights" in result.notes


def test_contradictory_minimum_stay_snapshot_is_rejected(ref):
    snapshot = _snapshot(ref)
    snapshot.update({"outcome": "min_stay", "minimum_stay_nights": 8})

    result = _parse(ref, snapshot)

    assert result.outcome == Outcome.ERROR
    assert "contradicts" in result.notes


def test_request_identity_mismatch_is_rejected(ref):
    snapshot = _snapshot(ref)
    snapshot["guests"] = 3

    result = _parse(ref, snapshot)

    assert result.outcome == Outcome.ERROR
    assert "request mismatch for guests" in result.notes


def test_exact_room_table_rate_is_preserved_but_not_called_comparable(ref):
    result = _parse_room_table_diagnostic(ref, _room_table_diagnostic(ref))

    assert result.outcome == Outcome.INCOMPLETE_EVIDENCE
    assert result.comparable_total is None
    assert result.headline_total == 2201.20
    assert result.breakdown.nightly_total == 2352.16
    assert result.breakdown.discount == 150.96
    assert result.reconciliation_difference == 0.0
    assert result.refundable is False
    assert result.cancellation_type == "Non-refundable"
    assert [(item.category, item.amount) for item in result.line_items] == [
        ("accommodation", 2352.16),
        ("discount", -150.96),
    ]
    assert "included taxes and charges are not separately quantified" in result.notes
    assert "comparable price withheld" in result.notes


def test_room_table_arithmetic_mismatch_remains_an_error_without_price(ref):
    snapshot = _room_table_diagnostic(ref)
    snapshot["diagnostics"]["scoped_price_texts"][0]["text"] = (
        snapshot["diagnostics"]["scoped_price_texts"][0]["text"].replace(
            "€ 2,201.20", "€ 2,200.00"
        )
    )

    result = _parse_room_table_diagnostic(ref, snapshot)

    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None
    assert result.headline_total is None
    assert "does not reconcile" in result.notes


def test_room_table_stay_length_mismatch_is_rejected(ref):
    snapshot = _room_table_diagnostic(ref)
    snapshot["diagnostics"]["scoped_price_texts"][0]["text"] = (
        snapshot["diagnostics"]["scoped_price_texts"][0]["text"].replace(
            "× 6 nights", "× 5 nights"
        )
    )

    result = _parse_room_table_diagnostic(ref, snapshot)

    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None
    assert "stay-length mismatch" in result.notes


def test_live_diagnostic_retains_bounded_availability_table_without_guessing(ref):
    row = {
        "row_index": 1,
        "testid": "",
        "class_name": "hprt-table-row",
        "text": "Three-Bedroom Villa € 2,352 Includes taxes and charges Non-refundable",
        "cells": [
            {
                "cell_index": 0,
                "tag": "td",
                "testid": "",
                "class_name": "hprt-table-cell-price",
                "text": "€ 2,352 Includes taxes and charges",
            }
        ],
        "selects": [{"name": "nr_rooms_1", "value": "0", "options": []}],
    }

    class Locator:
        def __init__(self, selector):
            self.selector = selector

        def evaluate_all(self, script):
            if "#hprt-table tr" in self.selector:
                return [row]
            if self.selector.startswith("#hprt-table,"):
                return [{
                    "testid": "",
                    "class_name": "hprt-table",
                    "text": row["text"],
                }]
            return [{
                "testid": "",
                "class_name": "bui-price-display__value",
                "text": "€ 2,352",
            }]

    class Page:
        def evaluate(self, script):
            return []

        def locator(self, selector):
            return Locator(selector)

    quote_url = booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR")
    snapshot = _capture_booking_pricing_snapshot(
        Page(),
        ref=ref,
        quote_url=quote_url,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "unknown"
    assert snapshot["displayed_total"] is None
    assert snapshot["line_items"] == []
    assert snapshot["diagnostics"]["capture_version"] == 5
    assert snapshot["diagnostics"]["availability_rows"] == [row]
    assert snapshot["diagnostics"]["availability_sections"][0]["text"].startswith(
        "Three-Bedroom Villa"
    )


def test_live_diagnostic_can_reveal_bounded_tax_popover_without_pricing(ref):
    class EmptyLocator:
        def evaluate_all(self, script):
            return []

    class TriggerLocator:
        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return self

        def hover(self, timeout):
            assert timeout == 1500

    class TooltipLocator:
        def evaluate_all(self, script):
            return [{
                "role": "tooltip",
                "testid": "tax-breakdown",
                "class_name": "bui-popover__content",
                "text": "Includes €120 VAT and €30 city tax",
            }]

    class Page:
        def evaluate(self, script):
            return []

        def locator(self, selector):
            if selector.startswith(".prd-taxes-and-fees-under-price"):
                return TriggerLocator()
            if selector.startswith('[role="tooltip"]'):
                return TooltipLocator()
            return EmptyLocator()

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 250

    quote_url = booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR")
    snapshot = _capture_booking_pricing_snapshot(
        Page(),
        ref=ref,
        quote_url=quote_url,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "unknown"
    assert snapshot["displayed_total"] is None
    assert snapshot["diagnostics"]["tax_charge_popovers"] == [{
        "trigger_index": 0,
        "popovers": [{
            "role": "tooltip",
            "testid": "tax-breakdown",
            "class_name": "bui-popover__content",
            "text": "Includes €120 VAT and €30 city tax",
        }],
    }]


def test_next_available_date_prices_mean_requested_dates_are_unavailable(ref):
    class Locator:
        def __init__(self, selector):
            self.selector = selector

        def evaluate_all(self, script):
            if self.selector.startswith('[data-testid*="price"]'):
                return [{
                    "testid": "",
                    "class_name": "bui-group__item c-next-available-dates__item__price",
                    "text": "From € 1,379",
                    "attributes": {},
                }]
            return []

        def count(self):
            return 0

    class Page:
        def evaluate(self, script):
            return []

        def locator(self, selector):
            return Locator(selector)

    quote_url = booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR")
    snapshot = _capture_booking_pricing_snapshot(
        Page(),
        ref=ref,
        quote_url=quote_url,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Villa info & price See availability",
    )
    result = _parse(ref, snapshot)

    assert snapshot["outcome"] == "unavailable"
    assert snapshot["displayed_total"] is None
    assert snapshot["line_items"] == []
    assert snapshot["diagnostics"]["capture_version"] == 5
    assert snapshot["diagnostics"]["availability_outcome_signal"] == (
        "next_available_dates_price_nodes"
    )
    assert result.outcome == Outcome.UNAVAILABLE
    assert result.comparable_total is None
    assert "offered alternative dates" in result.notes


def _villa_andrews_flexible_rate():
    return {
        "block_id": "1731604301_444769642_2_0_0",
        "cancellation_type": "free_cancellation",
        "total": 984.1,
        "total_value": "€ 984.10",
        "net_room": 760.0909000000001,
        "net_room_value": "€ 760.09",
        "included_sum": 224.0091,
        "included_charges": [
            {
                "label": "10 % VAT",
                "label_alt": "VAT",
                "type": "percentage__included",
                "raw_value": 76.00910000000009,
                "value": "€ 76.01",
            },
            {
                "label": "Cleaning fee",
                "label_alt": "Cleaning fee",
                "type": "per_stay__included",
                "raw_value": 120.00000000000009,
                "value": "€ 120",
            },
            {
                "label": "Towel charge",
                "label_alt": "Towel charge",
                "type": "per_stay__included",
                "raw_value": 0.0,
                "value": "€ 0",
            },
            {
                "label": "City tax",
                "label_alt": "City tax",
                "type": "per_person_per_night__included",
                "raw_value": 28.00000000000009,
                "value": "€ 28",
            },
        ],
        "excluded_charges_amount": 0.0,
        "excluded_count": 0,
        "conditional_count": 0,
        "nonmandatory_count": 0,
        "extra_addons_count": 0,
        "all_included": True,
    }


def _booking_source_for_rate(rate: dict) -> str:
    breakdown = {
        "b_total_price": [{
            "b_raw_value_user_currency": rate["total"],
            "b_value_user_currency": rate["total_value"],
        }],
        "b_net_room_price": [{
            "b_raw_value": rate["net_room"],
            "b_value": rate["net_room_value"],
        }],
        "b_sum_included_charges": [{
            "b_raw_value_user_currency": rate["included_sum"],
        }],
        "b_included_charges": [{
            "b_copy": item["label"],
            "b_copy_alt": item["label_alt"],
            "b_type": item["type"],
            "b_raw_value": item["raw_value"],
            "b_value": item["value"],
        } for item in rate["included_charges"]],
        "b_excluded_charges_amount": rate["excluded_charges_amount"],
        "b_excluded_charges": [],
        "b_conditional_charges": [],
        "b_nonmandatory_charges": [],
        "b_extra_addons": [],
        "b_charges_info_copy": {"b_is_all_included": 1},
    }
    return json.dumps({
        "b_block_id": rate["block_id"],
        "b_cancellation_type": rate["cancellation_type"],
        "b_price_breakdown_simplified": breakdown,
    }, separators=(",", ":"))


class _EmptyBookingLocator:
    def evaluate_all(self, script):
        return []

    def count(self):
        return 0


class _StructuredBookingPage:
    def __init__(self, rates, rate_displays=None):
        self.rates = rates
        self.rate_displays = rate_displays or []

    def evaluate(self, script):
        return {
            "legacy_keys": [],
            "rates": self.rates,
            "rate_displays": self.rate_displays,
        }

    def locator(self, selector):
        return _EmptyBookingLocator()


def test_embedded_villa_andrews_breakdown_prices_without_checkout_click(ref):
    quote_url = booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR")
    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([_villa_andrews_flexible_rate()]),
        ref=ref,
        quote_url=quote_url,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "available"
    assert snapshot["displayed_total"] == 984.1
    assert snapshot["refundable"] is True
    assert snapshot["cancellation_type"] == "Free cancellation"
    assert snapshot["evidence_source"] == "embedded_public_booking_price_breakdown"
    assert snapshot["diagnostics"]["capture_version"] == 5
    assert snapshot["diagnostics"]["selected_block_id"] == (
        "1731604301_444769642_2_0_0"
    )
    assert [item["label"] for item in snapshot["line_items"]] == [
        "8 nights accommodation",
        "10 % VAT",
        "Cleaning fee",
        "City tax",
    ]

    result = _parse(ref, snapshot)
    assert result.outcome == Outcome.PRICED
    assert result.headline_total == 984.1
    assert result.comparable_total == 880.09
    assert result.breakdown.nightly_total == 760.09
    assert result.breakdown.cleaning == 120.0
    assert result.breakdown.tourist_tax == 104.01
    assert result.reconciliation_difference == 0.0


def test_source_response_parser_extracts_villa_andrews_exact_components(ref):
    rate = _villa_andrews_flexible_rate()
    source = _booking_source_for_rate(rate)
    extracted = extract_booking_source_pricing(source)

    assert extracted["diagnostics"]["source_rate_count"] == 1
    assert extracted["diagnostics"]["source_failures"] == []
    assert extracted["rates"][0]["block_id"] == rate["block_id"]
    assert extracted["rates"][0]["net_room"] == rate["net_room"]
    assert extracted["rates"][0]["included_charges"][1]["label"] == "Cleaning fee"

    result = price_booking_source(
        source,
        ref=ref,
        checkin=date(2026, 10, 12),
        checkout=date(2026, 10, 19),
        guests=2,
        currency="EUR",
    )
    assert result.outcome == Outcome.PRICED
    assert result.headline_total == 984.1
    assert result.comparable_total == 880.09
    assert result.breakdown.nightly_total == 760.09
    assert result.breakdown.cleaning == 120.0
    assert result.breakdown.tourist_tax == 104.01


def test_navigation_source_rates_take_precedence_over_broken_rendered_script(ref):
    valid_rate = _villa_andrews_flexible_rate()
    broken_dom_rate = {**valid_rate, "total": None, "net_room": None}
    display = {
        "block_id": valid_rate["block_id"],
        "cancellation_type": "free_cancellation",
        "text": (
            "€ 140.59 × 7 nights € 984.10 Booking.com pays - € 60.81 "
            "Total € 923.29"
        ),
    }
    checkin = date(2026, 10, 12)
    checkout = date(2026, 10, 19)
    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([broken_dom_rate], [display]),
        ref=ref,
        quote_url=booking_quote_url(ref, checkin, checkout, 2, "EUR"),
        checkin=checkin,
        checkout=checkout,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
        source_text=_booking_source_for_rate(valid_rate),
    )

    result = parse_booking_pricing_snapshot(
        ref, snapshot, checkin, checkout, 2, "EUR"
    )
    assert snapshot["diagnostics"]["pricing_extraction_route"] == "navigation_response"
    assert snapshot["diagnostics"]["structured_rejection_reasons"] == []
    assert result.outcome == Outcome.PRICED
    assert result.headline_total == 923.29
    assert result.comparable_total == 819.28


def test_one_flexible_rate_is_selected_over_nonrefundable_rate(ref):
    nonrefundable = _villa_andrews_flexible_rate()
    nonrefundable.update({
        "block_id": "nonrefundable",
        "cancellation_type": "non_refundable",
        "total": 900.47,
        "net_room": 684.0636,
        "included_sum": 216.4064,
    })
    nonrefundable["included_charges"] = [
        {**item}
        for item in nonrefundable["included_charges"]
    ]
    nonrefundable["included_charges"][0]["raw_value"] = 68.4064
    quote_url = booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR")

    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([
            nonrefundable,
            _villa_andrews_flexible_rate(),
        ]),
        ref=ref,
        quote_url=quote_url,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "available"
    assert snapshot["displayed_total"] == 984.1
    assert snapshot["cancellation_type"] == "Free cancellation"
    assert snapshot["diagnostics"]["structured_rate_count"] == 2
    assert snapshot["diagnostics"]["structured_flexible_rate_count"] == 1


def test_multiple_flexible_structured_rates_remain_ambiguous(ref):
    first = _villa_andrews_flexible_rate()
    second = {**_villa_andrews_flexible_rate(), "block_id": "another-flexible-rate"}
    quote_url = booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR")

    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([first, second]),
        ref=ref,
        quote_url=quote_url,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )
    result = _parse(ref, snapshot)

    assert snapshot["outcome"] == "unknown"
    assert snapshot["displayed_total"] is None
    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None


def test_duplicate_embedded_rate_copies_are_deduplicated(ref):
    rate = _villa_andrews_flexible_rate()
    quote_url = booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR")

    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([rate, {**rate}]),
        ref=ref,
        quote_url=quote_url,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "available"
    assert snapshot["diagnostics"]["structured_rate_count"] == 1
    assert _parse(ref, snapshot).outcome == Outcome.PRICED


def test_zero_total_excluded_charge_marker_is_safe_when_rate_is_all_included(ref):
    rate = _villa_andrews_flexible_rate()
    rate.update({
        "block_id": "1711381401_442800794_6_0_0",
        "cancellation_type": "non_refundable",
        "total": 756.0,
        "total_value": "£756",
        "net_room": 756.0,
        "net_room_value": "£756",
        "included_sum": 0.0,
        "included_charges": [],
        "excluded_charges_amount": 0.0,
        "excluded_count": 1,
    })
    checkin = date(2027, 2, 15)
    checkout = date(2027, 2, 22)
    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([rate]),
        ref=ref,
        quote_url=booking_quote_url(ref, checkin, checkout, 2, "GBP"),
        checkin=checkin,
        checkout=checkout,
        guests=2,
        currency="GBP",
        body_text="Availability for your dates",
    )
    result = parse_booking_pricing_snapshot(
        ref, snapshot, checkin, checkout, 2, "GBP"
    )

    assert snapshot["outcome"] == "available"
    assert snapshot["displayed_total"] == 756.0
    assert snapshot["cancellation_type"] == "Non-refundable"
    assert snapshot["diagnostics"]["structured_rejection_reasons"] == []
    assert result.outcome == Outcome.PRICED
    assert result.headline_total == 756.0
    assert result.comparable_total == 756.0


def test_unique_rendered_rate_row_restores_missing_nonrefundable_policy(ref):
    rate = _villa_andrews_flexible_rate()
    rate.update({
        "block_id": "1711381401_442800794_6_0_0",
        "cancellation_type": "",
        "total": 756.0,
        "total_value": "£756",
        "net_room": 756.0,
        "net_room_value": "£756",
        "included_sum": 0.0,
        "included_charges": [],
        "excluded_charges_amount": 0.0,
        "excluded_count": 1,
    })
    availability_row = {
        "row_index": 1,
        "text": (
            "Apartment £1,400 £756 Includes taxes and charges 46% off "
            "Non-refundable • Pay the property before arrival Select rooms 0 1 (£756)"
        ),
        "cells": [],
        "selects": [{
            "name": "nr_rooms_1711381401_442800794_6_0_0",
            "value": "0",
            "options": [
                {"value": "0", "text": "0"},
                {"value": "1", "text": "1 (£756)"},
            ],
        }],
    }

    class Locator:
        def __init__(self, selector):
            self.selector = selector

        def evaluate_all(self, script):
            return [availability_row] if "#hprt-table tr" in self.selector else []

        def count(self):
            return 0

    class Page(_StructuredBookingPage):
        def locator(self, selector):
            return Locator(selector)

    checkin = date(2027, 2, 15)
    checkout = date(2027, 2, 22)
    snapshot = _capture_booking_pricing_snapshot(
        Page([rate]),
        ref=ref,
        quote_url=booking_quote_url(ref, checkin, checkout, 2, "GBP"),
        checkin=checkin,
        checkout=checkout,
        guests=2,
        currency="GBP",
        body_text="Availability for your dates",
    )
    result = parse_booking_pricing_snapshot(
        ref, snapshot, checkin, checkout, 2, "GBP"
    )

    assert snapshot["outcome"] == "available"
    assert snapshot["displayed_total"] == 756.0
    assert snapshot["refundable"] is False
    assert snapshot["cancellation_type"] == "Non-refundable"
    assert snapshot["diagnostics"]["cancellation_evidence_route"] == (
        "unique_rendered_rate_row"
    )
    assert result.outcome == Outcome.PRICED
    assert result.refundable is False


def test_nonzero_excluded_charge_is_still_rejected(ref):
    rate = _villa_andrews_flexible_rate()
    rate.update({
        "excluded_charges_amount": 25.0,
        "excluded_count": 1,
    })
    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([rate]),
        ref=ref,
        quote_url=booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR"),
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "unknown"
    assert snapshot["displayed_total"] is None
    assert snapshot["diagnostics"]["structured_rejection_reasons"] == [
        "selected rate has non-zero or missing excluded charges: 25.0"
    ]
    assert _parse(ref, snapshot).outcome == Outcome.ERROR


def test_structured_breakdown_with_bad_arithmetic_is_not_promoted(ref):
    malformed = _villa_andrews_flexible_rate()
    malformed["total"] = 999.0
    quote_url = booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR")

    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([malformed]),
        ref=ref,
        quote_url=quote_url,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "unknown"
    assert snapshot["line_items"] == []
    assert _parse(ref, snapshot).outcome == Outcome.ERROR


def test_villa_andrews_numeric_strings_and_sponsored_discount_reconcile(ref):
    rate = _villa_andrews_flexible_rate()
    rate["net_room"] = "760.0909000000001"
    rate["included_sum"] = "224.0091"
    rate["excluded_charges_amount"] = "0.0"
    for charge in rate["included_charges"]:
        charge["raw_value"] = str(charge["raw_value"])
    display = {
        "block_id": "1731604301_444769642_2_0_0",
        "text": (
            "€ 140.59 × 7 nights € 984.10 Booking.com pays - € 60.81 "
            "You’ll get a reduced rate when you pay online. Total € 923.29"
        ),
    }
    checkin = date(2026, 10, 12)
    checkout = date(2026, 10, 19)
    quote_url = booking_quote_url(ref, checkin, checkout, 2, "EUR")

    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([rate], [display]),
        ref=ref,
        quote_url=quote_url,
        checkin=checkin,
        checkout=checkout,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )
    result = parse_booking_pricing_snapshot(
        ref, snapshot, checkin, checkout, 2, "EUR"
    )

    assert snapshot["outcome"] == "available"
    assert snapshot["displayed_total"] == 923.29
    assert snapshot["diagnostics"]["selected_block_id"] == display["block_id"]
    assert result.outcome == Outcome.PRICED
    assert result.headline_total == 923.29
    assert result.comparable_total == 819.28
    assert result.breakdown.nightly_total == 760.09
    assert result.breakdown.discount == 60.81
    assert result.breakdown.cleaning == 120.0
    assert result.breakdown.tourist_tax == 104.01
    assert result.reconciliation_difference == 0.0
    assert [(item.category, item.amount) for item in result.line_items] == [
        ("accommodation", 760.09),
        ("tax", 76.01),
        ("cleaning", 120.0),
        ("tax", 28.0),
        ("discount", -60.81),
    ]


def test_sponsored_discount_mismatch_is_not_promoted(ref):
    rate = _villa_andrews_flexible_rate()
    display = {
        "block_id": "1731604301_444769642_2_0_0",
        "text": (
            "€ 140.59 × 7 nights € 984.10 Booking.com pays - € 60.81 "
            "Total € 900.00"
        ),
    }
    checkin = date(2026, 10, 12)
    checkout = date(2026, 10, 19)
    quote_url = booking_quote_url(ref, checkin, checkout, 2, "EUR")

    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([rate], [display]),
        ref=ref,
        quote_url=quote_url,
        checkin=checkin,
        checkout=checkout,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "unknown"
    assert snapshot["displayed_total"] is None


def test_rendered_row_restores_reordered_cancellation_metadata(ref):
    nonrefundable = _villa_andrews_flexible_rate()
    nonrefundable.update({
        "block_id": "stale-context-block",
        "cancellation_type": "",
        "total": 900.47,
        "net_room": "684.0636",
        "included_sum": "216.4064",
    })
    nonrefundable["included_charges"] = [
        {**item} for item in nonrefundable["included_charges"]
    ]
    nonrefundable["included_charges"][0]["raw_value"] = "68.4064"
    flexible = _villa_andrews_flexible_rate()
    flexible["block_id"] = "stale-context-block"
    flexible["cancellation_type"] = ""
    displays = [
        {
            "block_id": "1731604301_444768275_2_0_0",
            "cancellation_type": "non_refundable",
            "text": (
                "€ 128.64 × 7 nights € 900.47 Booking.com pays - € 54.72 "
                "Total € 845.75"
            ),
        },
        {
            "block_id": "1731604301_444769642_2_0_0",
            "cancellation_type": "free_cancellation",
            "text": (
                "€ 140.59 × 7 nights € 984.10 Booking.com pays - € 60.81 "
                "Total € 923.29"
            ),
        },
    ]
    checkin = date(2026, 10, 12)
    checkout = date(2026, 10, 19)
    quote_url = booking_quote_url(ref, checkin, checkout, 2, "EUR")

    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([nonrefundable, flexible], displays),
        ref=ref,
        quote_url=quote_url,
        checkin=checkin,
        checkout=checkout,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "available"
    assert snapshot["displayed_total"] == 923.29
    assert snapshot["cancellation_type"] == "Free cancellation"
    assert snapshot["diagnostics"]["structured_flexible_rate_count"] == 1
    assert snapshot["diagnostics"]["structured_rate_display_count"] == 2
    assert snapshot["diagnostics"]["selected_block_id"] == (
        "1731604301_444769642_2_0_0"
    )


def test_unique_rounded_row_total_restores_missing_cancellation_metadata(ref):
    nonrefundable = _villa_andrews_flexible_rate()
    nonrefundable.update({
        "block_id": "stale-nonrefundable",
        "cancellation_type": "",
        "total": 1403.37,
        "net_room": 1150.34,
        "included_sum": 253.03,
    })
    nonrefundable["included_charges"] = [
        {**item} for item in nonrefundable["included_charges"]
    ]
    nonrefundable["included_charges"][0]["raw_value"] = 105.03

    flexible = _villa_andrews_flexible_rate()
    flexible.update({
        "block_id": "stale-flexible",
        "cancellation_type": "",
        "total": 1542.25,
        "net_room": 1267.5,
        "included_sum": 274.75,
    })
    flexible["included_charges"] = [
        {**item} for item in flexible["included_charges"]
    ]
    flexible["included_charges"][0]["raw_value"] = 126.75

    displays = [
        {
            "block_id": "1731604301_444768275_2_0_0",
            "cancellation_type": "non_refundable",
            "text": "",
            "rounded_total": 1403,
        },
        {
            "block_id": "1731604301_444769642_2_0_0",
            "cancellation_type": "free_cancellation",
            "text": "",
            "rounded_total": 1542,
        },
    ]
    checkin = date(2027, 5, 10)
    checkout = date(2027, 5, 17)
    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([nonrefundable, flexible], displays),
        ref=ref,
        quote_url=booking_quote_url(ref, checkin, checkout, 2, "EUR"),
        checkin=checkin,
        checkout=checkout,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "available"
    assert snapshot["displayed_total"] == 1542.25
    assert snapshot["refundable"] is True
    assert snapshot["diagnostics"]["structured_flexible_rate_count"] == 1
    assert snapshot["diagnostics"]["selected_rate_display_join"] == (
        "unique_rounded_total"
    )


def test_rounded_row_total_collision_remains_ambiguous(ref):
    first = _villa_andrews_flexible_rate()
    first.update({
        "block_id": "stale-first",
        "cancellation_type": "",
        "total": 1542.25,
    })
    second = _villa_andrews_flexible_rate()
    second.update({
        "block_id": "stale-second",
        "cancellation_type": "",
        "total": 1542.40,
    })
    display = {
        "block_id": "rendered-flexible",
        "cancellation_type": "free_cancellation",
        "text": "",
        "rounded_total": 1542,
    }
    snapshot = _capture_booking_pricing_snapshot(
        _StructuredBookingPage([first, second], [display]),
        ref=ref,
        quote_url=booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR"),
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Availability for your dates",
    )

    assert snapshot["outcome"] == "unknown"
    assert snapshot["displayed_total"] is None
    assert snapshot["diagnostics"]["structured_flexible_rate_count"] == 0


def test_all_booking_rates_are_kept_with_the_compared_one_marked():
    """337856, 30 Nov-3 Dec 2026: EUR 1,334 non-refundable and EUR 1,460
    free cancellation were both on the page; only one was being reported."""
    from parity.adapters.browser.booking import booking_rate_options

    non_refundable = {"total": 1334.0, "cancellation_type": "non_refundable", "block_id": "a"}
    flexible = {"total": 1459.97, "cancellation_type": "free_cancellation", "block_id": "b"}
    options = booking_rate_options([flexible, non_refundable, dict(flexible)], flexible)

    assert options == [
        {"total": 1334.0, "cancellation": "Non-refundable", "selected": False},
        {"total": 1459.97, "cancellation": "Free cancellation", "selected": True},
    ]


def test_dashboard_price_view_carries_other_rates():
    from parity.dashboard import _price_view

    view = _price_view({
        "outcome": "priced",
        "headline_total": 1459.97,
        "rate_options": [
            {"total": 1334.0, "cancellation": "Non-refundable", "selected": False},
            {"total": "bad", "cancellation": "x"},
        ],
    })
    assert view["rate_options"] == [
        {"total": 1334.0, "cancellation": "Non-refundable", "selected": False}
    ]


def test_booking_dropping_the_requested_dates_is_reported_plainly():
    from parity.adapters.browser.booking import booking_dates_were_dropped

    undated = "Availability We Price Match Select dates to see this property's availability and prices"
    assert booking_dates_were_dropped(
        "https://www.booking.com/hotel/fr/veeve-natural-heights-in-the-6th.en-gb.html", undated
    )
    assert not booking_dates_were_dropped(
        "https://www.booking.com/hotel/fr/x.html?checkin=2026-11-30&checkout=2026-12-03", "Price for 3 nights"
    )
    assert not booking_dates_were_dropped("https://www.booking.com/hotel/fr/x.html", "Price for 3 nights")


class _GbpRatePage:
    """Stand-in page reproducing the operator's 337856 GBP capture: the
    embedded rates carried swapped cancellation terms, and the rate table
    rows show '1 (£1,127)' non-refundable and '1 (£1,234)' free cancellation."""

    def __init__(self):
        self.url = (
            "https://www.booking.com/hotel/fr/veeve-natural-heights-in-the-6th.en-gb.html"
            "?checkin=2026-11-30&checkout=2026-12-03&group_adults=2"
        )

    def evaluate(self, script, *args):
        return {
            "legacy_keys": [],
            "rates": [
                {"total": "1127.13", "cancellation_type": ""},
                {"total": "1233.57", "cancellation_type": "non_refundable"},
            ],
            "rate_displays": [
                {"block_id": "512977502_362962169_4_0_0", "cancellation_type": "non_refundable",
                 "text": "", "rounded_total": 1127},
                {"block_id": "512977502_428303482_4_0_0", "cancellation_type": "free_cancellation",
                 "text": "", "rounded_total": 1234},
            ],
        }

    def locator(self, selector):
        class _Empty:
            def evaluate_all(self, *args, **kwargs):
                return []

            def count(self):
                return 0

            @property
            def first(self):
                return self

            def nth(self, index):
                return self

        return _Empty()

    def wait_for_timeout(self, ms):
        pass


def test_gbp_rate_rows_pair_each_price_with_its_own_terms():
    from datetime import date

    from parity.adapters.browser.booking import _capture_booking_pricing_snapshot
    from parity.core.models import PropertyRef

    ref = PropertyRef(
        site="booking", raw_id="veeve-natural-heights-in-the-6th",
        url="https://www.booking.com/hotel/fr/veeve-natural-heights-in-the-6th.html",
    )
    snapshot = _capture_booking_pricing_snapshot(
        _GbpRatePage(), ref=ref, quote_url="https://www.booking.com/hotel/fr/x.html",
        checkin=date(2026, 11, 30), checkout=date(2026, 12, 3), guests=2,
        currency="GBP", body_text="Price for 3 nights", source_text=None,
    )
    assert snapshot["rate_options"] == [
        {"total": 1127.13, "cancellation": "Non-refundable", "selected": False},
        {"total": 1233.57, "cancellation": "Free cancellation", "selected": True},
    ]
    assert snapshot["diagnostics"]["structured_flexible_rate_count"] == 1


@pytest.mark.parametrize("label", ["6 % Tourism fee", "Tourist levy", "Tourism Levy"])
def test_percentage_tourism_fee_is_a_tax_not_an_unknown_line(ref, label):
    # Live regression (707947, Florida): Booking itemised "6 % Tourism fee".
    snapshot = _snapshot(ref)
    snapshot["line_items"][3]["label"] = label

    result = _parse(ref, snapshot)

    assert result.outcome == Outcome.PRICED
    assert result.breakdown.tourist_tax == 60.0
    assert result.comparable_total == 2150.0


@pytest.mark.parametrize("body, outcome, minimum", [
    ("Unfortunately, this property has no availability on our site for your dates.", "unavailable", None),
    ("We have no availability here between Sun 30 Nov and Tue 2 Dec", "unavailable", None),
    ("There are no rooms available for your dates", "unavailable", None),
    ("This property requires a minimum stay: 3 nights", "min_stay", 3),
    ("Min. stay 5 nights", "min_stay", 5),
    ("You need to book at least 4 nights at this property", "min_stay", 4),
])
def test_booking_recognises_other_unavailable_and_minimum_stay_wordings(ref, body, outcome, minimum):
    # Live (1024817): an unrecognised wording left the page "unknown" and
    # dropped Booking from the whole check.
    from parity.adapters.browser.booking import _BookingSourceOnlyPage

    snapshot = _capture_booking_pricing_snapshot(
        _BookingSourceOnlyPage(),
        ref=ref,
        quote_url=booking_quote_url(ref, CHECKIN, CHECKOUT, 2, "EUR"),
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text=body,
        source_text="",
    )
    assert snapshot["outcome"] == outcome
    assert snapshot.get("minimum_stay_nights") == minimum
