"""Shared math for turning a site's raw price data into the comparable
all-in total: nightly + mandatory fees, excluding tourist tax and any
refundable deposit. See parity-price-verification-2026-09-22.md for why.
"""
from __future__ import annotations

import math
import re

from parity.core.models import PriceBreakdown, PriceComparison, PriceResult


_CURRENCY_CODE_RE = re.compile(r"[A-Z]{3}")
_BREAKDOWN_TOLERANCE = 0.02


def normalise_currency_code(value: str) -> str:
    """Return one explicit ISO-style currency code or fail closed."""
    currency = str(value or "").strip().upper()
    if not _CURRENCY_CODE_RE.fullmatch(currency):
        raise ValueError("currency must be a three-letter code such as EUR or GBP")
    return currency


def apply_quote_evidence_policy(result: PriceResult, requested_currency: str) -> PriceResult:
    """Grade a quote's component evidence after enforcing its currency.

    Guest-visible totals remain the primary parity basis.  Component lines are
    labelled exact only when every included/excluded flag and currency is
    explicit and the included lines rebuild the displayed total.
    """
    expected = normalise_currency_code(requested_currency)
    result.breakdown_status = "unavailable"
    result.breakdown_note = None
    if result.outcome.value != "priced":
        result.breakdown_note = "no successfully priced quote was captured"
        return result
    if not result.currency:
        raise ValueError(f"price comparison blocked: {result.site} quote currency is missing")
    actual = normalise_currency_code(result.currency)
    if actual != expected:
        raise ValueError(
            "price comparison blocked: "
            f"{result.site} returned {actual}, requested {expected}"
        )
    result.currency = actual
    if result.headline_total is None:
        result.breakdown_note = "guest-visible displayed total is missing"
        return result
    if not result.line_items:
        result.breakdown_note = "no exact component lines were captured"
        return result

    included_total = 0.0
    has_accommodation = False
    for item in result.line_items:
        if not item.currency:
            result.breakdown_note = f"component {item.label!r} has no explicit currency"
            return result
        line_currency = normalise_currency_code(item.currency)
        if line_currency != expected:
            raise ValueError(
                "price comparison blocked: "
                f"{result.site} component {item.label!r} uses {line_currency}, "
                f"requested {expected}"
            )
        item.currency = line_currency
        if not isinstance(item.included_in_displayed_total, bool):
            result.breakdown_note = (
                f"component {item.label!r} does not state whether it is included "
                "in the displayed total"
            )
            return result
        if item.included_in_displayed_total:
            included_total += item.amount
        if item.category == "accommodation":
            has_accommodation = True
    if not has_accommodation:
        result.breakdown_note = "captured components contain no accommodation line"
        return result
    difference = round(included_total - float(result.headline_total), 2)
    if abs(difference) > _BREAKDOWN_TOLERANCE:
        result.breakdown_note = (
            "captured components do not reconcile to the guest-visible displayed "
            f"total (difference {difference:+.2f})"
        )
        return result
    if (
        result.reconciliation_difference is not None
        and abs(result.reconciliation_difference) > _BREAKDOWN_TOLERANCE
    ):
        result.breakdown_note = (
            "adapter reconciliation exceeds the permitted tolerance "
            f"({result.reconciliation_difference:+.2f})"
        )
        return result

    result.breakdown_status = "exact"
    result.breakdown_note = (
        "captured component lines reconcile to the guest-visible displayed total"
    )
    return result


def comparable_total(breakdown: PriceBreakdown) -> float | None:
    """Stay minus discount plus mandatory fees, excluding tax & deposit.

    Returns None if we don't even have a nightly total — never guess.
    """
    if breakdown.nightly_total is None:
        return None
    total = breakdown.nightly_total
    total -= breakdown.discount or 0.0
    total += breakdown.cleaning or 0.0
    total += breakdown.service_fee or 0.0
    total += breakdown.other_mandatory_fees or 0.0
    return round(total, 2)


def compare_aligned_prices(
    subject: PriceResult,
    candidate: PriceResult,
) -> PriceComparison:
    """Compare two quotes only after their critical dimensions align."""
    if not subject.is_priced() or not candidate.is_priced():
        raise ValueError("price comparison requires two successfully priced quotes")
    if (subject.checkin, subject.checkout) != (candidate.checkin, candidate.checkout):
        raise ValueError("price comparison blocked: check-in or checkout dates differ")
    if subject.guests != candidate.guests:
        raise ValueError("price comparison blocked: guest counts differ")
    if not subject.currency or not candidate.currency:
        raise ValueError("price comparison blocked: both quote currencies are required")
    subject_currency = subject.currency.upper()
    candidate_currency = candidate.currency.upper()
    if subject_currency != candidate_currency:
        raise ValueError(
            "price comparison blocked: currencies differ "
            f"({subject_currency} versus {candidate_currency})"
        )
    displayed_totals = (subject.headline_total, candidate.headline_total)
    if any(
        isinstance(total, bool)
        or not isinstance(total, (int, float))
        or not math.isfinite(float(total))
        or total <= 0
        for total in displayed_totals
    ):
        raise ValueError(
            "price comparison blocked: valid guest-visible displayed totals are "
            "required for both quotes"
        )
    refundability_note = None
    if subject.refundable is None or candidate.refundable is None:
        missing = [
            price.site
            for price in (subject, candidate)
            if price.refundable is None
        ]
        refundability_note = (
            "Refundability evidence is incomplete for "
            f"{', '.join(missing)}; guest-visible prices are still compared."
        )
    elif subject.refundable != candidate.refundable:
        subject_terms = "refundable" if subject.refundable else "non-refundable"
        candidate_terms = "refundable" if candidate.refundable else "non-refundable"
        refundability_note = (
            "Cancellation terms differ: "
            f"{subject.site} is {subject_terms}; "
            f"{candidate.site} is {candidate_terms}."
        )

    assert subject.comparable_total is not None
    assert candidate.comparable_total is not None
    if subject.comparable_total <= 0:
        raise ValueError("price comparison blocked: subject comparable total must be positive")

    subject_displayed = round(float(subject.headline_total), 2)
    candidate_displayed = round(float(candidate.headline_total), 2)
    difference = round(candidate_displayed - subject_displayed, 2)
    percentage = round((difference / subject_displayed) * 100, 2)
    if difference < 0:
        lower_price_site = candidate.site
        verdict = "candidate_cheaper"
    elif difference > 0:
        lower_price_site = subject.site
        verdict = "candidate_more_expensive"
    else:
        lower_price_site = None
        verdict = "equal"

    return PriceComparison(
        currency=subject_currency,
        subject_site=subject.site,
        candidate_site=candidate.site,
        comparison_basis="guest_visible_displayed_total",
        subject_displayed_total=subject_displayed,
        candidate_displayed_total=candidate_displayed,
        subject_comparable_total=subject.comparable_total,
        candidate_comparable_total=candidate.comparable_total,
        candidate_minus_subject=difference,
        candidate_difference_percent=percentage,
        lower_price_site=lower_price_site,
        verdict=verdict,
        refundability_note=refundability_note,
    )
