"""Fail-closed pricing evidence shared by Vrbo and Expedia.

Snapshots contain only bounded public checkout fields.  A quote is accepted
only when its exact request identity matches, every monetary component is
typed explicitly, and the included component arithmetic reconciles to the
displayed total.  The initial live capture is intentionally diagnostic: it
records bounded, allow-listed price elements but does not infer line items
from arbitrary visible money text.
"""
from __future__ import annotations

import math
import re
import time
from datetime import date
from urllib.parse import urlencode, urlsplit, urlunsplit

from parity.adapters.browser.browser_session import BrowserSession
from parity.adapters.browser.expedia_group_identity import (
    canonical_expedia_url,
    canonical_vrbo_url,
)
from parity.core.models import (
    Outcome,
    PriceBreakdown,
    PriceLineItem,
    PriceResult,
    PropertyRef,
)
from parity.core.pricing import comparable_total
from parity.http_control import RequestDeferred


EXPEDIA_GROUP_PRICING_SNAPSHOT_VERSION = 1
_RECONCILIATION_TOLERANCE = 0.02
_PLATFORMS = {"vrbo_expedia", "expedia"}


def _display_name(platform: str) -> str:
    return "Vrbo" if platform == "vrbo_expedia" else "Expedia"


def canonical_platform_url(platform: str, url: str) -> str:
    if platform == "vrbo_expedia":
        return canonical_vrbo_url(url)
    if platform == "expedia":
        return canonical_expedia_url(url)
    raise ValueError("unsupported Expedia Group pricing platform")


