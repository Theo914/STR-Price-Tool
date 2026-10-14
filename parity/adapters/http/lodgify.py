"""Lodgify direct-site adapter — plain HTTP JSON API.

Recipe (verified live on Villa Bracchi, 4-11 Oct: 4550 + 600 cleaning =
5150 comparable, 49 tourist tax excluded):
- Detected by `websites-static.lodgify.com` / `checkout.lodgify.com`
  scripts (see registry.match_by_html).
- Price API: GET https://checkout.lodgify.com/api/v2/websites/{slug}/rentals/{propertyId}/price
  ?adults=&arrival=YYYY-MM-DD&departure=YYYY-MM-DD&language=en
- Response: `rentalPrice.totalWithPromotions`, `fees.details[]` (cleaning
  etc, use `fees.total`), `localTaxes` (tourist tax, excluded),
  `totalPrice.total` (headline).
- Comparable price = rentalPrice.totalWithPromotions + fees.total,
  excluding localTaxes.
- Availability: GET .../rentals/{propertyId}/calendar?startDate=

`identify()` needs the site slug and propertyId, which live in the page's
own network calls rather than in a clean HTML tag — the doc notes these
come from `checkout.lodgify.com/api/v1/checkout/calendar?propertyId=`.
Since the slug/id pairing isn't reliably scrapable from raw page HTML,
`identify()` accepts either a full lodgify listing URL (parsed for a slug
and numeric id in the path/query) or the caller can construct a
PropertyRef directly with `extra={"slug": ..., "property_id": ...}` when
those were captured from the site's own network traffic (e.g. via a one-off
browser inspection). This is the one HTTP adapter most likely to need a
manual assist per new host site.

NOT yet live-tested against this exact codebase (no egress to
checkout.lodgify.com from this sandbox); the price-endpoint recipe itself
was manually verified live during research.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx

from parity.adapters.base import HttpOnly
from parity.core.models import Outcome, PriceBreakdown, PriceResult, PropertyRef
from parity.core.pricing import comparable_total

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_ID_IN_PATH_RE = re.compile(r"/(\d{4,})(?:/|$)")


class LodgifyAdapter(HttpOnly):
    site = "lodgify"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(headers=_HEADERS, timeout=20.0, follow_redirects=True)

    def identify(self, url: str) -> PropertyRef:
        parsed = urlparse(url)
        slug = None
        property_id = None

        # checkout.lodgify.com/{slug}/{propertyId}/reservation style URL
        parts = [p for p in parsed.path.split("/") if p]
        if len(parsed.netloc.split(".")) and "checkout.lodgify.com" in parsed.netloc and len(parts) >= 2:
            slug, maybe_id = parts[0], parts[1]
            if maybe_id.isdigit():
                property_id = maybe_id

        if property_id is None:
            qs = parse_qs(parsed.query)
            if "propertyId" in qs:
                property_id = qs["propertyId"][0]
            else:
                m = _ID_IN_PATH_RE.search(parsed.path)
                if m:
                    property_id = m.group(1)

        if slug is None:
            # fall back to the marketing-site subdomain as the slug guess;
            # this is the part most likely to need a manual override.
            host_parts = parsed.netloc.split(".")
            slug = host_parts[0] if host_parts else None

        if property_id is None:
            raise ValueError(
                f"Could not extract a Lodgify propertyId from {url!r}; "
                "capture it from the site's own network calls "
                "(checkout.lodgify.com/api/v1/checkout/calendar?propertyId=) "
                "and construct the PropertyRef manually with extra={'slug', 'property_id'}."
            )

        return PropertyRef(
            site=self.site,
            raw_id=property_id,
            url=url,
            extra={"slug": slug, "property_id": property_id},
        )

    def get_price(self, ref: PropertyRef, checkin: date, checkout: date, guests: int) -> PriceResult:
        def _err(outcome: Outcome, notes: str, source_url: str | None = None) -> PriceResult:
            return PriceResult(
                site=self.site, ref=ref, outcome=outcome, checkin=checkin, checkout=checkout,
                guests=guests, notes=notes, source_url=source_url,
            )

        slug = ref.extra.get("slug")
        property_id = ref.extra.get("property_id", ref.raw_id)
        if not slug or not property_id:
            return _err(Outcome.ERROR, "PropertyRef is missing slug/property_id needed for the price API")

        url = f"https://checkout.lodgify.com/api/v2/websites/{slug}/rentals/{property_id}/price"
        params = {
            "adults": guests,
            "arrival": checkin.isoformat(),
            "departure": checkout.isoformat(),
            "language": "en",
        }
        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as e:
            return _err(Outcome.ERROR, str(e))

        if resp.status_code == 404:
            return _err(Outcome.NOT_FOUND, "price endpoint 404 (wrong slug/propertyId?)", str(resp.url))
        if resp.status_code >= 400:
            return _err(Outcome.ERROR, f"HTTP {resp.status_code}", str(resp.url))

        try:
            data = resp.json()
        except ValueError:
            return _err(Outcome.ERROR, "non-JSON response from price endpoint", str(resp.url))

        if data.get("isAvailable") is False or data.get("available") is False:
            return _err(Outcome.UNAVAILABLE, "isAvailable=false for these dates", str(resp.url))

        min_stay_error = data.get("minimumStay") or data.get("minStayError")
        if min_stay_error and not data.get("rentalPrice"):
            return _err(Outcome.MIN_STAY, f"minimum stay restriction: {min_stay_error}", str(resp.url))

        rental_price = data.get("rentalPrice") or {}
        nightly_total = rental_price.get("totalWithPromotions")
        if nightly_total is None:
            return _err(Outcome.ERROR, "response had no rentalPrice.totalWithPromotions", str(resp.url))

        fees = data.get("fees") or {}
        fees_total = fees.get("total")

        local_taxes = data.get("localTaxes")
        tourist_tax = None
        if isinstance(local_taxes, (int, float)):
            tourist_tax = local_taxes
        elif isinstance(local_taxes, dict):
            tourist_tax = local_taxes.get("total")

        total_price = data.get("totalPrice") or {}
        headline = total_price.get("total")

        breakdown = PriceBreakdown(
            nightly_total=nightly_total,
            other_mandatory_fees=fees_total,
            tourist_tax=tourist_tax,
        )

        return PriceResult(
            site=self.site,
            ref=ref,
            outcome=Outcome.PRICED,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            currency=data.get("currencyCode", "EUR"),
            comparable_total=comparable_total(breakdown),
            headline_total=headline,
            breakdown=breakdown,
            source_url=str(resp.url),
        )
