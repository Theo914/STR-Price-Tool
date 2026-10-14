from datetime import date

from parity.core.discounts import build_discount_scenarios
from parity.core.models import Outcome, PriceLineItem, PriceResult, PropertyRef


CHECKIN = date(2027, 5, 10)
CHECKOUT = date(2027, 5, 17)


def _price(site, currency, headline, lines, *, status="exact"):
    return PriceResult(
        site=site,
        ref=PropertyRef(site=site, raw_id="1", url=f"https://example.test/{site}/1"),
        outcome=Outcome.PRICED,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency=currency,
        comparable_total=headline,
        headline_total=headline,
        line_items=lines,
        breakdown_status=status,
    )


def _line(label, amount, category, currency):
    return PriceLineItem(
        label=label,
        amount=amount,
        category=category,
        currency=currency,
        included_in_displayed_total=True,
    )


def test_airbnb_ten_percent_is_additional_to_a_different_long_stay_discount():
    subject = _price(
        "travelstaytion",
        "EUR",
        796.84,
        [_line("Accommodation", 796.84, "accommodation", "EUR")],
    )
    candidate = _price(
        "airbnb",
        "EUR",
        742.26,
        [
            _line("3 nights x EUR 254.00", 762.00, "accommodation", "EUR"),
            _line("Long stay discount", -19.74, "discount", "EUR"),
        ],
    )

    scenarios = build_discount_scenarios(candidate, subject)

    assert len(scenarios) == 1
    scenario = scenarios[0]
    assert scenario.discount_percent == 10
    assert scenario.status == "available"
    assert scenario.eligible_accommodation_total == 762.00
    assert scenario.additional_discount_amount == 76.20
    assert scenario.percentage_tax_reduction == 0.0
    assert scenario.adjusted_guest_total == 666.06
    assert scenario.adjusted_candidate_minus_subject == -130.78
    assert scenario.adjusted_difference_percent == -16.41


def test_booking_scenarios_recalculate_explicit_percentage_tax_only():
    subject = _price(
        "travelstaytion",
        "EUR",
        1480.58,
        [_line("Accommodation", 1480.58, "accommodation", "EUR")],
    )
    candidate = _price(
        "booking",
        "EUR",
        1542.25,
        [
            _line("7 nights accommodation", 1267.50, "accommodation", "EUR"),
            _line("10 % VAT", 126.75, "tax", "EUR"),
            _line("Cleaning fee", 120.00, "cleaning", "EUR"),
            _line("City tax", 28.00, "tax", "EUR"),
        ],
    )

    scenarios = build_discount_scenarios(candidate, subject)

    assert [item.discount_percent for item in scenarios] == [10, 15, 20]
    assert [item.additional_discount_amount for item in scenarios] == [126.75, 190.13, 253.50]
    assert [item.percentage_tax_reduction for item in scenarios] == [12.68, 19.01, 25.35]
    assert [item.adjusted_guest_total for item in scenarios] == [1402.82, 1333.11, 1263.40]
    assert all("Fixed fees" not in item.note for item in scenarios)


def test_equivalent_captured_discount_is_not_stacked():
    candidate = _price(
        "booking",
        "GBP",
        900.00,
        [
            _line("Accommodation", 1000.00, "accommodation", "GBP"),
            _line("10% Genius discount", -100.00, "discount", "GBP"),
        ],
    )

    scenarios = build_discount_scenarios(candidate)

    assert scenarios[0].status == "already_applied"
    assert scenarios[0].adjusted_guest_total == 900.00
    assert scenarios[0].additional_discount_amount == 0.0
    assert scenarios[1].status == "available"
    assert scenarios[2].status == "available"


def test_scenarios_are_disabled_without_exact_component_evidence():
    candidate = _price(
        "airbnb",
        "EUR",
        500.00,
        [_line("Accommodation", 500.00, "accommodation", "EUR")],
        status="unavailable",
    )

    scenarios = build_discount_scenarios(candidate)

    assert len(scenarios) == 1
    assert scenarios[0].status == "unavailable"
    assert scenarios[0].adjusted_guest_total is None
    assert "exact component evidence" in scenarios[0].note
