"""Booking.com identity and fail-closed pricing evidence.

Phase 6 deliberately enables identity before pricing. A Booking.com page may
only become a candidate ``PropertyRef`` when the rendered public page yields a
real title plus at least two property-gallery images. Live captures retain only
extracted public property fields; raw HTML, scripts, cookies, request headers
and session identifiers are not stored.

Pricing snapshots are keyed to the exact listing, dates, guests and currency.
The parser accepts only explicit component lines whose signed arithmetic
reconciles to an explicit displayed total. Live capture remains separately
locked by the CLI until the offline foundation is approved.
"""
from __future__ import annotations

import html
import json
import math
import re
import time
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from parity.adapters.base import NeedsBrowser
from parity.adapters.browser.browser_session import BrowserSession
from parity.core.matching import find_licence_numbers, registration_tokens
from parity.core.models import (
    Outcome,
    PriceBreakdown,
    PriceLineItem,
    PriceResult,
    PropertyRef,
)
from parity.core.pricing import comparable_total
from parity.http_control import RequestDeferred


_BOOKING_HOST_RE = re.compile(r"^(?:www\.)?booking\.com$", re.IGNORECASE)
_BSTATIC_IMAGE_RE = re.compile(
    r"https://[^\s\"'<>]*bstatic\.com/xdata/images/hotel/[^\s\"'<>]+",
    re.IGNORECASE,
)
_PROPERTY_TYPES = {
    "hotel",
    "lodgingbusiness",
    "resort",
    "villa",
    "house",
    "apartment",
    "vacationrental",
}
BOOKING_PRICING_SNAPSHOT_VERSION = 1
_RECONCILIATION_TOLERANCE = 0.02
_MAX_BOOKING_SOURCE_BYTES = 12 * 1024 * 1024


# Phrases Booking uses when the requested stay cannot be booked.  Missing
# one leaves the page "unknown" and drops Booking for the whole check (live:
# 1024817), so the list covers the wordings seen on property pages.
_BOOKING_UNAVAILABLE_MARKERS = (
    "not available for your dates",
    "no availability for your dates",
    "no availability on our site",
    "have no availability here",
    "no rooms available",
    "not available on our site",
    "sold out on our site",
    "no longer available",
)
_BOOKING_MINIMUM_STAY_RES = (
    re.compile(r"minimum (?:length of )?stay(?: requirement)?(?: is| of)?[:\s]*(\d+)\s*nights?"),
    re.compile(r"min\.? stay[:\s]*(\d+)\s*nights?"),
    re.compile(r"(?:book|stay|reserve)(?: for)? at least (\d+) nights?"),
)


_EXCERPT_KEYWORDS = (
    "availab", "sold out", "minimum", "min. stay", "no rooms", "select dates",
    "check-in date", "enter your dates", "reserve", "price",
)


def _booking_page_excerpt(body_text: str, width: int = 240) -> str:
    """A short slice of what Booking's page said, for plain error messages."""
    text = " ".join(str(body_text or "").split())
    if not text:
        return ""
    low = text.casefold()
    hits = [low.find(word) for word in _EXCERPT_KEYWORDS if low.find(word) >= 0]
    start = max(0, min(hits) - 60) if hits else 0
    return text[start:start + width]


def _unclear_booking_page_message(snapshot: dict) -> str:
    diagnostics = snapshot.get("diagnostics") or {}
    rates = diagnostics.get("structured_rate_count")
    rows = diagnostics.get("availability_rows")
    reasons = diagnostics.get("structured_rejection_reasons") or []
    excerpt = str(diagnostics.get("page_excerpt") or "").strip()
    parts = ["Booking checkout snapshot has no supported availability outcome"]
    details = []
    if isinstance(rates, int):
        details.append(f"rates read: {rates}")
    if isinstance(rows, list):
        details.append(f"room rows: {len(rows)}")
    if reasons:
        details.append(f"why: {reasons[0]}")
    if details:
        parts.append("(" + "; ".join(details) + ")")
    if excerpt:
        parts.append(f"— page said: \"{excerpt[:200]}\"")
    return " ".join(parts)


def _booking_minimum_stay_match(low: str):
    for pattern in _BOOKING_MINIMUM_STAY_RES:
        found = pattern.search(low)
        if found:
            return found
    return None


_BOOKING_RATES_READY_JS = """() => {
    if (document.querySelector('#hprt-table, .hprt-table, [data-component="hotel/new-rooms-table/HprtTable"]')) return true;
    const text = (document.body && document.body.innerText) || '';
    return /not available for your dates|no availability for your dates|no availability on our site|have no availability here|no rooms available|not available on our site|sold out on our site|no longer available|minimum (length of )?stay|min\\.? stay|at least \\d+ nights/i.test(text);
}"""


def booking_dates_were_dropped(final_url: str, body_text: str) -> bool:
    """True when Booking served the undated property page instead of rates."""
    query = urlsplit(final_url or "").query
    asks_for_dates = "select dates to see this property" in " ".join(body_text.casefold().split())
    return asks_for_dates and "checkin" not in query


def _wait_for_booking_rates(page, timeout_ms: int) -> bool:
    """Wait for the room/rate table or an explicit availability message."""
    try:
        page.wait_for_function(_BOOKING_RATES_READY_JS, timeout=timeout_ms)
        return True
    except Exception:
        return False


def _human_cleared(session, page) -> bool:
    """Hand a Booking challenge to the operator when the session allows it."""
    wait = getattr(session, "wait_for_human", None)
    return bool(wait and wait(page, "Booking.com"))


def canonical_booking_url(url: str) -> str:
    """Remove tracking/session parameters and require a Booking hotel path."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not _BOOKING_HOST_RE.fullmatch(host):
        raise ValueError("Booking.com URL must use the public booking.com host")
    path = re.sub(r"/{2,}", "/", parts.path)
    if not path.startswith("/hotel/") or not path.endswith(".html"):
        raise ValueError("Booking.com identity requires a /hotel/...html property URL")
    return urlunsplit(("https", host, path, "", ""))


def booking_quote_url(
    ref: PropertyRef,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
) -> str:
    """Build the canonical, deterministic Booking checkout-view URL."""
    canonical = canonical_booking_url(ref.url)
    currency = currency.upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Booking quote currency must be a three-letter code")
    if checkout <= checkin:
        raise ValueError("Booking quote checkout must be after checkin")
    if guests <= 0:
        raise ValueError("Booking quote guests must be greater than zero")
    query = urlencode([
        ("checkin", checkin.isoformat()),
        ("checkout", checkout.isoformat()),
        ("group_adults", str(guests)),
        ("group_children", "0"),
        ("no_rooms", "1"),
        ("selected_currency", currency),
    ])
    parts = urlsplit(canonical)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def _strict_amount(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    return amount if math.isfinite(amount) else None


def _embedded_booking_amount(value) -> float | None:
    """Parse Booking's bounded raw amount fields without accepting money text.

    Booking serialises some ``b_raw_value`` fields as JSON numbers and others
    as plain decimal strings.  Only a complete decimal token is accepted here;
    currency symbols, separators, exponents and arbitrary visible text remain
    invalid.
    """
    amount = _strict_amount(value)
    if amount is not None:
        return amount
    if not isinstance(value, str) or not re.fullmatch(
        r"-?(?:0|[1-9]\d*)(?:\.\d+)?", value.strip()
    ):
        return None
    amount = float(value)
    return amount if math.isfinite(amount) else None


def _balanced_json_object(text: str, start: int) -> str | None:
    """Return one JSON object beginning at *start*, respecting string escapes."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _last_json_string_field(text: str, field: str) -> str | None:
    expression = re.compile(
        rf'"{re.escape(field)}"\s*:\s*"([^"\\]{{1,160}}(?:\\.[^"\\]*?)*)"'
    )
    matches = list(expression.finditer(text))
    if not matches:
        return None
    try:
        return json.loads(f'"{matches[-1].group(1)}"')
    except json.JSONDecodeError:
        return None


def _first_mapping(value) -> dict:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def _bounded_booking_charge(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        "label": str(value.get("b_copy") or value.get("b_copy_alt") or "")[:160],
        "label_alt": str(value.get("b_copy_alt") or "")[:160],
        "type": str(value.get("b_type") or "")[:80],
        "raw_value": value.get("b_raw_value"),
        "value": str(
            value.get("b_value") or value.get("b_value_user_currency") or ""
        )[:80],
    }


