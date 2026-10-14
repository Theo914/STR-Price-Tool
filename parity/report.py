"""Turns a ListingReport into something Theo can read: a console table plus
optional JSON/CSV for keeping alongside other analysis. No history is kept
by this tool -- if he wants a record, this is what he saves himself.
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import asdict
from enum import Enum
from io import StringIO

from parity.core.models import DiscoveryReport, IdentityReport, ListingReport, Outcome, QuoteReport


def _fmt_money(v: float | None, currency: str = "EUR") -> str:
    return f"{currency} {v:,.2f}" if v is not None else "--"


def to_console(report: ListingReport) -> str:
    lines = []
    subj = report.subject
    lines.append(f"Parity check: {subj.title or subj.url}")
    lines.append(f"  site={subj.site} id={subj.raw_id} licence={subj.licence_number or '--'}")
    lines.append("")

    lines.append("Prices:")
    header = f"  {'site':<16} {'outcome':<12} {'comparable':>12} {'headline':>12}  refundable  notes"
    lines.append(header)
    for p in sorted(report.prices, key=lambda p: (p.comparable_total is None, p.comparable_total or 0)):
        is_cheapest = report.cheapest is not None and p is report.cheapest
        marker = " *" if is_cheapest else "  "
        refund = "yes" if p.refundable else ("no" if p.refundable is False else "?")
        lines.append(
            f"{marker}{p.site:<16} {p.outcome.value:<12} {_fmt_money(p.comparable_total):>12} "
            f"{_fmt_money(p.headline_total):>12}  {refund:<10}  {p.notes or ''}"
        )
    lines.append("")

    if report.cheapest:
        c = report.cheapest
        flag = " (NO REFUNDABLE RATE)" if c.refundable is False else ""
        lines.append(f"Cheapest comparable price: {c.site} -- {_fmt_money(c.comparable_total)}{flag}")
    else:
        lines.append("Cheapest comparable price: none of the checked channels priced successfully.")
    lines.append("")

    if report.neighbours:
        n = report.neighbours
        lines.append("Neighbour comps (Airbnb, same bedrooms/bathrooms/pool, <=3km):")
        lines.append(f"  {len(n.comps)} comps, median {_fmt_money(n.median)}"
                      f"{'  [LOW CONFIDENCE]' if n.low_confidence else ''}")
        if n.pct_vs_median is not None:
            direction = "below" if n.pct_vs_median < 0 else "above"
            lines.append(f"  our price is {abs(n.pct_vs_median)}% {direction} the comp median"
                         f" (percentile {n.percentile}%)")
        for c in sorted(n.comps, key=lambda c: c.comparable_total or 0):
            lines.append(f"    {c.ref.url}  {_fmt_money(c.comparable_total)}  {c.distance_km}km")
        lines.append("")

    if report.matches:
        lines.append("Matched listings:")
        for m in report.matches:
            details = "; ".join(f"{e.kind.value}: {e.detail}" for e in m.evidence) or "no evidence"
            lines.append(f"  {m.ref.url}  [{m.decision.value}] {details}")

    return "\n".join(lines)


def to_json(report: ListingReport) -> str:
    def default(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, Enum):
            return o.value
        raise TypeError(f"not serializable: {o!r}")

    return json.dumps(asdict(report), default=default, indent=2, ensure_ascii=False)


def to_csv(report: ListingReport) -> str:
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(["site", "outcome", "comparable_total", "headline_total", "refundable",
                      "cancellation_type", "source_url", "notes"])
    for p in report.prices:
        writer.writerow([p.site, p.outcome.value, p.comparable_total, p.headline_total,
                          p.refundable, p.cancellation_type, p.source_url, p.notes])
    return buf.getvalue()


def print_report(report: ListingReport, fmt: str = "console") -> None:
    if fmt == "json":
        print(to_json(report))
    elif fmt == "csv":
        sys.stdout.write(to_csv(report))
    else:
        print(to_console(report))


def discovery_to_console(report: DiscoveryReport) -> str:
    lines = [
        f"Discovery candidates: {report.subject.title or report.subject.url}",
        "Candidates are unverified and cannot be priced until identity verification passes.",
        "",
    ]
    if not report.candidates:
        lines.append("No candidates generated.")
    for candidate in report.candidates:
        lines.append(
            f"  {candidate.ref.site:<14} score={candidate.score:>5.1f}  {candidate.ref.url}"
        )
        lines.append(f"    sources: {', '.join(candidate.sources)}")
        if candidate.reasons:
            lines.append(f"    ranking: {'; '.join(candidate.reasons)}")
    if report.provider_diagnostics:
        lines.extend(("", "Provider diagnostics:"))
        for provider, diagnostic in sorted(report.provider_diagnostics.items()):
            lines.append(f"  {provider}: {diagnostic}")
    if report.provider_errors:
        lines.extend(("", "Provider errors:"))
        for provider, error in sorted(report.provider_errors.items()):
            lines.append(f"  {provider}: {error}")
    return "\n".join(lines)


def discovery_to_json(report: DiscoveryReport) -> str:
    def default(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, Enum):
            return o.value
        raise TypeError(f"not serializable: {o!r}")

    return json.dumps(asdict(report), default=default, indent=2, ensure_ascii=False)


def print_discovery_report(report: DiscoveryReport, fmt: str = "console") -> None:
    print(discovery_to_json(report) if fmt == "json" else discovery_to_console(report))


def identity_to_console(report: IdentityReport) -> str:
    lines = [
        f"Identity verification: {report.subject.title or report.subject.url}",
        "No prices were requested.",
        "",
    ]
    for match in report.matches:
        lines.append(f"  {match.decision.value:<26} {match.ref.url}")
        for evidence in match.evidence:
            lines.append(f"    {evidence.kind.value}: {evidence.detail}")
    return "\n".join(lines)


def identity_to_json(report: IdentityReport) -> str:
    def default(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, Enum):
            return o.value
        raise TypeError(f"not serializable: {o!r}")

    return json.dumps(asdict(report), default=default, indent=2, ensure_ascii=False)


def identity_verdicts_to_console(report: IdentityReport) -> str:
    """Compact verdict lines shown even when the full report is written to disk."""
    lines = []
    for match in report.matches:
        evidence = match.evidence[0].detail if match.evidence else "no evidence recorded"
        lines.append(
            f"[parity] RESULT {match.decision.value.upper()}: {match.ref.url} -- {evidence}"
        )
    return "\n".join(lines)


def quote_to_console(report: QuoteReport) -> str:
    price = report.price
    lines = [
        f"Price-only quote: {report.subject.url}",
        f"  verified mapping: {report.candidate.url}",
        f"  stay: {price.checkin.isoformat()} to {price.checkout.isoformat()}, {price.guests} guest(s)",
        f"  outcome: {price.outcome.value}",
        f"  comparable: {_fmt_money(price.comparable_total, price.currency or '')}",
        f"  displayed total: {_fmt_money(price.headline_total, price.currency or '')}",
        f"  price breakdown: {price.breakdown_status}",
    ]
    if report.comparison_stay_nights is not None:
        lines.append(f"  comparison stay: {report.comparison_stay_nights} night(s)")
    if report.subject_minimum_stay_nights is not None:
        lines.append(
            "  TravelStaytion minimum for check-in: "
            f"{report.subject_minimum_stay_nights} night(s)"
        )
    if report.candidate_minimum_stay_nights is not None:
        lines.append(
            f"  {report.candidate.site} minimum for check-in: "
            f"{report.candidate_minimum_stay_nights} night(s)"
        )
    elif report.candidate_minimum_stay_evidence == "accepted_comparison_duration_only":
        lines.append(
            f"  {report.candidate.site} accepts comparison stay; exact minimum not exposed"
        )
    if price.breakdown_status == "exact" and price.line_items:
        lines.append("  line items:")
        for item in price.line_items:
            lines.append(f"    {item.category:<16} {item.amount:>10.2f}  {item.label}")
    if price.reconciliation_difference is not None:
        lines.append(f"  reconciliation difference: {price.reconciliation_difference:+.2f}")
    if price.notes:
        lines.append(f"  notes: {price.notes}")
    if report.subject_price is not None:
        lines.extend((
            "",
            "Aligned TravelStaytion quote:",
            f"  comparable: {_fmt_money(report.subject_price.comparable_total, report.subject_price.currency or '')}",
            f"  displayed total: {_fmt_money(report.subject_price.headline_total, report.subject_price.currency or '')}",
            f"  price breakdown: {report.subject_price.breakdown_status}",
        ))
    if report.comparison is not None:
        comparison = report.comparison
        if comparison.verdict == "equal":
            summary = "equal guest-visible prices"
        else:
            relation = (
                "cheaper" if comparison.verdict == "candidate_cheaper" else "more expensive"
            )
            summary = (
                f"{comparison.candidate_site} {relation} by "
                f"{comparison.currency} {abs(comparison.candidate_minus_subject):.2f} "
                f"({abs(comparison.candidate_difference_percent):.2f}%)"
            )
        lines.extend(("", f"Parity result: {summary}"))
        if comparison.refundability_note:
            lines.append(f"Parity note: {comparison.refundability_note}")
    if report.discount_scenarios:
        lines.append("")
        lines.append("What-if discounts (hypothetical only):")
        for scenario in report.discount_scenarios:
            if scenario.status == "available":
                lines.append(
                    f"  {scenario.discount_percent:.0f}% -> "
                    f"{scenario.currency} {scenario.adjusted_guest_total:.2f} guest total"
                )
            else:
                lines.append(
                    f"  {scenario.discount_percent:.0f}% -> {scenario.status}: {scenario.note}"
                )
    return "\n".join(lines)


def quote_to_json(report: QuoteReport) -> str:
    def default(o):
        if hasattr(o, "isoformat"):
            return o.isoformat()
        if isinstance(o, Enum):
            return o.value
        raise TypeError(f"not serializable: {o!r}")

    return json.dumps(asdict(report), default=default, indent=2, ensure_ascii=False)


def quote_verdict_to_console(report: QuoteReport) -> str:
    price = report.price
    if price.is_priced() and price.headline_total is not None:
        detail = f"guest total {price.currency or ''} {price.headline_total:.2f}"
        if price.breakdown_status == "exact":
            detail += "; exact price breakdown reconciled"
        else:
            detail += "; price breakdown unavailable"
    else:
        detail = price.notes or "no safe quote produced"
    verdict = (
        f"[parity] RESULT {price.outcome.value.upper()}: "
        f"{report.candidate.url} -- {detail}"
    )
    if not price.is_priced():
        return verdict

    breakdown = []
    if price.breakdown_status == "exact":
        breakdown.append("[parity]   PRICE BREAKDOWN: exact and reconciled")
        for item in price.line_items:
            inclusion = (
                "included in displayed total"
                if item.included_in_displayed_total is True
                else "excluded from displayed total"
            )
            breakdown.append(
                f"[parity]   {item.label}: {item.currency} {item.amount:+.2f} "
                f"({item.category}, {inclusion})"
            )
    else:
        breakdown.append(
            "[parity]   PRICE BREAKDOWN: unavailable"
            f"{' -- ' + price.breakdown_note if price.breakdown_note else ''}"
        )
    if price.headline_total is not None:
        breakdown.append(
            f"[parity]   DISPLAYED TOTAL: {price.currency} {price.headline_total:.2f}"
        )
    if price.breakdown_status == "exact" and price.comparable_total is not None:
        breakdown.append(
            f"[parity]   COMPARABLE TOTAL: "
            f"{price.currency} {price.comparable_total:.2f}"
        )
    if report.subject_price is not None and report.comparison is not None:
        comparison = report.comparison
        breakdown.append(
            "[parity]   TRAVELSTAYTION GUEST TOTAL: "
            f"{comparison.currency} {comparison.subject_displayed_total:.2f}"
        )
        breakdown.append(
            f"[parity]   {comparison.candidate_site.upper()} GUEST TOTAL: "
            f"{comparison.currency} {comparison.candidate_displayed_total:.2f}"
        )
        if (
            report.subject_price.breakdown_status == "exact"
            and price.breakdown_status == "exact"
        ):
            breakdown.append(
                "[parity]   COMPONENT AUDIT (CAPTURED TAXES EXCLUDED): "
                f"{comparison.subject_site} {comparison.currency} "
                f"{comparison.subject_comparable_total:.2f}; "
                f"{comparison.candidate_site} {comparison.currency} "
                f"{comparison.candidate_comparable_total:.2f}"
            )
        else:
            breakdown.append(
                "[parity]   COMPONENT AUDIT: unavailable unless both exact "
                "breakdowns reconcile"
            )
        if comparison.verdict == "equal":
            parity_line = "[parity] PARITY: equal guest-visible prices"
        else:
            relation = (
                "cheaper" if comparison.verdict == "candidate_cheaper" else "more expensive"
            )
            parity_line = (
                f"[parity] PARITY: {comparison.candidate_site} {relation} by "
                f"{comparison.currency} {abs(comparison.candidate_minus_subject):.2f} "
                f"({abs(comparison.candidate_difference_percent):.2f}%)"
            )
        breakdown.append(parity_line)
        if comparison.refundability_note:
            breakdown.append(
                f"[parity]   TERMS: {comparison.refundability_note}"
            )
    if report.discount_scenarios:
        breakdown.append("[parity]   WHAT-IF DISCOUNTS (HYPOTHETICAL, NOT BOOKABLE):")
        for scenario in report.discount_scenarios:
            if scenario.status == "available":
                detail = (
                    f"{scenario.currency} {scenario.adjusted_guest_total:.2f} guest total; "
                    f"additional discount {scenario.currency} "
                    f"{scenario.additional_discount_amount:.2f}"
                )
                if scenario.percentage_tax_reduction:
                    detail += (
                        f"; percentage-tax reduction {scenario.currency} "
                        f"{scenario.percentage_tax_reduction:.2f}"
                    )
                breakdown.append(
                    f"[parity]     {scenario.discount_percent:.0f}%: {detail}"
                )
            else:
                breakdown.append(
                    f"[parity]     {scenario.discount_percent:.0f}%: "
                    f"{scenario.status} -- {scenario.note}"
                )
    return "\n".join([verdict, *breakdown])