def expedia_group_quote_url(
    platform: str,
    ref: PropertyRef,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
) -> str:
    """Build one deterministic public checkout-view URL."""
    canonical = canonical_platform_url(platform, ref.url)
    currency = currency.upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"{_display_name(platform)} quote currency must be a three-letter code")
    if checkout <= checkin:
        raise ValueError(f"{_display_name(platform)} quote checkout must be after checkin")
    if guests <= 0:
        raise ValueError(f"{_display_name(platform)} quote guests must be greater than zero")
    query = urlencode([
        ("chkin", checkin.isoformat()),
        ("chkout", checkout.isoformat()),
        ("adults", str(guests)),
        ("currency", currency),
    ])
    parts = urlsplit(canonical)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _strict_amount(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    return amount if math.isfinite(amount) else None


def _line_category(label: str) -> str | None:
    low = " ".join(label.casefold().split())
    if any(word in low for word in ("discount", "saving", "promotion", "coupon")):
        return "discount"
    if any(word in low for word in (
        "tourist tax", "city tax", "occupancy tax", "sales tax", "vat", "taxes", "tax",
    )):
        return "tax"
    if "deposit" in low or "damage protection" in low:
        return "deposit"
    if "clean" in low:
        return "cleaning"
    if any(word in low for word in ("service fee", "service charge", "traveller fee", "traveler fee")):
        return "service_fee"
    if any(word in low for word in (
        "night", "stay", "accommodation", "room price", "base price", "property rental",
    )):
        return "accommodation"
    if any(word in low for word in (
        "resort fee", "destination fee", "mandatory fee", "property fee", "booking fee",
        "administration fee", "management fee",
    )):
        return "mandatory_fee"
    if low in {"total", "price", "total price", "trip total", "due now", "final price"}:
        return "total"
    return None


def _price_error(
    platform: str,
    ref: PropertyRef,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
    notes: str,
    *,
    source_url: str | None = None,
) -> PriceResult:
    return PriceResult(
        site=platform,
        ref=ref,
        outcome=Outcome.ERROR,
        checkin=checkin,
        checkout=checkout,
        guests=guests,
        currency=currency,
        notes=notes,
        source_url=source_url,
    )


def _diagnostic_summary_error(
    platform: str,
    ref: PropertyRef,
    snapshot: dict,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
    source_url: str,
) -> PriceResult | None:
    """Explain a captured summary without promoting it to a quote.

    Vrbo can render a compact ``$466 for 8 nights All fees included`` summary
    even when EUR was requested.  The dollar symbol is not an ISO currency and
    the bundled fees/taxes are not itemised, so the evidence remains unusable.
    """
    diagnostics = snapshot.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return None
    texts = [
        str(item.get("text") or "")
        for item in diagnostics.get("scoped_price_elements") or []
        if isinstance(item, dict)
    ]
    matches = []
    for text in texts:
        match = re.search(
            r"\$\s*([0-9][0-9,]*(?:\.\d{2})?)\s+for\s+(\d+)\s+nights?"
            r".*?all fees included",
            text,
            re.IGNORECASE,
        )
        if match:
            matches.append((match.group(1), int(match.group(2))))
    if len(matches) != 1:
        return None
    displayed, captured_nights = matches[0]
    requested_nights = (checkout - checkin).days
    if captured_nights != requested_nights:
        notes = (
            f"{_display_name(platform)} rendered summary has a stay-length mismatch: "
            f"captured {captured_nights} nights, requested {requested_nights}"
        )
    else:
        notes = (
            f"{_display_name(platform)} rendered an ambiguous dollar-denominated "
            f"{displayed} total despite requested {currency}, and 'all fees included' "
            "does not separately quantify fees or taxes; comparable price withheld"
        )
    return _price_error(
        platform,
        ref,
        checkin,
        checkout,
        guests,
        currency,
        notes,
        source_url=source_url,
    )


def parse_expedia_group_pricing_snapshot(
    platform: str,
    ref: PropertyRef,
    snapshot: dict,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
) -> PriceResult:
    """Parse an exact, structured Vrbo or Expedia checkout snapshot."""
    if platform not in _PLATFORMS:
        raise ValueError("unsupported Expedia Group pricing platform")
    name = _display_name(platform)
    currency = currency.upper()
    expected_url = expedia_group_quote_url(
        platform, ref, checkin, checkout, guests, currency
    )
    source_url = str(snapshot.get("quote_url") or expected_url)
    error = lambda notes: _price_error(
        platform, ref, checkin, checkout, guests, currency, notes,
        source_url=source_url,
    )

    if snapshot.get("snapshot_version") != EXPEDIA_GROUP_PRICING_SNAPSHOT_VERSION:
        return error(f"{name} checkout snapshot version is unsupported")
    expected = {
        "platform": platform,
        "canonical_url": canonical_platform_url(platform, ref.url),
        "quote_url": expected_url,
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "guests": guests,
        "currency": currency,
    }
    for key, expected_value in expected.items():
        actual = snapshot.get(key)
        if key == "currency" and isinstance(actual, str):
            actual = actual.upper()
        if actual != expected_value:
            return error(
                f"{name} checkout snapshot request mismatch for {key}: "
                f"expected {expected_value!r}, got {actual!r}"
            )

    outcome = str(snapshot.get("outcome") or "").casefold()
    if outcome == "unavailable":
        return PriceResult(
            site=platform,
            ref=ref,
            outcome=Outcome.UNAVAILABLE,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            currency=currency,
            source_url=source_url,
            notes=str(snapshot.get("outcome_detail") or f"{name} checkout reports unavailable"),
        )
    if outcome == "min_stay":
        minimum = snapshot.get("minimum_stay_nights")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
            return error(f"{name} minimum-stay evidence is missing a valid night count")
        if minimum <= (checkout - checkin).days:
            return error(f"{name} minimum-stay evidence contradicts the requested stay length")
        return PriceResult(
            site=platform,
            ref=ref,
            outcome=Outcome.MIN_STAY,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            currency=currency,
            source_url=source_url,
            notes=f"{name} requires a minimum stay of {minimum} nights",
        )
    if outcome == "unknown":
        diagnostic_error = _diagnostic_summary_error(
            platform,
            ref,
            snapshot,
            checkin,
            checkout,
            guests,
            currency,
            source_url,
        )
        if diagnostic_error is not None:
            return diagnostic_error
    if outcome != "available":
        return error(f"{name} checkout snapshot has no supported availability outcome")

    displayed_total = _strict_amount(snapshot.get("displayed_total"))
    if displayed_total is None or displayed_total < 0:
        return error(f"{name} checkout snapshot is missing a valid displayed total")
    raw_items = snapshot.get("line_items")
    if not isinstance(raw_items, list) or not raw_items:
        return error(f"{name} checkout snapshot is missing explicit itemisation")

    totals = {
        "accommodation": 0.0,
        "discount": 0.0,
        "cleaning": 0.0,
        "service_fee": 0.0,
        "mandatory_fee": 0.0,
        "tax": 0.0,
        "deposit": 0.0,
    }
    lines: list[PriceLineItem] = []
    included_sum = 0.0
    accommodation_lines = 0
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            return error(f"{name} checkout line {index} is not an object")
        label = str(raw.get("label") or "").strip()
        amount = _strict_amount(raw.get("amount"))
        included = raw.get("included_in_displayed_total")
        raw_currency = raw.get("currency")
        line_currency = str(raw_currency).upper() if isinstance(raw_currency, str) else ""
        if not label or amount is None:
            return error(f"{name} checkout line {index} has a missing label or amount")
        if not line_currency:
            return error(f"{name} checkout line {label!r} has no explicit currency")
        if line_currency != currency:
            return error(f"{name} checkout line {label!r} uses {line_currency}, expected {currency}")
        if not isinstance(included, bool):
            return error(
                f"{name} checkout line {label!r} does not state whether it is in the displayed total"
            )
        category = _line_category(label)
        if category is None:
            return error(f"{name} checkout contains an unknown monetary line: {label!r}")
        if category == "total":
            return error(f"{name} total must be recorded separately from component lines")
        if category == "discount":
            if amount >= 0:
                return error(f"{name} discount line {label!r} is not signed negative")
            totals[category] += -amount
        else:
            if amount < 0:
                return error(f"{name} checkout line {label!r} has an invalid negative amount")
            totals[category] += amount
        if category == "deposit" and raw.get("refundable") is not True:
            return error(f"{name} deposit line {label!r} is not explicitly marked refundable")
        if category == "accommodation":
            accommodation_lines += 1
        if included:
            included_sum += amount
        lines.append(PriceLineItem(
            label=label,
            amount=round(amount, 2),
            category=category,
            currency=currency,
            included_in_displayed_total=included,
        ))

    if accommodation_lines == 0:
        return error(f"{name} checkout itemisation has no accommodation line")
    difference = round(included_sum - displayed_total, 2)
    if abs(difference) > _RECONCILIATION_TOLERANCE:
        return error(
            f"{name} checkout arithmetic does not reconcile: included lines "
            f"{included_sum:.2f}, displayed total {displayed_total:.2f}, "
            f"difference {difference:+.2f}"
        )

    breakdown = PriceBreakdown(
        nightly_total=round(totals["accommodation"], 2),
        discount=round(totals["discount"], 2),
        cleaning=round(totals["cleaning"], 2),
        service_fee=round(totals["service_fee"], 2),
        other_mandatory_fees=round(totals["mandatory_fee"], 2),
        tourist_tax=round(totals["tax"], 2),
        deposit=round(totals["deposit"], 2),
    )
    refundable = snapshot.get("refundable")
    if refundable is not None and not isinstance(refundable, bool):
        return error(f"{name} checkout refundable flag is invalid")
    cancellation_type = snapshot.get("cancellation_type")
    if cancellation_type is not None and not isinstance(cancellation_type, str):
        return error(f"{name} checkout cancellation type is invalid")
    return PriceResult(
        site=platform,
        ref=ref,
        outcome=Outcome.PRICED,
        checkin=checkin,
        checkout=checkout,
        guests=guests,
        currency=currency,
        comparable_total=comparable_total(breakdown),
        headline_total=round(displayed_total, 2),
        breakdown=breakdown,
        line_items=lines,
        reconciliation_difference=difference,
        refundable=refundable,
        cancellation_type=cancellation_type,
        source_url=source_url,
        notes=f"{name} checkout arithmetic reconciled from explicit component lines",
    )


def _capture_diagnostic_snapshot(
    page,
    *,
    platform: str,
    ref: PropertyRef,
    quote_url: str,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
    body_text: str,
) -> dict:
    """Capture bounded public evidence without promoting visible text to price."""
    name = _display_name(platform)
    low = " ".join(body_text.casefold().split())
    outcome = "unknown"
    outcome_detail = None
    minimum_stay_nights = None
    unavailable_markers = (
        "not available for your dates",
        "no availability for these dates",
        "sold out for your dates",
        "we are sold out",
        "property is unavailable",
    )
    if any(marker in low for marker in unavailable_markers):
        outcome = "unavailable"
        outcome_detail = f"{name} checkout page reports unavailable"
    else:
        minimum_match = re.search(
            r"minimum (?:length of )?stay(?: is| of)?\s*(\d+)\s*nights?",
            low,
        )
        if minimum_match:
            outcome = "min_stay"
            minimum_stay_nights = int(minimum_match.group(1))

    try:
        scoped_elements = page.locator(
            '[data-stid*="price"], [data-stid*="fee"], [data-stid*="tax"], '
            '[data-stid*="total"], [data-testid*="price"], [data-testid*="fee"], '
            '[data-testid*="tax"], [data-testid*="total"], '
            '[class*="price"], [class*="Price"]'
        ).evaluate_all(
            r"""els => els.slice(0, 80).map(el => ({
                stid: (el.getAttribute('data-stid') || '').slice(0, 120),
                testid: (el.getAttribute('data-testid') || '').slice(0, 120),
                role: (el.getAttribute('role') || '').slice(0, 80),
                text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 500),
                attributes: Object.fromEntries(Array.from(el.attributes)
                    .filter(attr => /^(?:aria-label|title)$/i.test(attr.name)
                        || /(?:price|tax|total|fee|currency|amount)/i.test(attr.name))
                    .slice(0, 20)
                    .map(attr => [attr.name.slice(0, 120), attr.value.slice(0, 500)]))
            })).filter(item => item.text)"""
        )
    except Exception:
        scoped_elements = []

    price_disclosures = []
    try:
        triggers = page.locator(
            '[data-stid="product-price-summary"] button, '
            '[data-stid="product-price-summary"] a, '
            '[data-stid="product-price-summary"][role="button"], '
            'button[data-stid="product-price-summary"]'
        )
        if triggers.count() > 0:
            trigger = triggers.nth(0)
            try:
                trigger_info = trigger.evaluate(
                    r"""el => ({
                        stid: (el.getAttribute('data-stid') || '').slice(0, 120),
                        testid: (el.getAttribute('data-testid') || '').slice(0, 120),
                        role: (el.getAttribute('role') || '').slice(0, 80),
                        text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 500)
                    })"""
                )
            except Exception:
                trigger_info = {}
            trigger.click(timeout=1800)
            page.wait_for_timeout(350)
            disclosures = page.locator(
                '[role="dialog"], [data-stid*="price-breakdown"], '
                '[data-stid*="price-details"], [data-stid*="fee"], '
                '[data-stid*="tax"], [data-stid*="modal"], '
                '[class*="uitk-dialog"], [class*="uitk-sheet"]'
            ).evaluate_all(
                r"""els => els.slice(0, 24).map(el => ({
                    stid: (el.getAttribute('data-stid') || '').slice(0, 120),
                    testid: (el.getAttribute('data-testid') || '').slice(0, 120),
                    role: (el.getAttribute('role') || '').slice(0, 80),
                    class_name: (el.getAttribute('class') || '').slice(0, 240),
                    text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 5000),
                    attributes: Object.fromEntries(Array.from(el.attributes)
                        .filter(attr => /^(?:aria-label|title)$/i.test(attr.name)
                            || /(?:price|tax|total|fee|currency|amount)/i.test(attr.name))
                        .slice(0, 24)
                        .map(attr => [attr.name.slice(0, 120), attr.value.slice(0, 500)]))
                })).filter(item => item.text)"""
            )
            if disclosures:
                price_disclosures.append({
                    "trigger": trigger_info if isinstance(trigger_info, dict) else {},
                    "elements": [
                        item for item in disclosures if isinstance(item, dict)
                    ][:24],
                })
    except Exception:
        price_disclosures = []

    try:
        currency_indicators = page.locator(
            '[data-stid*="currency"], [data-testid*="currency"], '
            '[aria-label*="currency" i], [title*="currency" i]'
        ).evaluate_all(
            r"""els => els.slice(0, 20).map(el => ({
                stid: (el.getAttribute('data-stid') || '').slice(0, 120),
                testid: (el.getAttribute('data-testid') || '').slice(0, 120),
                text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 500),
                aria_label: (el.getAttribute('aria-label') || '').slice(0, 500),
                title: (el.getAttribute('title') || '').slice(0, 500)
            })).filter(item => item.text || item.aria_label || item.title)"""
        )
    except Exception:
        currency_indicators = []

    return {
        "snapshot_version": EXPEDIA_GROUP_PRICING_SNAPSHOT_VERSION,
        "platform": platform,
        "canonical_url": canonical_platform_url(platform, ref.url),
        "quote_url": quote_url,
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "guests": guests,
        "currency": currency,
        "outcome": outcome,
        "outcome_detail": outcome_detail,
        "minimum_stay_nights": minimum_stay_nights,
        "displayed_total": None,
        "line_items": [],
        "refundable": None,
        "cancellation_type": None,
        "evidence_source": "rendered_public_expedia_group_checkout",
        "diagnostics": {
            "capture_version": 2,
            "scoped_price_elements": [
                {
                    "stid": str(item.get("stid") or ""),
                    "testid": str(item.get("testid") or ""),
                    "role": str(item.get("role") or ""),
                    "text": str(item.get("text") or ""),
                    "attributes": {
                        str(key)[:120]: str(value)[:500]
                        for key, value in (item.get("attributes") or {}).items()
                    } if isinstance(item.get("attributes"), dict) else {},
                }
                for item in scoped_elements
                if isinstance(item, dict)
            ][:80],
            "price_disclosures": price_disclosures[:1],
            "currency_indicators": [
                item for item in currency_indicators if isinstance(item, dict)
            ][:20],
        },
    }


def get_expedia_group_price(
    session: BrowserSession,
    platform: str,
    ref: PropertyRef,
    checkin: date,
    checkout: date,
    guests: int,
) -> PriceResult:
    """Replay or perform one governed diagnostic checkout navigation."""
    currency = str(ref.extra.get("quote_currency") or "EUR").upper()
    if not session.offline and not ref.extra.get("expedia_group_pricing_capture_enabled"):
        return _price_error(
            platform, ref, checkin, checkout, guests, currency,
            f"{_display_name(platform)} pricing is locked until explicitly authorised",
            source_url=ref.url,
        )
    quote_url = expedia_group_quote_url(
        platform, ref, checkin, checkout, guests, currency
    )
    if session.offline:
        snapshot = session.load_snapshot(platform, "pricing", quote_url)
        return parse_expedia_group_pricing_snapshot(
            platform, ref, snapshot, checkin, checkout, guests, currency
        )
    session.before_navigation(platform, "pricing")
    started = time.monotonic()
    page = session.new_page()
    recorded = False
    try:
        response = page.goto(quote_url, wait_until="domcontentloaded", timeout=25000)
        BrowserSession.decline_cookies(page)
        page.wait_for_timeout(1800)
        title = page.title()
        body_text = page.inner_text("body")
        if BrowserSession.is_bot_challenge(f"{title}\n{body_text}"):
            session.record_navigation(
                platform, "pricing", (time.monotonic() - started) * 1000,
                outcome="browser_challenge", challenge=True,
            )
            recorded = True
            raise RequestDeferred(
                f"DEFERRED — {_display_name(platform)} challenge detected; persistent cooldown opened"
            )
        status = response.status if response is not None else None
        if status is not None and status >= 400:
            session.record_navigation(
                platform, "pricing", (time.monotonic() - started) * 1000,
                outcome=f"browser_http_{status}",
            )
            recorded = True
            return _price_error(
                platform, ref, checkin, checkout, guests, currency,
                f"{_display_name(platform)} checkout page returned HTTP {status}",
                source_url=quote_url,
            )
        snapshot = _capture_diagnostic_snapshot(
            page,
            platform=platform,
            ref=ref,
            quote_url=quote_url,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            currency=currency,
            body_text=body_text,
        )
        session.record_navigation(platform, "pricing", (time.monotonic() - started) * 1000)
        recorded = True
        session.save_snapshot(platform, "pricing", quote_url, snapshot)
        return parse_expedia_group_pricing_snapshot(
            platform, ref, snapshot, checkin, checkout, guests, currency
        )
    except RequestDeferred:
        raise
    except Exception as error:
        if not recorded:
            session.record_navigation(
                platform, "pricing", (time.monotonic() - started) * 1000,
                outcome=f"browser_error:{type(error).__name__}",
            )
        return _price_error(
            platform, ref, checkin, checkout, guests, currency,
            f"{_display_name(platform)} pricing capture failed: {error}",
            source_url=quote_url,
        )
    finally:
        page.close()