def extract_booking_source_pricing(source_text: str) -> dict:
    """Extract bounded rate evidence from a Booking HTML/source response."""
    if not isinstance(source_text, str):
        raise ValueError("Booking source must be text")
    source_bytes = len(source_text.encode("utf-8", errors="replace"))
    if source_bytes > _MAX_BOOKING_SOURCE_BYTES:
        raise ValueError(
            f"Booking source exceeds the {_MAX_BOOKING_SOURCE_BYTES // (1024 * 1024)} MiB safety limit"
        )

    text = source_text
    key = '"b_price_breakdown_simplified"'
    if key not in text and "&quot;b_price_breakdown_simplified&quot;" in text:
        text = html.unescape(text)

    rates: list[dict] = []
    failures: list[str] = []
    occurrence_count = 0
    offset = 0
    prior_object_end = 0
    while len(rates) < 16:
        index = text.find(key, offset)
        if index < 0:
            break
        occurrence_count += 1
        colon = text.find(":", index + len(key))
        start = text.find("{", colon + 1) if colon >= 0 else -1
        if start < 0:
            failures.append(f"occurrence {occurrence_count}: missing object start")
            offset = index + len(key)
            continue
        raw = _balanced_json_object(text, start)
        offset = start + 1
        if raw is None:
            failures.append(f"occurrence {occurrence_count}: unterminated JSON object")
            continue
        object_end = start + len(raw)
        context_start = max(prior_object_end, index - 100_000)
        context = text[context_start:index]
        prior_object_end = object_end
        try:
            breakdown = json.loads(raw)
        except json.JSONDecodeError:
            failures.append(f"occurrence {occurrence_count}: invalid JSON object")
            continue
        if not isinstance(breakdown, dict):
            failures.append(f"occurrence {occurrence_count}: breakdown is not an object")
            continue

        total = _first_mapping(breakdown.get("b_total_price"))
        net = _first_mapping(breakdown.get("b_net_room_price"))
        included_sum = _first_mapping(breakdown.get("b_sum_included_charges"))
        charges = breakdown.get("b_included_charges")
        all_included = (
            breakdown.get("b_charges_info_copy") or {}
        ).get("b_is_all_included")
        rates.append({
            "block_id": _last_json_string_field(context, "b_block_id"),
            "cancellation_type": _last_json_string_field(
                context, "b_cancellation_type"
            ),
            "total": total.get("b_raw_value_user_currency"),
            "total_value": str(total.get("b_value_user_currency") or "")[:80],
            "net_room": net.get("b_raw_value"),
            "net_room_value": str(net.get("b_value") or "")[:80],
            "included_sum": included_sum.get("b_raw_value_user_currency"),
            "included_charges": [
                _bounded_booking_charge(charge)
                for charge in charges[:24]
            ] if isinstance(charges, list) else None,
            "excluded_charges_amount": breakdown.get("b_excluded_charges_amount"),
            "excluded_count": len(breakdown.get("b_excluded_charges") or [])
                if isinstance(breakdown.get("b_excluded_charges"), list) else None,
            "conditional_count": len(breakdown.get("b_conditional_charges") or [])
                if isinstance(breakdown.get("b_conditional_charges"), list) else None,
            "nonmandatory_count": len(breakdown.get("b_nonmandatory_charges") or [])
                if isinstance(breakdown.get("b_nonmandatory_charges"), list) else None,
            "extra_addons_count": len(breakdown.get("b_extra_addons") or [])
                if isinstance(breakdown.get("b_extra_addons"), list) else None,
            "all_included": all_included == 1 or all_included == "1",
        })

    normalised = " ".join(html.unescape(text).replace("\xa0", " ").split())
    displays: list[dict] = []
    for match in _BOOKING_EUR_RATE_RE.finditer(normalised):
        subtotal = _embedded_booking_amount(match.group(3).replace(",", ""))
        matched_rates = [
            rate for rate in rates
            if subtotal is not None
            and (amount := _embedded_booking_amount(rate.get("total"))) is not None
            and abs(amount - subtotal) <= _RECONCILIATION_TOLERANCE
        ]
        if len(matched_rates) != 1:
            continue
        rate = matched_rates[0]
        block_id = str(rate.get("block_id") or "")[:120]
        if block_id:
            displays.append({
                "block_id": block_id,
                "cancellation_type": str(rate.get("cancellation_type") or "")[:80],
                "text": match.group(0)[:2000],
            })

    return {
        "rates": rates,
        "rate_displays": displays,
        "diagnostics": {
            "source_bytes": source_bytes,
            "source_occurrence_count": occurrence_count,
            "source_rate_count": len(rates),
            "source_display_count": len(displays),
            "source_failures": failures[:16],
        },
    }


def _booking_line_category(label: str) -> str | None:
    low = " ".join(label.casefold().split())
    if any(word in low for word in (
        "discount", "saving", "promotion", "genius", "booking.com pays",
    )):
        return "discount"
    if any(word in low for word in ("tourist tax", "city tax", "occupancy tax", "vat", "tax")):
        return "tax"
    # Government tourism charges that Booking labels as a "fee" or "levy"
    # (e.g. Miami-Dade's "6 % Tourism fee"): taxes, not host fees.
    if any(word in low for word in (
        "tourism fee", "tourist fee", "tourism levy", "tourist levy",
    )):
        return "tax"
    if "deposit" in low:
        return "deposit"
    if "clean" in low:
        return "cleaning"
    if "service fee" in low or "service charge" in low:
        return "service_fee"
    if any(word in low for word in ("night", "stay", "accommodation", "room price", "base price")):
        return "accommodation"
    if any(word in low for word in (
        "resort fee",
        "destination fee",
        "mandatory fee",
        "property fee",
        "air conditioning fee",
    )):
        return "mandatory_fee"
    if low in {"total", "price", "total price", "final price"}:
        return "total"
    return None


def _booking_price_error(
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
        site="booking",
        ref=ref,
        outcome=Outcome.ERROR,
        checkin=checkin,
        checkout=checkout,
        guests=guests,
        currency=currency,
        notes=notes,
        source_url=source_url,
    )


_BOOKING_EUR_RATE_RE = re.compile(
    r"€\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})\s*[×x]\s*(\d+)\s+nights?\s+"
    r"€\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2}).*?"
    r"Booking\.com pays\s*-\s*€\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2}).*?"
    r"\bTotal\s+€\s*([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2})",
    re.IGNORECASE | re.DOTALL,
)


def _diagnostic_booking_rate(
    ref: PropertyRef,
    snapshot: dict,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
    source_url: str,
) -> PriceResult | None:
    """Preserve exact room-table evidence while withholding comparability.

    Booking sometimes exposes an exact accommodation subtotal, platform-funded
    reduction and displayed total while stating only that taxes and charges are
    included.  That arithmetic is useful audit evidence, but it cannot become a
    comparable price until the excluded tax categories are quantified.
    """
    if currency != "EUR":
        return None
    diagnostics = snapshot.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return None
    texts = []
    for item in diagnostics.get("scoped_price_texts") or []:
        if not isinstance(item, dict):
            continue
        class_name = str(item.get("class_name") or "")
        text = str(item.get("text") or "")
        if "prco-price-area-popover" in class_name and text:
            texts.append(text)
    matches = []
    for text in texts:
        match = _BOOKING_EUR_RATE_RE.search(text)
        if match:
            matches.append(match.groups())
    if len(matches) != 1:
        return None

    nightly_display, captured_nights, subtotal, discount, displayed_total = matches[0]
    captured_nights = int(captured_nights)
    requested_nights = (checkout - checkin).days
    if captured_nights != requested_nights:
        return _booking_price_error(
            ref, checkin, checkout, guests, currency,
            "Booking room-table evidence has a stay-length mismatch",
            source_url=source_url,
        )
    amounts = [
        float(value.replace(",", ""))
        for value in (nightly_display, subtotal, discount, displayed_total)
    ]
    nightly_display_amount, subtotal_amount, discount_amount, total_amount = amounts
    nightly_difference = round(
        nightly_display_amount * captured_nights - subtotal_amount,
        2,
    )
    if abs(nightly_difference) > _RECONCILIATION_TOLERANCE:
        return _booking_price_error(
            ref, checkin, checkout, guests, currency,
            "Booking room-table nightly arithmetic does not reconcile: "
            f"nightly {nightly_display_amount:.2f} × {captured_nights}, "
            f"accommodation {subtotal_amount:.2f}, difference {nightly_difference:+.2f}",
            source_url=source_url,
        )
    difference = round(subtotal_amount - discount_amount - total_amount, 2)
    if abs(difference) > _RECONCILIATION_TOLERANCE:
        return _booking_price_error(
            ref, checkin, checkout, guests, currency,
            "Booking room-table evidence does not reconcile: "
            f"accommodation {subtotal_amount:.2f}, reduction {discount_amount:.2f}, "
            f"displayed total {total_amount:.2f}, difference {difference:+.2f}",
            source_url=source_url,
        )

    joined_rows = " ".join(
        str(item.get("text") or "")
        for item in diagnostics.get("availability_rows") or []
        if isinstance(item, dict)
    )
    if "includes taxes and charges" not in joined_rows.casefold():
        return None
    tax_popovers = diagnostics.get("tax_charge_popovers")
    if tax_popovers:
        # A future shape-specific parser may classify an explicit tax popover.
        # Until then, do not guess from unseen or unfamiliar disclosure text.
        return None

    cancellation_type = (
        "Non-refundable" if "non-refundable" in joined_rows.casefold() else None
    )
    lines = [
        PriceLineItem(
            label=f"{captured_nights} nights accommodation",
            amount=round(subtotal_amount, 2),
            category="accommodation",
            currency=currency,
            included_in_displayed_total=True,
        ),
        PriceLineItem(
            label="Booking.com pays",
            amount=round(-discount_amount, 2),
            category="discount",
            currency=currency,
            included_in_displayed_total=True,
        ),
    ]
    return PriceResult(
        site="booking",
        ref=ref,
        outcome=Outcome.INCOMPLETE_EVIDENCE,
        checkin=checkin,
        checkout=checkout,
        guests=guests,
        currency=currency,
        comparable_total=None,
        headline_total=round(total_amount, 2),
        breakdown=PriceBreakdown(
            nightly_total=round(subtotal_amount, 2),
            discount=round(discount_amount, 2),
        ),
        line_items=lines,
        reconciliation_difference=difference,
        refundable=False if cancellation_type else None,
        cancellation_type=cancellation_type,
        source_url=source_url,
        notes=(
            "Booking rate arithmetic reconciles, but included taxes and charges "
            "are not separately quantified; comparable price withheld. "
            f"Displayed nightly rate {currency} {nightly_display_amount:.2f}."
        ),
    )


