"""Airbnb adapter — plain HTTP plus Airbnb's public checkout JSON. Also hosts the neighbour
search helpers used by parity/core/neighbours.py, since both hit the same
embedded-JSON shape.

Recipe (verified from a live browser response captured 29 Sep 2026):
- Price: one logged-out `StaysPdpSections` persisted-query POST returns
  `structuredDisplayPrice.explanationData.priceDetails`. The server-rendered
  Hyperloop listing page now leaves those fields null and is not a quote.
- Neighbour search: `/s/{place}/homes?...` with `min_bedrooms`,
  `room_types[]=Entire home/apt`, `amenities[]=7` (pool), a ~3km map box,
  and `paginationInfo.pageCursors` for paging. Each result carries
  `demandStayListing.location.coordinate` and bedroom/bathroom counts in
  `structuredContent.primaryLine`.

The direct API request assembled below still requires one controlled live
checkpoint after offline regression. Persisted-query hashes can change without
notice; failures must remain typed and must not trigger automatic retries.
"""
from __future__ import annotations

import base64
import hashlib
import html as html_lib
import json
import os
import re
from datetime import date
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from parity.adapters.base import HttpOnly
from parity.core.matching import find_licence_numbers, registration_tokens
from parity.core.models import (
    Outcome,
    PriceBreakdown,
    PriceLineItem,
    PriceResult,
    PropertyRef,
)
from parity.core.pricing import comparable_total


def _rate_limit_notes(resp: httpx.Response) -> str:
    retry_after = resp.headers.get("Retry-After")
    return f"HTTP 429 (rate limited); Retry-After={retry_after}" if retry_after else "HTTP 429 (rate limited)"

_ID_RE = re.compile(r"/rooms/(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")


_META_RE = re.compile(
    r'<meta\s+(?:property|name)="(og:title|og:description|twitter:title)"\s+content="([^"]*)"',
    re.IGNORECASE,
)
_PERSON_CAPACITY_RE = re.compile(r'"personCapacity"\s*:\s*(\d{1,2})\b')


def _structural_from_meta(html: str) -> dict:
    """Title and room counts from the listing's own share metadata.

    ``og:title`` reads like ``Home in Woodbridge · ★4.75 · 1 bedroom · 1 bed
    · 1.5 baths`` and ``og:description`` carries the listing title.  These
    describe only this listing, unlike free page text which can mention
    other properties.
    """
    meta = {key.lower(): html_lib.unescape(value) for key, value in _META_RE.findall(html)}
    summary = meta.get("og:title", "")
    out: dict = {}

    def number(pattern: str) -> float | None:
        found = re.search(pattern, summary, re.IGNORECASE)
        return float(found.group(1)) if found else None

    bedrooms = number(r"(\d+)\s+bedrooms?\b")
    if bedrooms is not None:
        out["bedrooms"] = int(bedrooms)
    bathrooms = number(r"(\d+(?:\.\d+)?)\s+(?:private\s+|shared\s+)?baths?\b")
    if bathrooms is not None:
        out["bathrooms"] = bathrooms
    capacity = _PERSON_CAPACITY_RE.search(html)
    if capacity:
        out["capacity"] = int(capacity.group(1))
    title = meta.get("og:description", "").strip()
    if title:
        out["title"] = title
    return out


_BEFORE_TAXES_RE = re.compile(
    r"before taxes|excluding taxes|excl\.? taxes|hors taxes|sin impuestos|ohne steuern",
    re.IGNORECASE,
)


def _listing_image_markers(raw_id: str) -> tuple[str, ...]:
    """Path markers identifying this listing's own photos.

    Older photos use ``Hosting-<id>/``; newer ones use the base64 of
    ``StaySupplyListing:<id>`` (or ``StayListing:<id>``) instead.
    """
    encoded = tuple(
        "Hosting-" + base64.b64encode(f"{prefix}:{raw_id}".encode()).decode() + "/"
        for prefix in ("StaySupplyListing", "StayListing")
    )
    return (f"Hosting-{raw_id}/", *encoded)


# Page timestamps such as ["PageGenerateAtToken",1792064261429] are 13 digits,
# the same shape as a French registration number; drop them before scanning.
_JSON_TIMESTAMP_RE = re.compile(
    r'"[A-Za-z_]*(?:At|Time|Timestamp|Date)[A-Za-z_]*"\s*[,:]\s*\d{10,13}\b'
)


def _identity_text(html: str) -> str:
    """Listing text including embedded JSON strings (descriptions, the
    "Registration Details" section), with escapes and markup removed."""
    text = (
        _JSON_TIMESTAMP_RE.sub(" ", html).replace("\\u003c", "<")
        .replace("\\u003e", ">")
        .replace("\\u0026", "&")
        .replace("\\u002F", "/")
        .replace("\\n", " ")
    )
    text = html_lib.unescape(text)
    return _TAG_RE.sub(" ", text)
