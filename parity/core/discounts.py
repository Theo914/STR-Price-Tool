"""Deterministic what-if discount arithmetic for the compact UI.

These scenarios never claim that an OTA will actually offer the adjusted
price.  They operate only on an already captured, exact price breakdown.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal, ROUND_HALF_UP

from parity.core.models import DiscountScenario, PriceResult


_ALLOWED_DISCOUNTS = {
    "airbnb": (10.0,),
    "booking": (10.0, 15.0, 20.0),
}
_PERCENT_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*%")
_RATE_TOLERANCE = 0.15


def _money(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _percentage_from_label(label: str) -> float | None:
    match = _PERCENT_RE.search(label)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return value if 0 < value < 100 else None


def _equivalent_discount_rates(price: PriceResult, accommodation: float) -> set[float]:
    rates: set[float] = set()
    captured_discount = 0.0
    for item in price.line_items:
        if item.category != "discount" or item.included_in_displayed_total is not True:
            continue
        captured_discount += abs(item.amount)
        label_rate = _percentage_from_label(item.label)
        if label_rate is not None:
            rates.add(round(label_rate, 2))
    if accommodation > 0 and captured_discount > 0:
        rates.add(round((captured_discount / accommodation) * 100, 2))
    return rates


def _unavailable(site: str, percent: float, note: str) -> DiscountScenario:
    return DiscountScenario(
        site=site,
        discount_percent=percent,
        status="unavailable",
        currency=None,
        original_guest_total=None,
        eligible_accommodation_total=None,
        additional_discount_amount=None,
        percentage_tax_reduction=None,
        adjusted_guest_total=None,
        adjusted_candidate_minus_subject=None,
        adjusted_difference_percent=None,
        note=note,
    )


def build_discount_scenarios(
    candidate: PriceResult,
    subject: PriceResult | None = None,
) -> list[DiscountScenario]:
    """Build every supported channel scenario from exact captured evidence."""
    percentages = _ALLOWED_DISCOUNTS.get(candidate.site, ())
    if not percentages:
        return []
    if candidate.breakdown_status != "exact":
        return [
            _unavailable(
                candidate.site,
                percent,
                "exact component evidence is required before applying a discount",
            )
            for percent in percentages
        ]
    if candidate.headline_total is None or not candidate.currency:
        return [
            _unavailable(
                candidate.site,
                percent,
                "guest-visible total and currency are required",
            )
            for percent in percentages
        ]

    accommodation = _money(sum(
        item.amount
        for item in candidate.line_items
        if item.category == "accommodation"
        and item.included_in_displayed_total is True
    ))
    if not math.isfinite(accommodation) or accommodation <= 0:
        return [
            _unavailable(
                candidate.site,
                percent,
                "no positive included accommodation component was captured",
            )
            for percent in percentages
        ]

    tax_rates = [
        rate
        for item in candidate.line_items
        if item.category == "tax" and item.included_in_displayed_total is True
        for rate in [_percentage_from_label(item.label)]
        if rate is not None
    ]
    existing_rates = _equivalent_discount_rates(candidate, accommodation)
    subject_total = (
        float(subject.headline_total)
        if subject is not None
        and subject.headline_total is not None
        and subject.currency == candidate.currency
        else None
    )

    scenarios: list[DiscountScenario] = []
    for percent in percentages:
        equivalent = next(
            (rate for rate in existing_rates if abs(rate - percent) <= _RATE_TOLERANCE),
            None,
        )
        if equivalent is not None:
            scenarios.append(DiscountScenario(
                site=candidate.site,
                discount_percent=percent,
                status="already_applied",
                currency=candidate.currency,
                original_guest_total=round(float(candidate.headline_total), 2),
                eligible_accommodation_total=accommodation,
                additional_discount_amount=0.0,
                percentage_tax_reduction=0.0,
                adjusted_guest_total=round(float(candidate.headline_total), 2),
                adjusted_candidate_minus_subject=(
                    round(float(candidate.headline_total) - subject_total, 2)
                    if subject_total is not None
                    else None
                ),
                adjusted_difference_percent=(
                    round(
                        ((float(candidate.headline_total) - subject_total) / subject_total) * 100,
                        2,
                    )
                    if subject_total is not None and subject_total > 0
                    else None
                ),
                note=(
                    f"an equivalent captured discount of approximately {equivalent:.2f}% "
                    "is already included; it was not stacked"
                ),
            ))
            continue

        discount = _money(accommodation * (percent / 100))
        tax_reduction = _money(sum(discount * (rate / 100) for rate in tax_rates))
        adjusted = _money(float(candidate.headline_total) - discount - tax_reduction)
        difference = _money(adjusted - subject_total) if subject_total is not None else None
        difference_percent = (
            round((difference / subject_total) * 100, 2)
            if difference is not None and subject_total and subject_total > 0
            else None
        )
        tax_note = (
            " Explicit percentage-based taxes were recalculated."
            if tax_rates
            else " Fixed fees and captured taxes were left unchanged."
        )
        scenarios.append(DiscountScenario(
            site=candidate.site,
            discount_percent=percent,
            status="available",
            currency=candidate.currency,
            original_guest_total=round(float(candidate.headline_total), 2),
            eligible_accommodation_total=accommodation,
            additional_discount_amount=discount,
            percentage_tax_reduction=tax_reduction,
            adjusted_guest_total=adjusted,
            adjusted_candidate_minus_subject=difference,
            adjusted_difference_percent=difference_percent,
            note=(
                "Hypothetical additional accommodation discount only; this is not a "
                "bookable OTA quote." + tax_note
            ),
        ))
    return scenarios