_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_ROOM_BEDROOMS_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d{1,2})[\s-]*bed(?:room)?s?\b",
    re.IGNORECASE,
)
_GENERIC_ROOM_WORDS = {
    "villa", "apartment", "house", "room", "rooms", "bedroom", "bedrooms", "with",
    "and", "the", "view", "suite", "studio", "deluxe", "superior", "standard",
    "family", "private", "pool", "sea", "garden", "balcony", "terrace", "home",
}


def _booking_room_id(block_id: str) -> str:
    head = str(block_id or "").split("_", 1)[0]
    return head if head.isdigit() else ""


def _room_bedrooms(name: str) -> int | None:
    found = _ROOM_BEDROOMS_RE.search(name or "")
    if not found:
        return 0 if re.search(r"\bstudio\b", name or "", re.IGNORECASE) else None
    token = found.group(1).casefold()
    return _NUMBER_WORDS.get(token) or (int(token) if token.isdigit() else None)


def choose_booking_room(
    rates: list[dict], room_info: dict[str, dict], profile, guests: int
) -> dict:
    """Narrow Booking rates to the one room type that matches the subject.

    One room on the page: all rates, unchanged.  Several rooms: score each by
    bedroom count in its name, maximum guests and distinctive title words
    against the TravelStaytion listing; only a clear winner is used.  Ties or
    no evidence return no rates and a plain reason listing the room names.
    """
    groups: dict[str, list[dict]] = {}
    for rate in rates:
        groups.setdefault(_booking_room_id(str(rate.get("block_id") or "")), []).append(rate)
    known = [room_id for room_id in groups if room_id]
    if len(known) <= 1:
        only = known[0] if known else ""
        return {"rates": list(rates), "name": (room_info.get(only) or {}).get("name") or "", "problem": ""}

    profile = profile if isinstance(profile, dict) else {}
    bedrooms = profile.get("bedrooms")
    capacity = profile.get("capacity")
    title_words = {
        word for word in re.findall(r"[a-zα-ω]{4,}", str(profile.get("title") or "").casefold())
        if word not in _GENERIC_ROOM_WORDS
    }
    scored = []
    for room_id, room_rates in groups.items():
        if not room_id:
            continue
        info = room_info.get(room_id) or {}
        name = str(info.get("name") or "")
        max_persons = info.get("max_persons")
        score = 0
        room_bedrooms = _room_bedrooms(name)
        if isinstance(bedrooms, int) and bedrooms > 0 and room_bedrooms is not None:
            score += 3 if room_bedrooms == bedrooms else -3
        if isinstance(max_persons, int) and max_persons:
            if max_persons < guests:
                score -= 5
            elif isinstance(capacity, int) and capacity > 0:
                score += 2 if max_persons == capacity else (-1 if abs(max_persons - capacity) > 2 else 0)
        name_words = set(re.findall(r"[a-zα-ω]{4,}", name.casefold())) - _GENERIC_ROOM_WORDS
        score += min(2, len(title_words & name_words))
        scored.append((score, room_id, name, room_rates))
    scored.sort(key=lambda item: item[0], reverse=True)
    best, runner_up = scored[0], scored[1]
    if best[0] > 0 and best[0] > runner_up[0]:
        return {"rates": best[3], "name": best[2], "problem": ""}
    names = "; ".join(item[2] or f"room {item[1]}" for item in scored[:6])
    wanted = []
    if isinstance(bedrooms, int) and bedrooms > 0:
        wanted.append(f"{bedrooms} bedrooms")
    if isinstance(capacity, int) and capacity > 0:
        wanted.append(f"{capacity} guests")
    return {
        "rates": [],
        "name": "",
        "problem": (
            f"Booking lists {len(groups)} room types ({names}) and none clearly "
            "matches this listing" + (f" ({', '.join(wanted)})" if wanted else "")
        ),
    }


def booking_rate_options(rates: list[dict], selected: dict | None) -> list[dict]:
    """All distinct rates on the page, cheapest first, selected one marked.

    The comparison uses the like-for-like refundable rate when one exists,
    but a cheaper non-refundable rate is real information for the operator
    (e.g. 337856: EUR 1,334 non-refundable vs EUR 1,460 free cancellation).
    """
    options: list[dict] = []
    seen: set[tuple] = set()
    for rate in rates:
        if not isinstance(rate, dict):
            continue
        total = _embedded_booking_amount(rate.get("total"))
        if total is None:
            continue
        terms = str(rate.get("cancellation_type") or "").strip()
        lowered = terms.casefold().replace("-", "_")
        if "free_cancellation" in lowered or "free cancellation" in lowered:
            label = "Free cancellation"
        elif "non_refundable" in lowered or "non refundable" in lowered:
            label = "Non-refundable"
        else:
            label = terms.replace("_", " ").capitalize() or "Terms not shown"
        key = (round(total, 2), label)
        if key in seen:
            continue
        seen.add(key)
        options.append({
            "total": round(total, 2),
            "cancellation": label,
            "selected": rate is selected,
        })
    options.sort(key=lambda item: item["total"])
    return options[:8]


