from datetime import date

import pytest

from parity.core.models import Outcome, PriceLineItem, PriceResult, PropertyRef
from parity.core.pricing import (
    apply_quote_evidence_policy,
    compare_aligned_prices,
    normalise_currency_code,
)


CHECKIN = date(2027, 5, 10)
CHECKOUT = date(2027, 5, 17)


def _price(
    site: str,
    total: float,
    *,
    displayed_total: float | None = None,
    currency: str = "EUR",
    guests: int = 2,
    refundable: bool | None = True,
) -> PriceResult:
    ref = PropertyRef(site=site, raw_id=site, url=f"https://example.com/{site}")
    return PriceResult(
        site=site,
        ref=ref,
        outcome=Outcome.PRICED,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=guests,
        currency=currency,
        comparable_total=total,
        headline_total=total if displayed_total is None else displayed_total,
        refundable=refundable,
    )


def test_compare_aligned_prices_reports_candidate_saving():
    comparison = compare_aligned_prices(
        _price("travelstaytion", 1480.57, displayed_total=1552.18),
        _price("booking", 1387.50, displayed_total=1542.25),
    )

    assert comparison.comparison_basis == "guest_visible_displayed_total"
    assert comparison.subject_displayed_total == 1552.18
    assert comparison.candidate_displayed_total == 1542.25
    assert comparison.subject_comparable_total == 1480.57
    assert comparison.candidate_comparable_total == 1387.50
    assert comparison.candidate_minus_subject == -9.93
    assert comparison.candidate_difference_percent == -0.64
    assert comparison.lower_price_site == "booking"
    assert comparison.verdict == "candidate_cheaper"
    assert comparison.refundability_note is None


def test_compare_aligned_prices_preserves_refundability_difference_as_note():
    comparison = compare_aligned_prices(
        _price("travelstaytion", 100, refundable=True),
        _price("booking", 90, refundable=False),
    )

    assert comparison.candidate_minus_subject == -10
    assert comparison.refundability_note == (
        "Cancellation terms differ: travelstaytion is refundable; "
        "booking is non-refundable."
    )


def test_compare_aligned_prices_preserves_missing_refundability_as_note():
    comparison = compare_aligned_prices(
        _price("travelstaytion", 100, refundable=False),
        _price("booking", 90, refundable=None),
    )

    assert comparison.candidate_minus_subject == -10
    assert comparison.refundability_note == (
        "Refundability evidence is incomplete for booking; "
        "guest-visible prices are still compared."
    )


def test_compare_aligned_prices_uses_guest_totals_for_tenby_verdict():
    comparison = compare_aligned_prices(
        _price("travelstaytion", 724.51, displayed_total=724.52, refundable=False),
        _price("booking", 630.00, displayed_total=756.00, refundable=False),
    )

    assert comparison.candidate_minus_subject == 31.48
    assert comparison.candidate_difference_percent == 4.34
    assert comparison.lower_price_site == "travelstaytion"
    assert comparison.verdict == "candidate_more_expensive"


def test_compare_aligned_prices_requires_both_guest_visible_totals():
    subject = _price("travelstaytion", 100)
    candidate = _price("booking", 90)
    candidate.headline_total = None

    with pytest.raises(ValueError, match="guest-visible displayed totals"):
        compare_aligned_prices(subject, candidate)


@pytest.mark.parametrize(
    ("subject", "candidate", "message"),
    [
        (_price("travelstaytion", 100, currency="EUR"), _price("booking", 90, currency="GBP"), "currencies differ"),
        (_price("travelstaytion", 100, guests=2), _price("booking", 90, guests=3), "guest counts differ"),
    ],
)
def test_compare_aligned_prices_fails_closed(subject, candidate, message):
    with pytest.raises(ValueError, match=message):
        compare_aligned_prices(subject, candidate)


def test_exact_breakdown_requires_explicit_currency_inclusion_and_reconciliation():
    price = _price("booking", 120.0)
    price.line_items = [
        PriceLineItem(
            label="Accommodation",
            amount=100.0,
            category="accommodation",
            currency="EUR",
            included_in_displayed_total=True,
        ),
        PriceLineItem(
            label="Cleaning fee",
            amount=20.0,
            category="cleaning",
            currency="EUR",
            included_in_displayed_total=True,
        ),
    ]
    price.reconciliation_difference = 0.0

    apply_quote_evidence_policy(price, "eur")

    assert price.currency == "EUR"
    assert price.breakdown_status == "exact"
    assert "reconcile" in price.breakdown_note


def test_incomplete_component_evidence_keeps_guest_total_but_hides_breakdown():
    price = _price("airbnb", 120.0)
    price.line_items = [
        PriceLineItem(
            label="Accommodation",
            amount=120.0,
            category="accommodation",
            currency=None,
            included_in_displayed_total=True,
        )
    ]

    apply_quote_evidence_policy(price, "EUR")

    assert price.headline_total == 120.0
    assert price.breakdown_status == "unavailable"
    assert "no explicit currency" in price.breakdown_note


def test_quote_evidence_policy_blocks_requested_or_component_currency_mismatch():
    quote_mismatch = _price("booking", 120.0, currency="GBP")
    with pytest.raises(ValueError, match="returned GBP, requested EUR"):
        apply_quote_evidence_policy(quote_mismatch, "EUR")

    component_mismatch = _price("booking", 120.0)
    component_mismatch.line_items = [
        PriceLineItem(
            label="Accommodation",
            amount=120.0,
            category="accommodation",
            currency="GBP",
            included_in_displayed_total=True,
        )
    ]
    with pytest.raises(ValueError, match="component .* uses GBP, requested EUR"):
        apply_quote_evidence_policy(component_mismatch, "EUR")


@pytest.mark.parametrize("value", ["EU", "EURO", "£", ""])
def test_currency_code_must_be_explicit_three_letters(value):
    with pytest.raises(ValueError, match="three-letter code"):
        normalise_currency_code(value)