_DEFERRED_STATE_RE = re.compile(
    r'<script[^>]*id="data-deferred-state-0"[^>]*>(.*?)</script>', re.DOTALL
)
_LEGACY_GALLERY_IMAGE_RE = re.compile(
    r"https://a0\.muscache\.com/im/pictures/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(?:jpe?g|png|webp)",
    re.IGNORECASE,
)
_MUSCACHE_IMAGE_RE = re.compile(
    r"https://a0\.muscache\.com/im/pictures/[^\s\"'<>]+",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(r"[-+]?\s*(\d[\d.,\s]*)")
_RECONCILIATION_TOLERANCE = 0.02
_CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "¥": "JPY"}
_DOLLAR_PREFIXES = {"US": "USD", "A": "AUD", "AU": "AUD", "C": "CAD", "CA": "CAD", "NZ": "NZD"}
_DOLLAR_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{0,3})\$")
_STAYS_PDP_OPERATION_HASH = "c47b106f155568d32d4396fac7bdd31dbffb3dead2ad9e5f1cf93a94bf7e5c75"
# Public browser key exposed in Airbnb's logged-out Hyperloop bootstrap data.
_AIRBNB_PUBLIC_API_KEY = "d306zoyjsyarp7ifhu67rjxn52tv0t20"

# Airbnb's bot detection appears to score on more than User-Agent -- a
# plain httpx request with just UA + Accept-Language came back on 22 Sep
# without the data-deferred-state-0 script that a real Chrome fetch() (same
# UA, no cookies) reliably got. This fuller header set mimics an actual
# Chrome navigation more closely; if it's still not enough, the adapter now
# logs exactly what it got back instead of a bare "not found" error.
AIRBNB_NAVIGATION_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    # Deliberately omit Accept-Encoding. httpx will advertise only the
    # compression formats for which this installation has working decoders.
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _find_key(obj, key: str):
    """Depth-first search for the first occurrence of `key` anywhere in a
    nested dict/list. Airbnb's embedded JSON is deeply nested and the exact
    path drifts between deploys; searching by key name is more robust than
    hardcoding a path."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def _parse_money_text(value: str) -> float | None:
    """Parse a displayed money string without guessing when separators conflict."""
    match = _MONEY_RE.search(value.replace("\u00a0", " "))
    if not match:
        return None
    number = match.group(1).replace(" ", "")
    if "," in number and "." in number:
        # The last separator is the decimal separator; the other is grouping.
        decimal = "," if number.rfind(",") > number.rfind(".") else "."
        grouping = "." if decimal == "," else ","
        number = number.replace(grouping, "").replace(decimal, ".")
    elif "," in number:
        tail = number.rsplit(",", 1)[1]
        number = number.replace(",", "." if len(tail) == 2 else "")
    elif number.count(".") > 1:
        tail = number.rsplit(".", 1)[1]
        number = number.replace(".", "") if len(tail) != 2 else number.replace(".", "", number.count(".") - 1)
    try:
        amount = float(number)
    except ValueError:
        return None
    if match.group(0).lstrip().startswith("-"):
        amount = -amount
    return amount


def _money_value(value, *, micros: bool = False) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
        return amount / 1_000_000 if micros else amount
    if isinstance(value, str):
        return _parse_money_text(value)
    if isinstance(value, dict):
        for key in ("amountMicros", "amount", "priceString", "formattedValue", "price"):
            if key in value:
                amount = _money_value(value[key], micros=key == "amountMicros")
                if amount is not None:
                    return amount
    return None


def _price_detail_items(price_details) -> list[dict]:
    """Return only line items below the anchored priceDetails subtree."""
    found: list[dict] = []

    def visit(node) -> None:
        if isinstance(node, dict):
            items = node.get("items")
            if isinstance(items, list):
                found.extend(item for item in items if isinstance(item, dict))
            for key, value in node.items():
                if key != "items":
                    visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(price_details)
    return found


def _line_label(item: dict) -> str:
    return str(
        item.get("description")
        or item.get("localizedTitle")
        or item.get("title")
        or item.get("label")
        or ""
    ).strip()


def _line_amount(item: dict) -> float | None:
    for key in ("total", "amountMicros", "amount", "priceString", "price"):
        if key in item:
            amount = _money_value(item[key], micros=key == "amountMicros")
            if amount is not None:
                return amount
    return None


def _line_category(label: str) -> str | None:
    low = label.casefold()
    # Airbnb's all-in ("total price") display ends its checkout breakdown
    # with "Price after discount" instead of "Total".
    if "total" in low or low.strip() == "price after discount":
        return "total"
    if any(word in low for word in ("discount", "saving", "promotion", "promo")):
        return "discount"
    if any(word in low for word in ("tax", "vat", "occupancy")):
        return "tax"
    if "deposit" in low:
        return "deposit"
    if "clean" in low:
        return "cleaning"
    if "service fee" in low or "airbnb service" in low:
        return "service_fee"
    if any(word in low for word in ("night", "stay", "accommodation", "base price")):
        return "accommodation"
    if "fee" in low:
        return "mandatory_fee"
    return None


def _displayed_total(
    structured_price: dict, items: list[dict], nights: int | None = None
) -> float | None:
    for item in items:
        if _line_category(_line_label(item)) == "total":
            amount = _line_amount(item)
            if amount is not None:
                return amount
    for key in ("grandTotal", "totalPrice"):
        amount = _money_value(_find_key(structured_price, key))
        if amount is not None:
            return amount
    secondary = structured_price.get("secondaryLine")
    if isinstance(secondary, dict):
        label = " ".join(str(secondary.get(key) or "") for key in ("price", "accessibilityLabel"))
        if "total" in label.casefold():
            return _money_value(secondary.get("price") or secondary.get("accessibilityLabel"))
    primary = structured_price.get("primaryLine")
    if isinstance(primary, dict) and str(primary.get("qualifier") or "").casefold() == "total":
        return _money_value(primary.get("price") or primary.get("accessibilityLabel"))
    # All-in display: "$595 AUD" + "for 3 nights" is the whole-stay total,
    # accepted only when the night count matches the requested stay.
    if (
        isinstance(primary, dict)
        and nights is not None
        and structured_price.get("displayPriceStyle") == "TOTAL_ONLY"
    ):
        qualifier = str(primary.get("qualifier") or "").casefold().strip()
        found = re.fullmatch(r"for (\d+) nights?", qualifier)
        if found and int(found.group(1)) == nights:
            return _money_value(primary.get("price") or primary.get("accessibilityLabel"))
    return None


def _precise_checkout_total(state, observed_currency: str) -> float | None:
    """Return Airbnb's explicit checkout Total when the headline is rounded.

    Some zero-decimal headline cards round an AUD total to the nearest dollar,
    while the BOOK_IT_SIDEBAR ``BasicPriceDetail`` retains the exact cents.
    Only an explicitly labelled Total inside that anchored checkout component
    is accepted. Conflicting totals or a currency mismatch fail closed by
    returning no override.
    """
    totals: set[float] = set()

    def visit(node) -> None:
        if isinstance(node, dict):
            if node.get("__typename") == "BasicPriceDetail":
                node_currency = _observed_currency(node, observed_currency)
                if node_currency == observed_currency:
                    price_details = _find_key(node.get("explanationData"), "priceDetails")
                    for item in _price_detail_items(price_details):
                        if _line_category(_line_label(item)) == "total":
                            amount = _line_amount(item)
                            if amount is not None:
                                totals.add(round(amount, 2))
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(state)
    return next(iter(totals)) if len(totals) == 1 else None


def _observed_currency(structured_price: dict, requested: str | None = None) -> str | None:
    """Read the currency Airbnb actually rendered; never trust only the request.

    A bare "$" (Airbnb's USD display, e.g. "$8,030") only confirms a requested
    dollar currency when nothing else on the price names a different one.
    """
    explicit: set[str] = set()
    symbols: set[str] = set()
    bare_dollar = False

    def visit(value, key: str | None = None) -> None:
        nonlocal bare_dollar
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, child_key)
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str):
            if key and key.casefold() in {"currency", "currencycode", "currency_code"}:
                code = value.strip().upper()
                if re.fullmatch(r"[A-Z]{3}", code):
                    explicit.add(code)
            for symbol, code in _CURRENCY_SYMBOLS.items():
                if symbol in value:
                    symbols.add(code)
            for prefix in _DOLLAR_RE.findall(value):
                code = _DOLLAR_PREFIXES.get(prefix.upper())
                if code:
                    symbols.add(code)
                elif prefix:
                    symbols.add(f"{prefix.upper()}$")
                else:
                    bare_dollar = True
            explicit.update(re.findall(r"(?<![A-Z])[A-Z]{3}(?![A-Z])", value.upper()))

    visit(structured_price)
    recognised = {code for code in explicit if code in {
        "AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NZD", "USD",
    }}
    observed = recognised | symbols
    if any(code.endswith("$") for code in observed):
        return None  # a dollar we cannot name (e.g. MX$)
    if bare_dollar:
        dollars = set(_DOLLAR_PREFIXES.values())
        if not observed <= dollars:
            return None  # "$" beside a non-dollar currency is ambiguous
        if not observed and requested in dollars:
            observed = {requested}
    return next(iter(observed)) if len(observed) == 1 else None


def _global_id(prefix: str, raw_id: str) -> str:
    return base64.b64encode(f"{prefix}:{raw_id}".encode("ascii")).decode("ascii")


def _p3_impression_id(raw_id: str, checkin: date, checkout: date, guests: int) -> str:
    """Stable per quote so exact HTTP fixtures replay without random body drift."""
    material = f"{raw_id}|{checkin.isoformat()}|{checkout.isoformat()}|{guests}".encode("ascii")
    digest = hashlib.sha256(material).hexdigest()
    digits = str(int(digest[:12], 16)).zfill(14)[:10]
    return f"p3_{digits}_{digest[12:24]}"


def _airbnb_locale(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(".com.au"):
        return "en-AU"
    if host.endswith(".co.uk"):
        return "en-GB"
    return "en"


def _stays_pdp_variables(
    raw_id: str,
    checkin: date,
    checkout: date,
    guests: int,
) -> dict:
    p3_id = _p3_impression_id(raw_id, checkin, checkout, guests)
    request = {
        "adults": str(guests),
        "amenityFilters": None,
        "bypassTargetings": False,
        "categoryTag": None,
        "causeId": None,
        "children": None,
        "disasterId": None,
        "discountedGuestFeeVersion": None,
        "federatedSearchId": None,
        "forceBoostPriorityMessageType": None,
        "guestAges": None,
        "hostPreview": False,
        "infants": None,
        "interactionType": None,
        "layouts": ["SIDEBAR", "SINGLE_COLUMN"],
        "pets": 0,
        "pdpTypeOverride": None,
        "photoId": None,
        "preview": False,
        "previousStateCheckIn": None,
        "previousStateCheckOut": None,
        "priceDropSource": None,
        "partner": None,
        "partnerProgram": None,
        "directBookingParams": None,
        "privateBooking": False,
        "promotionUuid": None,
        "relaxedAmenityIds": None,
        "searchId": None,
        "selectedCancellationPolicyId": None,
        "selectedRatePlanId": None,
        "splitStays": None,
        "staysBookingMigrationEnabled": False,
        "translateUgc": None,
        "useNewSectionWrapperApi": False,
        "sectionIds": [
            "BOOK_IT_CALENDAR_SHEET",
            "POLICIES_DEFAULT",
            "BOOK_IT_SIDEBAR",
            "BOOK_IT_NAV",
            "BOOK_IT_FLOATING_FOOTER",
            "CANCELLATION_POLICY_PICKER_MODAL",
        ],
        "checkIn": checkin.isoformat(),
        "checkOut": checkout.isoformat(),
        "p3ImpressionId": p3_id,
    }
    variables = {
        "id": _global_id("StayListing", raw_id),
        "demandStayListingId": _global_id("DemandStayListing", raw_id),
        "pdpSectionsRequest": request,
        "categoryTag": None,
        "federatedSearchId": None,
        "federatedSearchSessionId": None,
        "p3ImpressionId": p3_id,
        "photoId": None,
        "includePdpLayoutPipelineInputs": False,
        "amenityIds": None,
        "causeId": None,
        "dateRange": {"startDate": checkin.isoformat(), "endDate": checkout.isoformat()},
        "guestCounts": {"numberOfAdults": guests},
        "numberOfChildren": None,
        "numberOfInfants": None,
        "numberOfPets": None,
    }
    true_flags = (
        "includeGpAccessibilityFeaturesFragment",
        "includeGpLuxeServicesFragment",
        "includeGpBookItFragment",
        "includeGpAmenitiesFragment",
        "includeGpAvailabilityCalendarInlineFragment",
        "includeGpAvailabilityCalendarFragment",
        "includeGpHeroFragment",
        "includeGpLocationPdpFragment",
        "includeGpMeetYourHostFragment",
        "includeGpMessageBannerFragment",
        "includeGpBookItNonExperiencedGuestFragment",
        "includeGpBathroomFragment",
        "includeGpPropertyAvailableRoomsFragment",
        "includeGpReviewsFragment",
        "includeGpSleepingArrangementFragment",
        "includeGpSleepingArrangementImagesFragment",
        "includeGpPoliciesFragment",
    )
    false_flags = (
        "includePdpMigrationAccessibilityFeaturesPreviewCarouselFragment",
        "includePdpMigrationLuxeServicesFragment",
        "includePdpMigrationBookItNavFragment",
        "includePdpMigrationAmenitiesFragment",
        "includePdpMigrationAvailabilityCalendarInlineFragment",
        "includePdpMigrationAvailabilityCalendarFragment",
        "includePdpMigrationHeroFragment",
        "includePdpMigrationLocationPdpFragment",
        "includePdpMigrationMeetYourHostFragment",
        "includePdpMigrationMessageBannerFragment",
        "includePdpMigrationBookItFloatingFooterFragment",
        "includePdpMigrationBookItSidebarFragment",
        "includePdpMigrationBookItCalendarSheetFragment",
        "includePdpMigrationBookItNonExperiencedGuestFragment",
        "includePdpMigrationBathroomFragment",
        "includePdpMigrationPropertyAvailableRoomsFragment",
        "includePdpMigrationReviewsFragment",
        "includePdpMigrationSleepingArrangementFragment",
        "includePdpMigrationSleepingArrangementImagesFragment",
        "includePdpMigrationPoliciesFragment",
        "includePdpMigrationOnlyOnBookItFragment",
        "includePdpMigrationOnlyOnBookItNavFragment",
        "includeRecentAskPdpQuestions",
        "includePdpOffers",
    )
    variables.update({key: True for key in true_flags})
    variables.update({key: False for key in false_flags})
    return variables


def _stays_pdp_request(
    ref: PropertyRef,
    checkin: date,
    checkout: date,
    guests: int,
    currency: str,
) -> tuple[str, dict, dict, dict]:
    host = (urlparse(ref.url).hostname or "www.airbnb.com").lower()
    if not re.fullmatch(r"(?:www\.)?airbnb\.[a-z.]+", host):
        host = "www.airbnb.com"
    url = f"https://{host}/api/v3/StaysPdpSections/{_STAYS_PDP_OPERATION_HASH}"
    params = {
        "operationName": "StaysPdpSections",
        "locale": _airbnb_locale(ref.url),
        "currency": currency,
    }
    payload = {
        "operationName": "StaysPdpSections",
        "variables": _stays_pdp_variables(ref.raw_id, checkin, checkout, guests),
        "extensions": {
            "persistedQuery": {"version": 1, "sha256Hash": _STAYS_PDP_OPERATION_HASH}
        },
    }
    headers = {
        "User-Agent": AIRBNB_NAVIGATION_HEADERS["User-Agent"],
        "Accept": "application/json",
        "Accept-Language": "en-GB,en;q=0.9",
        "Content-Type": "application/json",
        "Referer": ref.url,
        "X-Airbnb-API-Key": _AIRBNB_PUBLIC_API_KEY,
        "X-Airbnb-GraphQL-Platform": "web",
        "X-Airbnb-GraphQL-Platform-Client": "minimalist-niobe",
        # Without a point-of-sale country Airbnb may quote a "total before
        # taxes" (seen from a US network: GBP 1,044.44 vs the guest's GBP
        # 1,087.28 incl. taxes for 34141360).  With a country it returns the
        # tax-inclusive guest total and an explicit Taxes line.
        "X-Airbnb-Country": airbnb_point_of_sale_country(),
    }
    return url, params, payload, headers


def airbnb_point_of_sale_country() -> str:
    value = os.environ.get("PARITY_AIRBNB_COUNTRY", "GB").strip().upper()
    return value if re.fullmatch(r"[A-Z]{2}", value) else "GB"


class AirbnbAdapter(HttpOnly):
    site = "airbnb"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            headers=AIRBNB_NAVIGATION_HEADERS,
            timeout=20.0,
            follow_redirects=True,
        )

    # identify() runs before we know the real stay dates (it's used purely
    # to read static property info -- licence, lat/lng, gallery -- not to
    # price anything). But a DATELESS request to the room URL got a ~2x
    # larger page *without* the data-deferred-state-0 script on a live run
    # 22 Sep, while the same URL WITH check_in/check_out/adults/currency
    # (what get_price() always sends) reliably had it. So identify() always
    # sends a placeholder date range just to get the hydrated page variant
    # -- the actual dates don't matter here, only that some are present.
    _PLACEHOLDER_CHECKIN = "2026-11-01"
    _PLACEHOLDER_CHECKOUT = "2026-11-04"

    def identify(self, url: str) -> PropertyRef:
        match = _ID_RE.search(urlparse(url).path)
        if not match:
            raise ValueError(f"Not a recognizable Airbnb listing URL: {url}")
        raw_id = match.group(1)

        params = {
            "check_in": self._PLACEHOLDER_CHECKIN,
            "check_out": self._PLACEHOLDER_CHECKOUT,
            "adults": 2,
            "currency": "EUR",
        }
        resp = self._client.get(url, params=params)
        if resp.status_code == 429:
            raise RuntimeError(_rate_limit_notes(resp))
        resp.raise_for_status()
        state, parse_error = self._extract_deferred_state(resp.text)
        if not state:
            fallback = self._identify_from_public_html(resp.text, raw_id, url)
            if fallback is not None:
                return fallback
            raise ValueError(f"identify(): {self._diagnose(resp.text, parse_error)}")

        lat = _find_key(state, "lat")
        lng = _find_key(state, "lng")
        if lat is None or lng is None:
            lat = _find_key(state, "latitude")
            lng = _find_key(state, "longitude")
        title = _find_key(state, "title") or _find_key(state, "name")
        # Older payloads list "photos"; newer ones list "mediaItems" whose
        # image URLs carry an encoded listing id (see _listing_image_markers).
        photos = _find_key(state, "photos") or _find_key(state, "mediaItems") or []
        gallery = []
        for p in photos if isinstance(photos, list) else []:
            if isinstance(p, dict):
                pic = p.get("picture") or p.get("baseUrl") or p.get("url")
                if isinstance(pic, str) and pic and pic not in gallery:
                    gallery.append(pic)
        markers = _listing_image_markers(raw_id)
        own = [pic for pic in gallery if any(marker in pic for marker in markers)]
        if own:
            gallery = own

        page_text = _identity_text(resp.text)
        registration_ids = list(find_licence_numbers(page_text))
        structural = _structural_from_meta(resp.text)
        return PropertyRef(
            site=self.site,
            raw_id=raw_id,
            url=url,
            licence_number=registration_ids[0] if registration_ids else None,
            registration_ids=registration_ids,
            bedrooms=structural.get("bedrooms"),
            bathrooms=structural.get("bathrooms"),
            capacity=structural.get("capacity"),
            lat=lat,
            lng=lng,
            # The share metadata names this listing; a generic "title" key in
            # the state can be a date label ("Nov 1 – 4") or a section heading.
            title=structural.get("title") or (title if isinstance(title, str) else None),
            gallery=gallery,
            extra={"registration_tokens": sorted(registration_tokens(page_text))},
        )

    @staticmethod
    def _identify_from_public_html(
        html: str,
        raw_id: str,
        url: str,
    ) -> PropertyRef | None:
        """Fallback for Airbnb's server-rendered Hyperloop listing page.

        This reads only public page content. It intentionally requires listing-
        specific image URLs before returning so a generic Airbnb shell cannot
        be mistaken for usable identity evidence.
        """
        soup = BeautifulSoup(html, "html.parser")
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else None
        visible_text = soup.get_text(" ", strip=True)

        normalised_html = html_lib.unescape(
            html.replace(r"\u002F", "/")
                .replace(r"\u0026", "&")
                .replace(r"\/", "/")
        )
        image_urls: list[str] = []
        seen_images: set[str] = set()
        listing_markers = _listing_image_markers(raw_id)
        for match in _MUSCACHE_IMAGE_RE.finditer(normalised_html):
            image_url = match.group(0).rstrip("\\),]}")
            image_key = image_url.split("?", 1)[0]
            if not any(marker in image_key for marker in listing_markers) or image_key in seen_images:
                continue
            seen_images.add(image_key)
            image_urls.append(f"{image_key}?im_w=720")

        if not image_urls:
            # Older listings publish gallery photos as bare
            # /im/pictures/<uuid>.<ext> without a Hosting-<id> marker.
            # Platform assets and user avatars live in sub-folders, so
            # requiring the uuid directly under /pictures/ excludes them.
            for match in _LEGACY_GALLERY_IMAGE_RE.finditer(normalised_html):
                image_key = match.group(0)
                if image_key not in seen_images:
                    seen_images.add(image_key)
                    image_urls.append(f"{image_key}?im_w=720")

        page_text = _identity_text(html)
        registration_ids = list(dict.fromkeys([
            *find_licence_numbers(visible_text),
            *find_licence_numbers(page_text),
        ]))
        # A generic Airbnb shell has neither listing photos nor a licence.
        if not image_urls and not registration_ids:
            return None

        def _integer(pattern: str) -> int | None:
            found = re.search(pattern, visible_text, re.IGNORECASE)
            return int(found.group(1)) if found else None

        capacity = _integer(r"\b(\d+)\s+guests?\b")
        bedrooms = _integer(r"\b(\d+)\s+bedrooms?\b")
        # Require a real number. ``[\d.]+`` also accepts a lone full stop,
        # which occurred in a live Hyperloop page as ``. bathrooms`` before
        # the actual structured count and crashed ``float('.')``.
        bathroom_match = re.search(
            r"\b(\d+(?:\.\d+)?)\s+bathrooms?\b",
            visible_text,
            re.IGNORECASE,
        )
        bathrooms = float(bathroom_match.group(1)) if bathroom_match else None
        property_match = re.search(
            r"\bEntire\s+(villa|home|house|apartment|cottage|bungalow)\b",
            visible_text,
            re.IGNORECASE,
        )
        property_type = property_match.group(1).title() if property_match else None

        structural = _structural_from_meta(html)
        title = title or structural.get("title")
        bedrooms = structural.get("bedrooms", bedrooms)
        bathrooms = structural.get("bathrooms", bathrooms)
        capacity = structural.get("capacity", capacity)
        return PropertyRef(
            site="airbnb",
            raw_id=raw_id,
            url=url,
            licence_number=registration_ids[0] if registration_ids else None,
            registration_ids=registration_ids,
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            capacity=capacity,
            property_type=property_type,
            title=title,
            gallery=image_urls,
            extra={
                "identity_source": "public_server_rendered_html",
                "registration_tokens": sorted(registration_tokens(page_text)),
            },
        )

    @staticmethod
    def _extract_deferred_state(html: str) -> tuple[dict, str | None]:
        """Returns (state, error). error is None on success, otherwise a
        specific reason: 'not_found' (the script tag itself is missing) or
        the actual JSONDecodeError message (the tag was there but its
        content didn't parse -- e.g. truncated response, or a shape change
        that needs a real look, not a guess)."""
        m = _DEFERRED_STATE_RE.search(html)
        if not m:
            return {}, "not_found"
        try:
            return json.loads(m.group(1)), None
        except json.JSONDecodeError as e:
            return {}, f"json_decode_error: {e}"

    @staticmethod
    def _diagnose(html: str, parse_error: str | None) -> str:
        """Called only when we came away with no usable state, so we report
        what actually happened instead of a bare "not found" -- the
        difference between a one-line error and knowing whether it's a
        bot-detection page, a truncated response, or a shape change."""
        if parse_error and parse_error != "not_found":
            return f"data-deferred-state-0 was present but failed to parse: {parse_error}"

        low = html.lower()
        # NOTE: "captcha"/"recaptcha" show up in Airbnb's own feature-flag
        # config on completely normal pages (disable_google_recaptcha,
        # niobe_disable_recaptcha_retrier, etc) -- confirmed 22 Sep via
        # tools/debug_airbnb.py, so that text alone is NOT a reliable
        # bot-detection signal. Only a much more specific phrase counts.
        if "verify you are a human" in low or "press & hold" in low or "unusual traffic" in low:
            hint = "looks like a real bot-detection/CAPTCHA challenge page"
        elif len(html) < 5000:
            hint = "response body is suspiciously short for a listing page"
        elif "<title>" not in low:
            hint = "no <title> tag -- likely not a normal listing page"
        else:
            hint = "page loaded normally but had no data-deferred-state-0 script"
        snippet = re.sub(r"\s+", " ", html[:300]).strip()
        return f"{hint}; length={len(html)}; starts with: {snippet!r}"

    def get_price(self, ref: PropertyRef, checkin: date, checkout: date, guests: int) -> PriceResult:
        currency = str(ref.extra.get("quote_currency") or "EUR").upper()

        def _err(outcome: Outcome, notes: str, source_url: str | None = None) -> PriceResult:
            return PriceResult(
                site=self.site, ref=ref, outcome=outcome, checkin=checkin, checkout=checkout,
                guests=guests, currency=currency, notes=notes, source_url=source_url,
            )

        url, params, payload, headers = _stays_pdp_request(
            ref,
            checkin,
            checkout,
            guests,
            currency,
        )
        try:
            pricing_post = getattr(self._client, "post_pricing", None)
            resp = (
                pricing_post(url, params=params, json=payload, headers=headers)
                if callable(pricing_post)
                else self._client.post(url, params=params, json=payload, headers=headers)
            )
        except httpx.HTTPError as e:
            return _err(Outcome.ERROR, str(e))

        if resp.status_code == 429:
            return _err(Outcome.BLOCKED, _rate_limit_notes(resp), str(resp.url))
        if resp.status_code == 404:
            return _err(Outcome.NOT_FOUND, "pricing endpoint returned 404", str(resp.url))
        if resp.status_code >= 400:
            return _err(Outcome.ERROR, f"HTTP {resp.status_code}", str(resp.url))

        try:
            state = resp.json()
        except (json.JSONDecodeError, ValueError):
            state, parse_error = self._extract_deferred_state(resp.text)
            if not state:
                return _err(
                    Outcome.ERROR,
                    "pricing endpoint returned neither JSON nor supported embedded state: "
                    + self._diagnose(resp.text, parse_error),
                    str(resp.url),
                )
        errors = state.get("errors") if isinstance(state, dict) else None
        if isinstance(errors, list) and errors:
            messages = [
                str(item.get("message") or "unknown GraphQL error")
                for item in errors
                if isinstance(item, dict)
            ]
            return _err(
                Outcome.ERROR,
                "pricing endpoint rejected the persisted query: " + "; ".join(messages[:3]),
                str(resp.url),
            )

        structured_price = _find_key(state, "structuredDisplayPrice")
        available = _find_key(state, "isAvailable")
        if available is None:
            available = _find_key(state, "available")
        raw_minimum_stay = _find_key(state, "minimumStay") or _find_key(state, "minNights")
        minimum_stay = None
        if not isinstance(raw_minimum_stay, bool):
            try:
                parsed_minimum_stay = int(raw_minimum_stay)
            except (TypeError, ValueError):
                parsed_minimum_stay = 0
            if parsed_minimum_stay > 0:
                minimum_stay = parsed_minimum_stay
        if available is False:
            requested_nights = (checkout - checkin).days
            if minimum_stay is not None and minimum_stay > requested_nights:
                return PriceResult(
                    site=self.site,
                    ref=ref,
                    outcome=Outcome.MIN_STAY,
                    checkin=checkin,
                    checkout=checkout,
                    guests=guests,
                    currency=currency,
                    minimum_stay_nights=minimum_stay,
                    notes=(
                        f"minimum stay is {minimum_stay} nights for the requested dates"
                    ),
                    source_url=str(resp.url),
                )
            return _err(Outcome.UNAVAILABLE, "no price data for these dates (likely sold out)", str(resp.url))
        if not isinstance(structured_price, dict):
            return _err(
                Outcome.ERROR,
                "checkout price rejected: structuredDisplayPrice is missing",
                str(resp.url),
            )
        if _BEFORE_TAXES_RE.search(json.dumps(structured_price, ensure_ascii=False)):
            return _err(
                Outcome.INCOMPLETE_EVIDENCE,
                "checkout price rejected: Airbnb showed a total before taxes, so it is "
                "not the guest total (set PARITY_AIRBNB_COUNTRY to the guest's country)",
                str(resp.url),
            )
        observed_currency = _observed_currency(structured_price, currency)
        if observed_currency is None:
            return _err(
                Outcome.INCOMPLETE_EVIDENCE,
                "checkout price rejected: displayed currency could not be verified",
                str(resp.url),
            )
        if observed_currency != currency:
            return _err(
                Outcome.INCOMPLETE_EVIDENCE,
                "checkout price rejected: requested currency "
                f"{currency}, displayed {observed_currency}",
                str(resp.url),
            )
        price_details = _find_key(structured_price, "priceDetails")
        items = _price_detail_items(price_details)
        if not items:
            return _err(
                Outcome.ERROR,
                "checkout price rejected: itemized priceDetails are missing",
                str(resp.url),
            )

        totals = {
            "accommodation": 0.0,
            "discount": 0.0,
            "cleaning": 0.0,
            "service_fee": 0.0,
            "mandatory_fee": 0.0,
            "tax": 0.0,
            "deposit": 0.0,
        }
        line_items: list[PriceLineItem] = []
        precise_checkout_total = _precise_checkout_total(state, observed_currency)
        displayed_total = (
            precise_checkout_total
            if precise_checkout_total is not None
            else _displayed_total(structured_price, items, (checkout - checkin).days)
        )
        unknown: list[str] = []
        for item in items:
            label = _line_label(item)
            amount = _line_amount(item)
            if amount is None:
                continue
            category = _line_category(label)
            if category == "total":
                continue
            if category is None:
                unknown.append(label or "unlabelled monetary line")
                continue
            signed_amount = -abs(amount) if category == "discount" else amount
            totals[category] += abs(amount) if category == "discount" else amount
            line_items.append(
                PriceLineItem(
                    label=label,
                    amount=round(signed_amount, 2),
                    category=category,
                    currency=observed_currency,
                    included_in_displayed_total=True,
                )
            )

        if unknown:
            return _err(
                Outcome.ERROR,
                "checkout price rejected: unrecognized monetary line item(s): "
                + ", ".join(unknown),
                str(resp.url),
            )
        if not line_items or totals["accommodation"] <= 0:
            return _err(
                Outcome.ERROR,
                "checkout price rejected: no explicit accommodation line item",
                str(resp.url),
            )
        if displayed_total is None:
            return _err(
                Outcome.ERROR,
                "checkout price rejected: displayed total is missing",
                str(resp.url),
            )

        calculated_displayed = round(sum(item.amount for item in line_items), 2)
        difference = round(calculated_displayed - displayed_total, 2)
        if abs(difference) > _RECONCILIATION_TOLERANCE:
            return PriceResult(
                site=self.site,
                ref=ref,
                outcome=Outcome.ERROR,
                checkin=checkin,
                checkout=checkout,
                guests=guests,
                currency=currency,
                headline_total=round(displayed_total, 2),
                line_items=line_items,
                reconciliation_difference=difference,
                source_url=str(resp.url),
                notes=(
                    "checkout price rejected: itemized sum "
                    f"{calculated_displayed:.2f} does not reconcile to displayed total "
                    f"{displayed_total:.2f} (difference {difference:+.2f})"
                ),
            )

        breakdown = PriceBreakdown(
            nightly_total=round(totals["accommodation"], 2),
            discount=round(totals["discount"], 2) or None,
            cleaning=round(totals["cleaning"], 2) or None,
            service_fee=round(totals["service_fee"], 2) or None,
            other_mandatory_fees=round(totals["mandatory_fee"], 2) or None,
            tourist_tax=round(totals["tax"], 2) or None,
            deposit=round(totals["deposit"], 2) or None,
        )

        cxl = _find_key(state, "cancellationPolicy") or _find_key(state, "cancellationPolicyType")
        cancellation_type = cxl if isinstance(cxl, str) else None
        refundable = None
        if cancellation_type:
            low = cancellation_type.lower()
            refundable = "flexible" in low or "moderate" in low or "free" in low

        return PriceResult(
            site=self.site,
            ref=ref,
            outcome=Outcome.PRICED,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            currency=currency,
            comparable_total=comparable_total(breakdown),
            headline_total=round(displayed_total, 2),
            breakdown=breakdown,
            line_items=line_items,
            reconciliation_difference=difference,
            refundable=refundable,
            cancellation_type=cancellation_type,
            minimum_stay_nights=minimum_stay,
            source_url=str(resp.url),
            notes="itemized checkout price reconciled to displayed total",
        )