def parse_booking_pricing_snapshot(
    ref: PropertyRef,
    snapshot: dict,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
) -> PriceResult:
    """Turn one exact structured checkout snapshot into an auditable quote.

    The snapshot is deliberately smaller than a rendered page. It contains
    request identity, availability, explicit monetary components, whether each
    component participates in the displayed total, and the displayed total.
    Unknown or ambiguous evidence returns ``ERROR`` with no comparable amount.
    """
    currency = currency.upper()
    source_url = str(snapshot.get("quote_url") or booking_quote_url(
        ref, checkin, checkout, guests, currency
    ))
    error = lambda notes: _booking_price_error(
        ref, checkin, checkout, guests, currency, notes, source_url=source_url
    )

    if snapshot.get("snapshot_version") != BOOKING_PRICING_SNAPSHOT_VERSION:
        return error("Booking checkout snapshot version is unsupported")
    expected = {
        "canonical_url": canonical_booking_url(ref.url),
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "guests": guests,
        "currency": currency,
        "quote_url": booking_quote_url(ref, checkin, checkout, guests, currency),
    }
    for key, expected_value in expected.items():
        actual = snapshot.get(key)
        if key == "currency" and isinstance(actual, str):
            actual = actual.upper()
        if actual != expected_value:
            return error(
                f"Booking checkout snapshot request mismatch for {key}: "
                f"expected {expected_value!r}, got {actual!r}"
            )

    outcome = str(snapshot.get("outcome") or "").casefold()
    if outcome == "unavailable":
        return PriceResult(
            site="booking", ref=ref, outcome=Outcome.UNAVAILABLE,
            checkin=checkin, checkout=checkout, guests=guests, currency=currency,
            source_url=source_url,
            notes=str(snapshot.get("outcome_detail") or "Booking checkout reports unavailable"),
        )
    if outcome == "min_stay":
        minimum = snapshot.get("minimum_stay_nights")
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
            return error("Booking minimum-stay evidence is missing a valid night count")
        requested_nights = (checkout - checkin).days
        if minimum <= requested_nights:
            return error(
                "Booking minimum-stay evidence contradicts the requested stay length"
            )
        return PriceResult(
            site="booking", ref=ref, outcome=Outcome.MIN_STAY,
            checkin=checkin, checkout=checkout, guests=guests, currency=currency,
            minimum_stay_nights=minimum,
            source_url=source_url,
            notes=f"Booking requires a minimum stay of {minimum} nights",
        )
    if outcome == "unknown":
        diagnostic_result = _diagnostic_booking_rate(
            ref,
            snapshot,
            checkin,
            checkout,
            guests,
            currency,
            source_url,
        )
        if diagnostic_result is not None:
            return diagnostic_result
    if outcome != "available":
        return error(_unclear_booking_page_message(snapshot))

    displayed_total = _strict_amount(snapshot.get("displayed_total"))
    if displayed_total is None or displayed_total < 0:
        return error("Booking checkout snapshot is missing a valid displayed total")
    raw_items = snapshot.get("line_items")
    if not isinstance(raw_items, list) or not raw_items:
        return error("Booking checkout snapshot is missing explicit itemisation")

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
            return error(f"Booking checkout line {index} is not an object")
        label = str(raw.get("label") or "").strip()
        amount = _strict_amount(raw.get("amount"))
        included = raw.get("included_in_displayed_total")
        raw_currency = raw.get("currency")
        line_currency = str(raw_currency).upper() if isinstance(raw_currency, str) else ""
        if not label or amount is None:
            return error(f"Booking checkout line {index} has a missing label or amount")
        if not line_currency:
            return error(f"Booking checkout line {label!r} has no explicit currency")
        if line_currency != currency:
            return error(
                f"Booking checkout line {label!r} uses {line_currency}, expected {currency}"
            )
        if not isinstance(included, bool):
            return error(
                f"Booking checkout line {label!r} does not state whether it is in the displayed total"
            )
        category = _booking_line_category(label)
        if category is None:
            return error(f"Booking checkout contains an unknown monetary line: {label!r}")
        if category == "total":
            return error("Booking total must be recorded separately from component lines")
        if category == "discount":
            if amount >= 0:
                return error(f"Booking discount line {label!r} is not signed negative")
            totals[category] += -amount
        else:
            if amount < 0:
                return error(f"Booking checkout line {label!r} has an invalid negative amount")
            totals[category] += amount
        if category == "deposit" and raw.get("refundable") is not True:
            return error(
                f"Booking deposit line {label!r} is not explicitly marked refundable"
            )
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
        return error("Booking checkout itemisation has no accommodation line")
    difference = round(included_sum - displayed_total, 2)
    if abs(difference) > _RECONCILIATION_TOLERANCE:
        return error(
            "Booking checkout arithmetic does not reconcile: "
            f"included lines {included_sum:.2f}, displayed total {displayed_total:.2f}, "
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
    comparable = comparable_total(breakdown)
    refundable = snapshot.get("refundable")
    if refundable is not None and not isinstance(refundable, bool):
        return error("Booking checkout refundable flag is invalid")
    cancellation_type = snapshot.get("cancellation_type")
    if cancellation_type is not None and not isinstance(cancellation_type, str):
        return error("Booking checkout cancellation type is invalid")
    return PriceResult(
        site="booking",
        ref=ref,
        outcome=Outcome.PRICED,
        checkin=checkin,
        checkout=checkout,
        guests=guests,
        currency=currency,
        comparable_total=comparable,
        headline_total=round(displayed_total, 2),
        breakdown=breakdown,
        line_items=lines,
        reconciliation_difference=difference,
        refundable=refundable,
        cancellation_type=cancellation_type,
        source_url=source_url,
        notes="Booking structured rate arithmetic reconciled from explicit component lines",
        rate_options=_valid_rate_options(snapshot.get("rate_options")),
        room_name=(str(snapshot.get("room_name") or "").strip()[:160] or None),
    )


def _valid_rate_options(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    options = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        total = item.get("total")
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            continue
        options.append({
            "total": round(float(total), 2),
            "cancellation": str(item.get("cancellation") or "")[:60],
            "selected": item.get("selected") is True,
        })
    return options


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _one_number(text: str, patterns: tuple[str, ...], *, integer: bool) -> int | float | None:
    values: set[float] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                value = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if 0 < value <= 50:
                values.add(value)
    if len(values) != 1:
        return None
    value = next(iter(values))
    return int(value) if integer and value.is_integer() else value


def _lodging_nodes(value) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        raw_types = value.get("@type")
        types = raw_types if isinstance(raw_types, list) else [raw_types]
        if any(str(item).casefold() in _PROPERTY_TYPES for item in types if item):
            found.append(value)
        for child in value.values():
            found.extend(_lodging_nodes(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_lodging_nodes(child))
    return found


def _clean_image_url(value: str) -> str | None:
    value = value.replace(r"\u002F", "/").replace(r"\/", "/")
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not (host == "bstatic.com" or host.endswith(".bstatic.com")):
        return None
    if "/xdata/images/hotel/" not in parts.path:
        return None
    # Booking's public CDN commonly requires its content signature (`k`) and
    # transform selector (`o`).  Retain only those allow-listed image fields;
    # advertising, session and referral parameters are discarded.
    safe_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key in {"k", "o"}
    ]
    return urlunsplit(("https", host, parts.path, urlencode(safe_query), ""))


def _images_from_value(value) -> list[str]:
    raw: list[str] = []
    if isinstance(value, str):
        raw.append(value)
    elif isinstance(value, list):
        for item in value:
            raw.extend(_images_from_value(item))
    elif isinstance(value, dict):
        for key in ("url", "contentUrl", "thumbnailUrl"):
            raw.extend(_images_from_value(value.get(key)))
    return raw


def _build_identity_snapshot(
    *,
    canonical_url: str,
    page_title: str,
    headings: list[str],
    body_text: str,
    html: str,
    json_ld_texts: list[str],
    dom_image_values: list[str],
) -> dict:
    nodes: list[dict] = []
    for raw in json_ld_texts:
        try:
            nodes.extend(_lodging_nodes(json.loads(raw)))
        except (TypeError, ValueError):
            continue
    node = nodes[0] if nodes else {}

    title = str(node.get("name") or "").strip()
    if not title:
        title = next((item.strip() for item in headings if item.strip()), "")
    if not title:
        title = re.split(r"\s+[|–-]\s+Booking\.com", page_title, maxsplit=1)[0].strip()

    address = node.get("address") if isinstance(node.get("address"), dict) else {}
    geo = node.get("geo") if isinstance(node.get("geo"), dict) else {}
    country_value = address.get("addressCountry")
    if isinstance(country_value, dict):
        country_value = country_value.get("name") or country_value.get("addressCountry")

    raw_images: list[str] = []
    raw_images.extend(_images_from_value(node.get("image")))
    raw_images.extend(dom_image_values)
    raw_images.extend(
        match.group(0).rstrip("\\),]}") for match in _BSTATIC_IMAGE_RE.finditer(html)
    )
    gallery: list[str] = []
    seen: set[str] = set()
    for raw in raw_images:
        cleaned = _clean_image_url(str(raw))
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            gallery.append(cleaned)

    amenities: list[str] = []
    raw_amenities = node.get("amenityFeature")
    if isinstance(raw_amenities, list):
        for item in raw_amenities:
            if isinstance(item, dict) and item.get("name"):
                amenities.append(str(item["name"]).strip())

    registrations = list(find_licence_numbers(body_text))
    bedrooms = _one_number(
        body_text,
        (r"\b(\d+)\s+bedrooms?\b", r"\bbedrooms?\s*[:：]\s*(\d+)\b"),
        integer=True,
    )
    bathrooms = _one_number(
        body_text,
        (
            r"\b(\d+(?:\.\d+)?)\s+bathrooms?\b",
            r"\bbathrooms?\s*[:：]\s*(\d+(?:\.\d+)?)\b",
        ),
        integer=False,
    )
    capacity = _one_number(
        body_text,
        (
            r"\bsleeps?\s+(\d+)\b",
            r"\bmax(?:imum)?\s+(?:occupancy|guests?|adults?)\D{0,10}(\d+)\b",
        ),
        integer=True,
    )

    path_parts = [part for part in urlsplit(canonical_url).path.split("/") if part]
    slug = re.sub(
        r"(?:\.[a-z]{2}(?:-[a-z]{2})?)?\.html$",
        "",
        path_parts[-1],
        flags=re.IGNORECASE,
    )
    property_type = None
    raw_type = node.get("@type")
    if isinstance(raw_type, list):
        raw_type = next(
            (item for item in raw_type if str(item).casefold() in _PROPERTY_TYPES),
            None,
        )
    if raw_type:
        property_type = str(raw_type)

    return {
        "snapshot_version": 1,
        "canonical_url": canonical_url,
        "raw_id": slug,
        "title": title or None,
        "registration_ids": registrations,
        # Bounded, normalised licence-shaped tokens so identity can find the
        # subject's id even in a registration format the regex lacks.
        "registration_tokens": sorted(registration_tokens(body_text))[:3000],
        "lat": _as_float(geo.get("latitude")),
        "lng": _as_float(geo.get("longitude")),
        "country": str(country_value).strip() if country_value else None,
        "city": str(address.get("addressLocality") or "").strip() or None,
        "postcode": str(address.get("postalCode") or "").strip() or None,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "capacity": capacity,
        "property_type": property_type,
        "amenities": list(dict.fromkeys(item for item in amenities if item)),
        "gallery": gallery[:80],
        "identity_source": "rendered_public_booking_page",
    }


def _property_from_snapshot(snapshot: dict) -> PropertyRef:
    if snapshot.get("snapshot_version") != 1:
        raise ValueError("Booking.com identity snapshot version is unsupported")
    title = str(snapshot.get("title") or "").strip()
    gallery = [str(item) for item in snapshot.get("gallery") or [] if item]
    if not title:
        raise ValueError("Booking.com identity rejected: property title is missing")
    if len(gallery) < 2:
        raise ValueError(
            "Booking.com identity rejected: fewer than two property-gallery images were extracted"
        )
    registrations = [str(item) for item in snapshot.get("registration_ids") or [] if item]
    return PropertyRef(
        site="booking",
        raw_id=str(snapshot.get("raw_id") or snapshot["canonical_url"]),
        url=str(snapshot["canonical_url"]),
        licence_number=registrations[0] if registrations else None,
        registration_ids=registrations,
        lat=_as_float(snapshot.get("lat")),
        lng=_as_float(snapshot.get("lng")),
        country=snapshot.get("country"),
        city=snapshot.get("city"),
        postcode=snapshot.get("postcode"),
        bedrooms=int(snapshot["bedrooms"]) if snapshot.get("bedrooms") is not None else None,
        bathrooms=_as_float(snapshot.get("bathrooms")),
        capacity=int(snapshot["capacity"]) if snapshot.get("capacity") is not None else None,
        property_type=snapshot.get("property_type"),
        amenities=[str(item) for item in snapshot.get("amenities") or [] if item],
        title=title,
        gallery=gallery,
        extra={
            "identity_source": snapshot.get("identity_source"),
            "snapshot_version": snapshot.get("snapshot_version"),
            "registration_tokens": [
                str(item) for item in snapshot.get("registration_tokens") or [] if item
            ],
        },
    )


def _capture_booking_pricing_snapshot(
    page,
    *,
    ref: PropertyRef,
    quote_url: str,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
    body_text: str,
    source_text: str | None = None,
) -> dict:
    """Capture bounded public pricing evidence, never a rendered page.

    Booking currently embeds an exact ``b_price_breakdown_simplified`` object
    for each dated rate in public listing-page scripts.  We inspect those
    scripts in memory, retain only allow-listed monetary fields and discard the
    script text.  Arbitrary visible money strings remain diagnostics and are
    never promoted to line items.
    """
    low = " ".join(body_text.casefold().split())
    outcome = "unknown"
    outcome_detail = None
    availability_outcome_signal = None
    minimum_stay_nights = None
    if any(marker in low for marker in _BOOKING_UNAVAILABLE_MARKERS):
        outcome = "unavailable"
        outcome_detail = "Booking checkout page reports unavailable"
        availability_outcome_signal = "body_text_unavailable_marker"
    else:
        minimum_match = _booking_minimum_stay_match(low)
        if minimum_match:
            outcome = "min_stay"
            minimum_stay_nights = int(minimum_match.group(1))

    try:
        embedded_capture = page.evaluate(
            """() => {
                const legacy = window.b_price_breakdown_simplified;
                const legacyKeys = legacy && typeof legacy === 'object'
                    ? Object.keys(legacy).slice(0, 50) : [];
                const rates = [];
                const rateDisplays = [];
                const key = '"b_price_breakdown_simplified"';

                function balancedObject(text, start) {
                    let depth = 0;
                    let inString = false;
                    let escaped = false;
                    for (let i = start; i < text.length; i++) {
                        const char = text[i];
                        if (inString) {
                            if (escaped) escaped = false;
                            else if (char === '\\\\') escaped = true;
                            else if (char === '"') inString = false;
                            continue;
                        }
                        if (char === '"') inString = true;
                        else if (char === '{') depth += 1;
                        else if (char === '}') {
                            depth -= 1;
                            if (depth === 0) return text.slice(start, i + 1);
                        }
                    }
                    return null;
                }

                function lastStringField(text, field) {
                    const expression = new RegExp('"' + field + '"\\\\s*:\\\\s*"([^"]{1,160})"', 'g');
                    let found = null;
                    for (const match of text.matchAll(expression)) found = match[1];
                    return found;
                }

                function strictNumber(value) {
                    if (typeof value === 'number' && Number.isFinite(value)) return value;
                    if (typeof value !== 'string'
                            || !/^-?(?:0|[1-9]\\d*)(?:\\.\\d+)?$/.test(value.trim())) return null;
                    const parsed = Number(value);
                    return Number.isFinite(parsed) ? parsed : null;
                }

                for (const script of Array.from(document.scripts).slice(0, 250)) {
                    const text = script.textContent || '';
                    if (!text.includes(key) || text.length > 3000000) continue;
                    let offset = 0;
                    while (rates.length < 16) {
                        const index = text.indexOf(key, offset);
                        if (index < 0) break;
                        const colon = text.indexOf(':', index + key.length);
                        const start = colon < 0 ? -1 : text.indexOf('{', colon + 1);
                        if (start < 0) break;
                        const raw = balancedObject(text, start);
                        offset = start + 1;
                        if (!raw) continue;
                        let breakdown;
                        try { breakdown = JSON.parse(raw); } catch (_) { continue; }
                        const context = text.slice(Math.max(0, index - 20000), index);
                        const first = value => Array.isArray(value) && value.length
                            && value[0] && typeof value[0] === 'object' ? value[0] : {};
                        const total = first(breakdown.b_total_price);
                        const net = first(breakdown.b_net_room_price);
                        const sumIncluded = first(breakdown.b_sum_included_charges);
                        const charge = value => ({
                            label: String(value.b_copy || value.b_copy_alt || '').slice(0, 160),
                            label_alt: String(value.b_copy_alt || '').slice(0, 160),
                            type: String(value.b_type || '').slice(0, 80),
                            raw_value: value.b_raw_value,
                            value: String(value.b_value || value.b_value_user_currency || '').slice(0, 80),
                        });
                        rates.push({
                            block_id: lastStringField(context, 'b_block_id'),
                            cancellation_type: lastStringField(context, 'b_cancellation_type'),
                            total: strictNumber(total.b_raw_value_user_currency),
                            total_value: String(total.b_value_user_currency || '').slice(0, 80),
                            net_room: strictNumber(net.b_raw_value),
                            net_room_value: String(net.b_value || '').slice(0, 80),
                            included_sum: strictNumber(sumIncluded.b_raw_value_user_currency),
                            included_charges: Array.isArray(breakdown.b_included_charges)
                                ? breakdown.b_included_charges.slice(0, 24).map(charge) : [],
                            excluded_charges_amount: strictNumber(breakdown.b_excluded_charges_amount),
                            excluded_count: Array.isArray(breakdown.b_excluded_charges)
                                ? breakdown.b_excluded_charges.length : 0,
                            conditional_count: Array.isArray(breakdown.b_conditional_charges)
                                ? breakdown.b_conditional_charges.length : 0,
                            nonmandatory_count: Array.isArray(breakdown.b_nonmandatory_charges)
                                ? breakdown.b_nonmandatory_charges.length : 0,
                            extra_addons_count: Array.isArray(breakdown.b_extra_addons)
                                ? breakdown.b_extra_addons.length : 0,
                            all_included: breakdown.b_charges_info_copy
                                && breakdown.b_charges_info_copy.b_is_all_included === 1,
                        });
                    }
                }
                let currentRoomName = '';
                for (const row of Array.from(document.querySelectorAll(
                        '#hprt-table tr, .hprt-table tr')).slice(0, 40)) {
                    // The room-type cell spans all rate rows of that room, so
                    // carry its name forward to the rows below it.
                    const roomNameEl = row.querySelector(
                        '.hprt-roomtype-icon-link, .hprt-roomtype-link, .hprt-roomtype-name');
                    if (roomNameEl) currentRoomName = (roomNameEl.innerText
                        || roomNameEl.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 160);
                    const select = row.querySelector('select[name^="nr_rooms_"]');
                    if (!select) continue;
                    const occupancyEl = row.querySelector(
                        '.hprt-occupancy-occupancy-info, [class*="occupancy"]');
                    const occupancyText = occupancyEl
                        ? (occupancyEl.innerText || occupancyEl.textContent || '') : '';
                    const occupancyMatch = occupancyText.match(/(\\d{1,2})/);
                    const maxPersons = occupancyMatch ? Number(occupancyMatch[1]) : null;
                    const name = select.getAttribute('name') || '';
                    const blockId = name.startsWith('nr_rooms_')
                        ? name.slice('nr_rooms_'.length, 'nr_rooms_'.length + 120) : '';
                    const popover = row.querySelector('.prco-price-area-popover');
                    const rowText = (row.innerText || row.textContent || '')
                        .replace(/\\s+/g, ' ').trim().slice(0, 2000);
                    const text = popover
                        ? (popover.innerText || popover.textContent || '')
                            .replace(/\\s+/g, ' ').trim().slice(0, 2000)
                        : '';
                    const pricedOption = Array.from(select.options).find(option =>
                        option.value && option.value !== '0'
                        && /(?:€|£|US\\$|A\\$|C\\$|NZ\\$|\\$|CHF|R\\$)\\s*[0-9]/.test(option.textContent || ''));
                    const pricedOptionText = pricedOption
                        ? (pricedOption.textContent || '').replace(/\\s+/g, ' ').trim()
                        : '';
                    const roundedMatch = pricedOptionText.match(
                        /(?:€|£|US\\$|A\\$|C\\$|NZ\\$|\\$|CHF|R\\$)\\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\\.[0-9]{2})?)/
                    );
                    const roundedTotal = roundedMatch
                        ? Number(roundedMatch[1].replace(/,/g, '')) : null;
                    let cancellationType = '';
                    if (/free cancellation/i.test(rowText)) {
                        cancellationType = 'free_cancellation';
                    } else if (/non-refundable/i.test(rowText)) {
                        cancellationType = 'non_refundable';
                    }
                    if (blockId && (text || Number.isFinite(roundedTotal))) rateDisplays.push({
                        block_id: blockId,
                        cancellation_type: cancellationType,
                        text,
                        rounded_total: Number.isFinite(roundedTotal) ? roundedTotal : null,
                        room_name: currentRoomName,
                        max_persons: Number.isFinite(maxPersons) ? maxPersons : null,
                    });
                }
                return {legacy_keys: legacyKeys, rates, rate_displays: rateDisplays};
            }"""
        )
    except Exception:
        embedded_capture = {}
    if not isinstance(embedded_capture, dict):
        embedded_capture = {}
    source_capture = {"rates": [], "rate_displays": [], "diagnostics": {}}
    if source_text:
        try:
            source_capture = extract_booking_source_pricing(source_text)
        except ValueError as error:
            source_capture = {
                "rates": [],
                "rate_displays": [],
                "diagnostics": {"source_failures": [str(error)]},
            }
    pricing_object_keys = embedded_capture.get("legacy_keys") or []
    raw_rates = source_capture.get("rates") or embedded_capture.get("rates") or []
    structured_rates = []
    seen_structured_rates = set()
    for item in raw_rates[:16]:
        if not isinstance(item, dict):
            continue
        dedupe_key = (
            str(item.get("block_id") or ""),
            str(item.get("cancellation_type") or ""),
            _strict_amount(item.get("total")),
            _strict_amount(item.get("net_room")),
        )
        if dedupe_key in seen_structured_rates:
            continue
        seen_structured_rates.add(dedupe_key)
        structured_rates.append(item)

    rate_display_texts: dict[str, set[str]] = {}
    rate_display_metadata: list[dict] = []
    raw_displays = [
        *(embedded_capture.get("rate_displays") or []),
        *(source_capture.get("rate_displays") or []),
    ]
    seen_rate_displays = set()
    room_info: dict[str, dict] = {}
    for item in raw_displays[:40]:
        if not isinstance(item, dict):
            continue
        block_id = str(item.get("block_id") or "")[:120]
        room_id = _booking_room_id(block_id)
        room_name = " ".join(str(item.get("room_name") or "").split())[:160]
        max_persons = item.get("max_persons")
        if room_id and (room_name or isinstance(max_persons, int)):
            info = room_info.setdefault(room_id, {"name": "", "max_persons": None})
            info["name"] = info["name"] or room_name
            if isinstance(max_persons, int) and not isinstance(max_persons, bool):
                info["max_persons"] = max(info["max_persons"] or 0, max_persons)
        text = " ".join(str(item.get("text") or "").split())[:2000]
        rounded_total = _strict_amount(item.get("rounded_total"))
        if block_id and (text or rounded_total is not None):
            display_key = (block_id, text, rounded_total)
            if display_key in seen_rate_displays:
                continue
            seen_rate_displays.add(display_key)
            rate_display_texts.setdefault(block_id, set()).add(text)
            matches = list(_BOOKING_EUR_RATE_RE.finditer(text))
            subtotal = None
            if currency == "EUR" and len(matches) == 1:
                subtotal = _embedded_booking_amount(
                    matches[0].group(3).replace(",", "")
                )
            rate_display_metadata.append({
                "block_id": block_id,
                "cancellation_type": str(
                    item.get("cancellation_type") or ""
                )[:80],
                "text": text,
                "subtotal": subtotal,
                "rounded_total": rounded_total,
            })

    # Booking does not guarantee field order around the embedded breakdown.
    # Prefer an exact subtotal join.  When the table exposes only a rounded
    # whole-euro total, accept it solely when exactly one structured rate falls
    # within that rounding bucket.  Ambiguous totals remain untouched.
    for rate in structured_rates:
        total = _embedded_booking_amount(rate.get("total"))
        if total is None:
            continue
        matches = []
        for item in rate_display_metadata:
            exact_subtotal = item["subtotal"]
            rounded_total = item["rounded_total"]
            if (
                exact_subtotal is not None
                and abs(exact_subtotal - total) <= _RECONCILIATION_TOLERANCE
            ):
                matches.append((item, "exact_total"))
                continue
            if rounded_total is None or abs(rounded_total - total) > 0.51:
                continue
            rounded_candidates = [
                candidate
                for candidate in structured_rates
                if (
                    (candidate_total := _embedded_booking_amount(candidate.get("total")))
                    is not None
                    and abs(candidate_total - rounded_total) <= 0.51
                )
            ]
            if len(rounded_candidates) == 1:
                matches.append((item, "unique_rounded_total"))
        if len(matches) != 1:
            continue
        matched_display, join_method = matches[0]
        rate["block_id"] = matched_display["block_id"]
        rate["display_join"] = join_method
        if matched_display["cancellation_type"]:
            rate["cancellation_type"] = matched_display["cancellation_type"]

    selected_rate = None
    # Complexes list several room types (live 1024817: 5 rates in 7 rows).
    # Pick the room that matches the TravelStaytion listing, then choose a
    # rate inside that room only; never mix rooms.
    room_choice = choose_booking_room(
        structured_rates, room_info, ref.extra.get("subject_profile"), guests
    )
    room_rates = room_choice["rates"]
    flexible_rates = [
        item for item in room_rates
        if "free_cancellation" in str(item.get("cancellation_type") or "").casefold()
    ]
    if len(flexible_rates) == 1:
        selected_rate = flexible_rates[0]
    elif len(room_rates) == 1:
        selected_rate = room_rates[0]

    structured_line_items = []
    structured_total = None
    structured_refundable = None
    structured_cancellation = None
    structured_cancellation_evidence = None
    selected_block_id = None
    structured_rejection_reasons: list[str] = []
    if selected_rate is None and outcome == "unknown":
        structured_rejection_reasons.append(
            room_choice["problem"]
            or "rate selection failed: expected exactly one flexible rate or one total rate"
        )
    if selected_rate is not None and outcome == "unknown":
        total = _embedded_booking_amount(selected_rate.get("total"))
        accommodation = _embedded_booking_amount(selected_rate.get("net_room"))
        included_sum = _embedded_booking_amount(selected_rate.get("included_sum"))
        excluded_amount = _embedded_booking_amount(
            selected_rate.get("excluded_charges_amount")
        )
        excluded_count = selected_rate.get("excluded_count")
        unsupported_counts = [
            selected_rate.get("conditional_count"),
            selected_rate.get("nonmandatory_count"),
            selected_rate.get("extra_addons_count"),
        ]
        charges = selected_rate.get("included_charges")
        if total is None or total < 0:
            structured_rejection_reasons.append("selected rate has no valid total")
        if accommodation is None or accommodation < 0:
            structured_rejection_reasons.append(
                "selected rate has no valid accommodation amount"
            )
        if included_sum is None or included_sum < 0:
            structured_rejection_reasons.append(
                "selected rate has no valid included-charge sum"
            )
        if excluded_amount != 0:
            structured_rejection_reasons.append(
                f"selected rate has non-zero or missing excluded charges: {excluded_amount!r}"
            )
        if selected_rate.get("all_included") is not True:
            structured_rejection_reasons.append(
                "selected rate is not explicitly all-included"
            )
        if (
            not isinstance(excluded_count, int)
            or isinstance(excluded_count, bool)
            or excluded_count < 0
        ):
            structured_rejection_reasons.append(
                f"selected rate has an invalid excluded-charge count: {excluded_count!r}"
            )
        if not all(
            isinstance(value, int) and value == 0 for value in unsupported_counts
        ):
            structured_rejection_reasons.append(
                f"selected rate has unsupported charge groups: {unsupported_counts!r}"
            )
        if not isinstance(charges, list):
            structured_rejection_reasons.append(
                "selected rate has no included-charge list"
            )
        safe = not structured_rejection_reasons
        if safe:
            structured_line_items.append({
                "label": f"{(checkout - checkin).days} nights accommodation",
                "amount": accommodation,
                "currency": currency,
                "included_in_displayed_total": True,
            })
            captured_charge_sum = 0.0
            for charge in charges[:24]:
                if not isinstance(charge, dict):
                    safe = False
                    structured_rejection_reasons.append(
                        "included charge is not an object"
                    )
                    break
                label = str(charge.get("label") or charge.get("label_alt") or "").strip()
                charge_type = str(charge.get("type") or "")
                amount = _embedded_booking_amount(charge.get("raw_value"))
                if not label or amount is None or amount < 0 or not charge_type.endswith("__included"):
                    safe = False
                    structured_rejection_reasons.append(
                        f"invalid included charge at index {len(structured_line_items)}"
                    )
                    break
                captured_charge_sum += amount
                if amount:
                    structured_line_items.append({
                        "label": label,
                        "amount": amount,
                        "currency": currency,
                        "included_in_displayed_total": True,
                    })
            arithmetic_difference = (
                abs((accommodation + captured_charge_sum) - total)
                if safe else math.inf
            )
            declared_difference = (
                abs(captured_charge_sum - included_sum)
                if safe else math.inf
            )
            if (
                arithmetic_difference <= _RECONCILIATION_TOLERANCE
                and declared_difference <= _RECONCILIATION_TOLERANCE
            ):
                selected_block_id = (
                    str(selected_rate.get("block_id") or "")[:120] or None
                )
                display_texts = (
                    rate_display_texts.get(selected_block_id, set())
                    if selected_block_id else set()
                )
                sponsored_discount = None
                payable_total = total
                if len(display_texts) == 1:
                    display_text = next(iter(display_texts))
                    if "booking.com pays" in display_text.casefold():
                        matches = list(_BOOKING_EUR_RATE_RE.finditer(display_text))
                        if currency != "EUR" or len(matches) != 1:
                            safe = False
                            structured_rejection_reasons.append(
                                "sponsored-discount disclosure is ambiguous or non-EUR"
                            )
                        else:
                            _, captured_nights, subtotal, discount, final_total = (
                                matches[0].groups()
                            )
                            captured_nights = int(captured_nights)
                            subtotal_amount, discount_amount, final_amount = [
                                float(value.replace(",", ""))
                                for value in (subtotal, discount, final_total)
                            ]
                            if (
                                captured_nights != (checkout - checkin).days
                                or abs(subtotal_amount - total)
                                    > _RECONCILIATION_TOLERANCE
                                or abs(
                                    subtotal_amount - discount_amount - final_amount
                                ) > _RECONCILIATION_TOLERANCE
                            ):
                                safe = False
                                structured_rejection_reasons.append(
                                    "sponsored-discount arithmetic or stay length does not reconcile"
                                )
                            else:
                                sponsored_discount = discount_amount
                                payable_total = final_amount
                    elif "booking.com pays" in low:
                        safe = False
                        structured_rejection_reasons.append(
                            "page discloses a sponsored discount outside the selected rate"
                        )
                elif any(
                    "booking.com pays" in text.casefold()
                    for text in display_texts
                ) or "booking.com pays" in low:
                    safe = False
                    structured_rejection_reasons.append(
                        "selected rate has ambiguous sponsored-discount disclosures"
                    )

                if safe:
                    if sponsored_discount is not None:
                        structured_line_items.append({
                            "label": "Booking.com pays",
                            "amount": -sponsored_discount,
                            "currency": currency,
                            "included_in_displayed_total": True,
                        })
                    structured_total = payable_total
                    outcome = "available"
                    cancellation = str(selected_rate.get("cancellation_type") or "")
                    if "free_cancellation" in cancellation.casefold():
                        structured_refundable = True
                        structured_cancellation = "Free cancellation"
                        structured_cancellation_evidence = "structured_rate"
                    elif "non_refundable" in cancellation.casefold() or "non-refundable" in cancellation.casefold():
                        structured_refundable = False
                        structured_cancellation = "Non-refundable"
                        structured_cancellation_evidence = "structured_rate"
            else:
                if arithmetic_difference > _RECONCILIATION_TOLERANCE:
                    structured_rejection_reasons.append(
                        "component arithmetic differs from total by "
                        f"{arithmetic_difference:.6f}"
                    )
                if declared_difference > _RECONCILIATION_TOLERANCE:
                    structured_rejection_reasons.append(
                        "captured charges differ from declared included sum by "
                        f"{declared_difference:.6f}"
                    )
    try:
        scoped_price_texts = page.locator(
            '[data-testid*="price"], [data-testid*="tax"], '
            '[data-testid*="charge"], [data-testid*="breakdown"], '
            '.bui-price-display__value, .prco-valign-middle-helper, '
            '[class*="price"]'
        ).evaluate_all(
            """els => els.slice(0, 100).map(el => ({
                testid: (el.getAttribute('data-testid') || '').slice(0, 120),
                class_name: (el.getAttribute('class') || '').slice(0, 240),
                text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 500),
                attributes: Object.fromEntries(Array.from(el.attributes)
                    .filter(attr => /^(?:aria-describedby|title)$/i.test(attr.name)
                        || /(?:price|tax|charge|fee|currency|amount)/i.test(attr.name))
                    .slice(0, 24)
                    .map(attr => [attr.name.slice(0, 120), attr.value.slice(0, 500)]))
            })).filter(item => item.text)"""
        )
    except Exception:
        scoped_price_texts = []
    tax_charge_popovers = []
    try:
        triggers = page.locator(
            '.prd-taxes-and-fees-under-price, [data-testid*="tax"], '
            '[data-testid*="charge"]'
        )
        for trigger_index in range(min(triggers.count(), 4)):
            try:
                triggers.nth(trigger_index).hover(timeout=1500)
                page.wait_for_timeout(250)
                popovers = page.locator(
                    '[role="tooltip"], .bui-popover__content, .bui-tooltip__content, '
                    '[class*="tax"][class*="popover"], '
                    '[class*="charge"][class*="popover"]'
                ).evaluate_all(
                    """els => els.slice(0, 20).map(el => ({
                        role: (el.getAttribute('role') || '').slice(0, 80),
                        testid: (el.getAttribute('data-testid') || '').slice(0, 120),
                        class_name: (el.getAttribute('class') || '').slice(0, 240),
                        text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 2000)
                    })).filter(item => item.text)"""
                )
                if popovers:
                    tax_charge_popovers.append({
                        "trigger_index": trigger_index,
                        "popovers": [
                            item for item in popovers if isinstance(item, dict)
                        ][:20],
                    })
            except Exception:
                continue
    except Exception:
        tax_charge_popovers = []
    try:
        availability_rows = page.locator(
            '#hprt-table tr, .hprt-table tr, '
            '[data-testid*="availability"] tr'
        ).evaluate_all(
            """els => els.slice(0, 40).map((el, row_index) => ({
                row_index,
                testid: (el.getAttribute('data-testid') || '').slice(0, 120),
                class_name: (el.getAttribute('class') || '').slice(0, 240),
                text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 2000),
                cells: Array.from(el.querySelectorAll('th, td')).slice(0, 16).map((cell, cell_index) => ({
                    cell_index,
                    tag: cell.tagName.toLowerCase(),
                    testid: (cell.getAttribute('data-testid') || '').slice(0, 120),
                    class_name: (cell.getAttribute('class') || '').slice(0, 240),
                    text: (cell.innerText || cell.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 600)
                })).filter(cell => cell.text),
                selects: Array.from(el.querySelectorAll('select')).slice(0, 4).map(select => ({
                    name: (select.getAttribute('name') || '').slice(0, 160),
                    value: (select.value || '').slice(0, 80),
                    options: Array.from(select.options).slice(0, 20).map(option => ({
                        value: (option.value || '').slice(0, 80),
                        text: (option.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 160)
                    }))
                }))
            })).filter(row => row.text)"""
        )
    except Exception:
        availability_rows = []

    # The navigation response can contain a complete, reconciling price while
    # omitting cancellation metadata.  If the rendered room table exposes
    # exactly one selectable rate row, its plain-language policy belongs to
    # that sole structured rate and can safely restore the missing flag.
    if structured_total is not None and structured_refundable is None:
        priced_rows = []
        for row in availability_rows:
            if not isinstance(row, dict):
                continue
            has_priced_option = any(
                str(option.get("value") or "") not in {"", "0"}
                for select in (row.get("selects") or [])
                if isinstance(select, dict)
                for option in (select.get("options") or [])
                if isinstance(option, dict)
            )
            if has_priced_option:
                priced_rows.append(row)
        if len(priced_rows) == 1:
            row_text = str(priced_rows[0].get("text") or "").casefold()
            says_free = "free cancellation" in row_text
            says_nonrefundable = (
                "non-refundable" in row_text or "non refundable" in row_text
            )
            if says_free != says_nonrefundable:
                structured_refundable = says_free
                structured_cancellation = (
                    "Free cancellation" if says_free else "Non-refundable"
                )
                structured_cancellation_evidence = "unique_rendered_rate_row"
    try:
        availability_sections = page.locator(
            '#hprt-table, .hprt-table, [data-block-id="available_rooms"], '
            '[data-testid*="availability"]'
        ).evaluate_all(
            """els => els.slice(0, 8).map(el => ({
                testid: (el.getAttribute('data-testid') || '').slice(0, 120),
                class_name: (el.getAttribute('class') || '').slice(0, 240),
                text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 8000)
            })).filter(item => item.text)"""
        )
    except Exception:
        availability_sections = []

    next_available_date_prices = [
        item for item in scoped_price_texts
        if isinstance(item, dict)
        and "c-next-available-dates__item__price"
        in str(item.get("class_name") or "")
    ]
    if (
        outcome == "unknown"
        and structured_total is None
        and not availability_rows
        and next_available_date_prices
    ):
        outcome = "unavailable"
        outcome_detail = (
            "Booking offered alternative dates because the requested stay "
            "is unavailable"
        )
        availability_outcome_signal = "next_available_dates_price_nodes"

    canonical = canonical_booking_url(ref.url)
    return {
        "snapshot_version": BOOKING_PRICING_SNAPSHOT_VERSION,
        "canonical_url": canonical,
        "quote_url": quote_url,
        "checkin": checkin.isoformat(),
        "checkout": checkout.isoformat(),
        "guests": guests,
        "currency": currency,
        "outcome": outcome,
        "outcome_detail": outcome_detail,
        "minimum_stay_nights": minimum_stay_nights,
        "displayed_total": structured_total,
        "line_items": structured_line_items if structured_total is not None else [],
        "refundable": structured_refundable,
        "cancellation_type": structured_cancellation,
        "rate_options": booking_rate_options(room_rates, selected_rate),
        "room_name": room_choice["name"],
        "evidence_source": (
            "embedded_public_booking_price_breakdown"
            if structured_total is not None
            else "rendered_public_booking_listing"
        ),
        "diagnostics": {
            "capture_version": 5,
            "availability_outcome_signal": availability_outcome_signal,
            "page_excerpt": _booking_page_excerpt(body_text),
            "pricing_extraction_route": (
                "navigation_response"
                if source_capture.get("rates")
                else "rendered_scripts"
            ),
            **(source_capture.get("diagnostics") or {}),
            "structured_rate_count": len(structured_rates),
            "structured_flexible_rate_count": len(flexible_rates),
            "structured_rate_display_count": len(rate_display_metadata),
            "selected_block_id": selected_block_id,
            "selected_rate_display_join": (
                str(selected_rate.get("display_join") or "")
                if selected_rate is not None else ""
            ),
            "cancellation_evidence_route": structured_cancellation_evidence,
            "structured_rejection_reasons": structured_rejection_reasons[:24],
            "legacy_pricing_object_keys": [
                str(item) for item in pricing_object_keys if isinstance(item, str)
            ][:50],
            "scoped_price_texts": [
                {
                    "testid": str(item.get("testid") or ""),
                    "class_name": str(item.get("class_name") or ""),
                    "text": str(item.get("text") or ""),
                    "attributes": {
                        str(key)[:120]: str(value)[:500]
                        for key, value in (item.get("attributes") or {}).items()
                    } if isinstance(item.get("attributes"), dict) else {},
                }
                for item in scoped_price_texts
                if isinstance(item, dict)
            ][:80],
            "tax_charge_popovers": tax_charge_popovers[:4],
            "availability_rows": [
                item for item in availability_rows if isinstance(item, dict)
            ][:40],
            "availability_sections": [
                item for item in availability_sections if isinstance(item, dict)
            ][:8],
        },
    }


class _BookingSourceOnlyLocator:
    def evaluate_all(self, _script):
        return []

    def count(self):
        return 0


class _BookingSourceOnlyPage:
    def evaluate(self, _script):
        return {}

    def locator(self, _selector):
        return _BookingSourceOnlyLocator()


def price_booking_source(
    source_text: str,
    *,
    ref: PropertyRef,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
) -> PriceResult:
    """Parse a user-saved Booking page source without any network access."""
    currency = currency.upper()
    quote_url = booking_quote_url(ref, checkin, checkout, guests, currency)
    snapshot = _capture_booking_pricing_snapshot(
        _BookingSourceOnlyPage(),
        ref=ref,
        quote_url=quote_url,
        checkin=checkin,
        checkout=checkout,
        guests=guests,
        currency=currency,
        body_text="",
        source_text=source_text,
    )
    return parse_booking_pricing_snapshot(
        ref, snapshot, checkin, checkout, guests, currency
    )


_IDENTIFY_SERVER_ERROR_RETRIES = 2
_IDENTIFY_RETRY_PAUSE_SECONDS = 4.0


class _BookingServerError(ValueError):
    """Booking answered with a 5xx: its fault, worth a paced retry."""


class BookingAdapter(NeedsBrowser):
    site = "booking"

    def __init__(self, session: BrowserSession):
        self._session = session

    def identify(self, url: str) -> PropertyRef:
        # Booking intermittently answers a property page with a 5xx (live:
        # HTTP 500 for 965991's match); one paced retry usually succeeds.
        for attempt in range(1 + _IDENTIFY_SERVER_ERROR_RETRIES):
            try:
                return self._identify_once(url)
            except _BookingServerError:
                if attempt == _IDENTIFY_SERVER_ERROR_RETRIES:
                    raise
                time.sleep(_IDENTIFY_RETRY_PAUSE_SECONDS)
        raise AssertionError("unreachable")

    def _identify_once(self, url: str) -> PropertyRef:
        canonical_url = canonical_booking_url(url)
        if self._session.offline:
            return _property_from_snapshot(
                self._session.load_snapshot(self.site, "page", canonical_url)
            )

        self._session.before_navigation(self.site, "page")
        started = time.monotonic()
        page = self._session.new_page()
        recorded = False
        try:
            response = page.goto(canonical_url, wait_until="domcontentloaded", timeout=25000)
            BrowserSession.decline_cookies(page)
            page.wait_for_timeout(1200)
            title = page.title()
            body_text = page.inner_text("body")
            if BrowserSession.is_bot_challenge(f"{title}\n{body_text}") and _human_cleared(
                self._session, page
            ):
                title = page.title()
                body_text = page.inner_text("body")
            if BrowserSession.is_bot_challenge(f"{title}\n{body_text}"):
                self._session.record_navigation(
                    self.site,
                    "page",
                    (time.monotonic() - started) * 1000,
                    outcome="browser_challenge",
                    challenge=True,
                )
                recorded = True
                raise RequestDeferred(
                    "DEFERRED — Booking.com challenge detected; persistent cooldown opened"
                )
            status = response.status if response is not None else None
            if status is not None and status >= 400:
                self._session.record_navigation(
                    self.site,
                    "page",
                    (time.monotonic() - started) * 1000,
                    outcome=f"browser_http_{status}",
                )
                recorded = True
                error_class = _BookingServerError if status >= 500 else ValueError
                raise error_class(f"Booking.com property page returned HTTP {status}")

            try:
                headings = page.locator("h1, h2").all_text_contents()
            except Exception:
                headings = []
            try:
                json_ld_texts = page.locator(
                    'script[type="application/ld+json"]'
                ).all_text_contents()
            except Exception:
                json_ld_texts = []
            try:
                dom_image_values = page.locator("img").evaluate_all(
                    """els => els.flatMap(el => [
                        el.currentSrc || '', el.src || '', el.getAttribute('data-src') || '',
                        ...(el.srcset || '').split(',').map(part => part.trim().split(/\\s+/)[0])
                    ]).filter(Boolean)"""
                )
            except Exception:
                dom_image_values = []
            snapshot = _build_identity_snapshot(
                canonical_url=canonical_url,
                page_title=title,
                headings=headings,
                body_text=body_text,
                html=page.content(),
                json_ld_texts=json_ld_texts,
                dom_image_values=dom_image_values,
            )
            self._session.record_navigation(
                self.site,
                "page",
                (time.monotonic() - started) * 1000,
            )
            recorded = True
            self._session.save_snapshot(self.site, "page", canonical_url, snapshot)
            return _property_from_snapshot(snapshot)
        except (RequestDeferred, ValueError):
            raise
        except Exception as error:
            if not recorded:
                self._session.record_navigation(
                    self.site,
                    "page",
                    (time.monotonic() - started) * 1000,
                    outcome=f"browser_error:{type(error).__name__}",
                )
            raise
        finally:
            page.close()

    def get_price(
        self,
        ref: PropertyRef,
        checkin: date,
        checkout: date,
        guests: int,
    ) -> PriceResult:
        currency = str(ref.extra.get("quote_currency") or "EUR").upper()
        quote_url = booking_quote_url(ref, checkin, checkout, guests, currency)
        if self._session.offline:
            snapshot = self._session.load_snapshot(self.site, "pricing", quote_url)
            return parse_booking_pricing_snapshot(
                ref, snapshot, checkin, checkout, guests, currency
            )
        if not ref.extra.get("booking_pricing_capture_enabled"):
            return _booking_price_error(
                ref,
                checkin,
                checkout,
                guests,
                currency,
                "Live Booking pricing capture is locked until explicitly authorised",
                source_url=quote_url,
            )

        self._session.before_navigation(self.site, "pricing")
        started = time.monotonic()
        page = self._session.new_page()
        recorded = False
        try:
            response = page.goto(quote_url, wait_until="domcontentloaded", timeout=25000)
            source_text = None
            if response is not None:
                try:
                    candidate_source = response.text()
                    if (
                        len(candidate_source.encode("utf-8", errors="replace"))
                        <= _MAX_BOOKING_SOURCE_BYTES
                    ):
                        source_text = candidate_source
                except Exception:
                    source_text = None
            BrowserSession.decline_cookies(page)
            if response is not None and response.status == 202:
                # Booking's interstitial: its HTML is a holding page that later
                # reloads into the real listing, so it is not pricing evidence.
                source_text = None
            if not _wait_for_booking_rates(page, 15000):
                wait = getattr(self._session, "wait_for_human", None)
                if wait:
                    wait(page, "Booking.com price page",
                         ready=lambda current: _wait_for_booking_rates(current, 1000))
            page.wait_for_timeout(800)
            title = page.title()
            body_text = page.inner_text("body")
            if BrowserSession.is_bot_challenge(f"{title}\n{body_text}") and _human_cleared(
                self._session, page
            ):
                title = page.title()
                body_text = page.inner_text("body")
            if BrowserSession.is_bot_challenge(f"{title}\n{body_text}"):
                self._session.record_navigation(
                    self.site,
                    "pricing",
                    (time.monotonic() - started) * 1000,
                    outcome="browser_challenge",
                    challenge=True,
                )
                recorded = True
                raise RequestDeferred(
                    "DEFERRED — Booking.com challenge detected; persistent cooldown opened"
                )
            status = response.status if response is not None else None
            if status is not None and status >= 400:
                self._session.record_navigation(
                    self.site,
                    "pricing",
                    (time.monotonic() - started) * 1000,
                    outcome=f"browser_http_{status}",
                )
                recorded = True
                return _booking_price_error(
                    ref,
                    checkin,
                    checkout,
                    guests,
                    currency,
                    f"Booking checkout page returned HTTP {status}",
                    source_url=quote_url,
                )

            if booking_dates_were_dropped(page.url, body_text):
                self._session.record_navigation(
                    self.site,
                    "pricing",
                    (time.monotonic() - started) * 1000,
                    outcome="browser_dates_dropped",
                )
                recorded = True
                return _booking_price_error(
                    ref,
                    checkin,
                    checkout,
                    guests,
                    currency,
                    "Booking ignored the requested dates (it redirected to the plain "
                    "property page asking to 'select dates'), so no rate could be read; "
                    "Booking does this when it is limiting automated visits - try again later",
                    source_url=quote_url,
                )
            snapshot = _capture_booking_pricing_snapshot(
                page,
                ref=ref,
                quote_url=quote_url,
                checkin=checkin,
                checkout=checkout,
                guests=guests,
                currency=currency,
                body_text=body_text,
                source_text=source_text,
            )
            self._session.record_navigation(
                self.site,
                "pricing",
                (time.monotonic() - started) * 1000,
            )
            recorded = True
            self._session.save_snapshot(self.site, "pricing", quote_url, snapshot)
            return parse_booking_pricing_snapshot(
                ref, snapshot, checkin, checkout, guests, currency
            )
        except RequestDeferred:
            raise
        except Exception as error:
            if not recorded:
                self._session.record_navigation(
                    self.site,
                    "pricing",
                    (time.monotonic() - started) * 1000,
                    outcome=f"browser_error:{type(error).__name__}",
                )
            return _booking_price_error(
                ref,
                checkin,
                checkout,
                guests,
                currency,
                f"Booking pricing capture failed: {error}",
                source_url=quote_url,
            )
        finally:
            page.close()
