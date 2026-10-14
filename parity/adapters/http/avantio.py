"""Avantio-powered direct site adapter — plain HTTP, query-param booking form.

Recipe (verified live against the Casa Tatano host's own site):
- Detected by `crs.avantio.com` scripts in the page (see registry.match_by_html).
- URL: `?FRMEntrada=dd/mm/yyyy&FRMSalida=dd/mm/yyyy&FRMAdultos=2`.
- The page lists a "Not included in the price (payable in cash on site)"
  block (typically the tourist/city tax) that must be ADDED to the headline
  to get the true stay cost, but EXCLUDED from the comparable total per the
  tax rule — so it's tracked as tourist_tax, not added into comparable_total.
- Confirmed example: headline €3,220 (7 nights x €460), cash-on-site tax
  excluded, cancellation non-refundable -> comparable total €3,640 once the
  price shown already includes the cleaning fee for this property (per the
  research doc's verified breakdown table).

NOT yet live-tested against this exact codebase (no egress to the direct
site's own domain from this sandbox, and Avantio direct sites vary in
markup per property/host, unlike TS/Airbnb which have a fixed shape). This
adapter is written to be resilient to markup differences: it looks for
labelled price lines by keyword rather than fixed CSS selectors/JSON paths,
but it should be pointed at a real saved HTML fixture and adjusted before
being trusted on a new host site.
"""
from __future__ import annotations

import re
from datetime import date

import httpx
from bs4 import BeautifulSoup

from parity.adapters.base import HttpOnly
from parity.core.models import Outcome, PriceBreakdown, PriceResult, PropertyRef
from parity.core.pricing import comparable_total
from parity.core.textparse import MONEY_RE, parse_money

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

class AvantioAdapter(HttpOnly):
    site = "avantio"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(headers=_HEADERS, timeout=20.0, follow_redirects=True)

    def identify(self, url: str) -> PropertyRef:
        resp = self._client.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        title_node = soup.find("h1")
        title = title_node.get_text(strip=True) if title_node else None

        gallery = [
            img.get("src") or img.get("data-src")
            for img in soup.find_all("img")
            if (img.get("src") or img.get("data-src"))
        ]
        gallery = [g for g in gallery if g and not g.startswith("data:")][:60]

        licence = None
        lic_match = re.search(r"\b([A-Z]{2,4}[-/]?[A-Z0-9]{3,}[-/]?\d{3,})\b", resp.text)
        if lic_match:
            licence = lic_match.group(1)

        return PropertyRef(
            site=self.site,
            raw_id=url,  # Avantio direct sites have no stable numeric id across hosts; key by URL
            url=url,
            licence_number=licence,
            title=title,
            gallery=gallery,
            extra={},
        )

    def get_price(self, ref: PropertyRef, checkin: date, checkout: date, guests: int) -> PriceResult:
        def _err(outcome: Outcome, notes: str, source_url: str | None = None) -> PriceResult:
            return PriceResult(
                site=self.site, ref=ref, outcome=outcome, checkin=checkin, checkout=checkout,
                guests=guests, notes=notes, source_url=source_url,
            )

        params = {
            "FRMEntrada": checkin.strftime("%d/%m/%Y"),
            "FRMSalida": checkout.strftime("%d/%m/%Y"),
            "FRMAdultos": guests,
        }
        try:
            resp = self._client.get(ref.url, params=params)
        except httpx.HTTPError as e:
            return _err(Outcome.ERROR, str(e))

        if resp.status_code >= 400:
            return _err(Outcome.ERROR, f"HTTP {resp.status_code}", str(resp.url))

        text = resp.text
        low = text.lower()

        if "not available" in low or "no disponible" in low or "sold out" in low:
            return _err(Outcome.UNAVAILABLE, "page indicates no availability", str(resp.url))
        if "minimum stay" in low or "estancia m" in low:
            return _err(Outcome.MIN_STAY, "page indicates a minimum-stay restriction", str(resp.url))

        soup = BeautifulSoup(text, "html.parser")
        body_text = soup.get_text(separator="\n")

        headline = None
        for line in body_text.splitlines():
            l = line.lower()
            if "total" in l and MONEY_RE.search(line):
                headline = parse_money(line)
                if headline:
                    break

        if headline is None:
            return _err(Outcome.ERROR, "could not find a headline total on the booking page", str(resp.url))

        tourist_tax = None
        for line in body_text.splitlines():
            l = line.lower()
            if ("not included" in l or "no incluid" in l or "payable" in l or "pagar" in l) and MONEY_RE.search(line):
                tourist_tax = parse_money(line)
                break

        non_refundable = "non-refundable" in low or "no reembolsable" in low or "sin reembolso" in low

        breakdown = PriceBreakdown(
            nightly_total=headline,
            tourist_tax=tourist_tax,
        )

        return PriceResult(
            site=self.site,
            ref=ref,
            outcome=Outcome.PRICED,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            currency="EUR",
            comparable_total=comparable_total(breakdown),
            headline_total=headline,
            breakdown=breakdown,
            refundable=(not non_refundable) if non_refundable else None,
            cancellation_type="non-refundable" if non_refundable else None,
            source_url=str(resp.url),
        )
