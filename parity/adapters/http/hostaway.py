"""Hostaway direct-site adapter — plain HTTP: a GET calendar check plus a
POST for the itemized price. No booking is created; priceDetails is a
read-only quote endpoint.

Recipe (verified live on Colina Del Mar, 17-24 Oct: headline 3550 included
a 750 damage deposit; true comparable = 2450 accommodation + 350 cleaning
= 2800):
- Detected by `*.holidayfuture.com` hosts, or `static-production-nextjs
  .hostaway.com` scripts on a custom domain (see registry).
- Page URL: /listings/{id}?start=YYYY-MM-DD&end=YYYY-MM-DD&numberOfGuests=2
- Calendar: GET https://booking-engine.hostaway.com/bookingEngines/{slug}/listings/{id}/calendar?startingDate=
  -> per-day isAvailable / price / minimumStay.
- Price: POST .../listings/{id}/calendar/priceDetails
  body: {"startingDate": ..., "endingDate": ..., "numberOfGuests": ...}
  -> {"components": [{"type": "accommodation"|"fee"|"tax"|"damageDeposit"|"other", "name": ..., "total": ...}]}
- Comparable price = sum of accommodation + fee components, EXCLUDING
  damageDeposit and any tax components. This is the one site where the
  headline total is actively misleading (27% overstatement on the tested
  property) because it folds in the refundable damage deposit.

`bookingEngines/{slug}` is not derivable from the public listing URL alone
in every case; identify() tries to read it out of the listing page's own
script tags (the same `static-production-nextjs.hostaway.com` bundle that
the registry sniffs for), falling back to the netloc's first label as a
guess. If that guess is wrong for a given host site, override it via
PropertyRef.extra={"slug": ...} after a one-off inspection of that site's
network calls.

NOT yet live-tested against this exact codebase (no egress to
booking-engine.hostaway.com from this sandbox); the price-endpoint recipe
and the damage-deposit bug were both manually verified live during
research.
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

import httpx

from parity.adapters.base import HttpOnly
from parity.core.models import Outcome, PriceBreakdown, PriceResult, PropertyRef
from parity.core.pricing import comparable_total

_ID_RE = re.compile(r"/listings/(\d+)")
_SLUG_RE = re.compile(r"bookingEngines/([a-zA-Z0-9_-]+)/listings")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_EXCLUDED_TYPES = {"damagedeposit", "tax", "taxes"}
_INCLUDED_TYPES = {"accommodation", "fee", "fees", "other"}


class HostawayAdapter(HttpOnly):
    site = "hostaway"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(headers=_HEADERS, timeout=20.0, follow_redirects=True)

    def identify(self, url: str) -> PropertyRef:
        match = _ID_RE.search(urlparse(url).path)
        if not match:
            raise ValueError(f"Not a recognizable Hostaway listing URL: {url}")
        raw_id = match.group(1)

        resp = self._client.get(url)
        resp.raise_for_status()
        html = resp.text

        slug_match = _SLUG_RE.search(html)
        slug = slug_match.group(1) if slug_match else urlparse(url).netloc.split(".")[0]

        title_match = re.search(r"<title>([^<]+)</title>", html)
        title = title_match.group(1).strip() if title_match else None

        return PropertyRef(
            site=self.site,
            raw_id=raw_id,
            url=url,
            title=title,
            extra={"slug": slug},
        )

    def get_price(self, ref: PropertyRef, checkin: date, checkout: date, guests: int) -> PriceResult:
        def _err(outcome: Outcome, notes: str, source_url: str | None = None) -> PriceResult:
            return PriceResult(
                site=self.site, ref=ref, outcome=outcome, checkin=checkin, checkout=checkout,
                guests=guests, notes=notes, source_url=source_url,
            )

        slug = ref.extra.get("slug")
        if not slug:
            return _err(Outcome.ERROR, "PropertyRef is missing the Hostaway bookingEngine slug")

        base = f"https://booking-engine.hostaway.com/bookingEngines/{slug}/listings/{ref.raw_id}"

        try:
            cal_resp = self._client.get(f"{base}/calendar", params={"startingDate": checkin.isoformat()})
        except httpx.HTTPError as e:
            return _err(Outcome.ERROR, str(e))

        if cal_resp.status_code >= 400:
            return _err(Outcome.ERROR, f"calendar HTTP {cal_resp.status_code}", str(cal_resp.url))

        try:
            cal_data = cal_resp.json()
        except ValueError:
            cal_data = None

        days = cal_data.get("days") if isinstance(cal_data, dict) else cal_data
        if isinstance(days, list):
            in_range = [
                d for d in days
                if isinstance(d, dict) and checkin.isoformat() <= str(d.get("date", "")) < checkout.isoformat()
            ]
            if in_range and any(d.get("isAvailable") is False for d in in_range):
                return _err(Outcome.UNAVAILABLE, "calendar shows one or more nights unavailable", str(cal_resp.url))
            min_stays = [d.get("minimumStay") for d in in_range if d.get("minimumStay")]
            nights = (checkout - checkin).days
            if min_stays and nights < max(min_stays):
                return _err(Outcome.MIN_STAY, f"minimum stay is {max(min_stays)} nights", str(cal_resp.url))

        body = {
            "startingDate": checkin.isoformat(),
            "endingDate": checkout.isoformat(),
            "numberOfGuests": guests,
        }
        try:
            price_resp = self._client.post(f"{base}/calendar/priceDetails", json=body)
        except httpx.HTTPError as e:
            return _err(Outcome.ERROR, str(e))

        if price_resp.status_code == 404:
            return _err(Outcome.NOT_FOUND, "priceDetails endpoint 404", str(price_resp.url))
        if price_resp.status_code >= 400:
            return _err(Outcome.ERROR, f"priceDetails HTTP {price_resp.status_code}", str(price_resp.url))

        try:
            price_data = price_resp.json()
        except ValueError:
            return _err(Outcome.ERROR, "non-JSON response from priceDetails", str(price_resp.url))

        components = price_data.get("components") or []
        if not components:
            return _err(Outcome.ERROR, "priceDetails returned no components", str(price_resp.url))

        accommodation = 0.0
        fees = 0.0
        deposit = None
        headline_reported = price_data.get("totalPrice") or price_data.get("total")

        for c in components:
            ctype = str(c.get("type", "")).lower()
            total = c.get("total")
            if total is None:
                continue
            if ctype == "accommodation":
                accommodation += total
            elif ctype in ("fee", "fees"):
                fees += total
            elif ctype == "damagedeposit":
                deposit = (deposit or 0.0) + total
            elif ctype in ("tax", "taxes"):
                pass  # excluded from comparable total, not tracked as tourist_tax specifically
            elif ctype == "other":
                fees += total

        breakdown = PriceBreakdown(
            nightly_total=round(accommodation, 2) if accommodation else None,
            other_mandatory_fees=round(fees, 2) if fees else None,
            deposit=deposit,
        )

        if breakdown.nightly_total is None:
            return _err(Outcome.ERROR, "no 'accommodation' component in priceDetails", str(price_resp.url))

        return PriceResult(
            site=self.site,
            ref=ref,
            outcome=Outcome.PRICED,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            currency=price_data.get("currency", "EUR"),
            comparable_total=comparable_total(breakdown),
            headline_total=headline_reported,
            breakdown=breakdown,
            notes="damage deposit stripped from headline per confirmed recipe" if deposit else None,
            source_url=str(price_resp.url),
        )
