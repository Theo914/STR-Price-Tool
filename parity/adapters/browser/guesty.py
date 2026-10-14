"""Guesty direct-site adapter -- the backend returns nothing to a plain
fetch (confirmed in research), so this reads the rendered page's visible
text (Subtotal / Fees / Total), the same way the doc describes checking
it manually.

Recipe (NOT yet live-tested against this codebase):
- URL: /en/properties/{24-hex id}?checkIn=YYYY-MM-DD&checkOut=YYYY-MM-DD&minOccupancy=2
- Page shows Subtotal / Fees / Total (confirmed example: EUR 584.25 + 40 = 624.25)
- "Fees" bundles cleaning and any other fees; taxes need checking per site
  (no single confirmed rule here, unlike TS/Lodgify/Hostaway) -- so this
  adapter reports whatever tax-labelled line it finds, but doesn't assume
  it's excluded or included in the way the other sites are.
"""
from __future__ import annotations

import re
from datetime import date

from parity.adapters.base import NeedsBrowser
from parity.adapters.browser.browser_session import BrowserSession
from parity.core.models import Outcome, PriceBreakdown, PriceResult, PropertyRef
from parity.core.pricing import comparable_total
from parity.core.textparse import MONEY_RE, parse_money

_ID_RE = re.compile(r"/properties/([0-9a-f]{24})")
class GuestyAdapter(NeedsBrowser):
    site = "guesty"

    def __init__(self, session: BrowserSession):
        self._session = session

    def identify(self, url: str) -> PropertyRef:
        match = _ID_RE.search(url)
        raw_id = match.group(1) if match else url  # fall back to URL if the id isn't in this path shape

        page = self._session.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            BrowserSession.decline_cookies(page)
            title = page.title()
        finally:
            page.close()

        return PropertyRef(site=self.site, raw_id=raw_id, url=url, title=title)

    def get_price(self, ref: PropertyRef, checkin: date, checkout: date, guests: int) -> PriceResult:
        def _err(outcome: Outcome, notes: str, source_url: str | None = None) -> PriceResult:
            return PriceResult(
                site=self.site, ref=ref, outcome=outcome, checkin=checkin, checkout=checkout,
                guests=guests, notes=notes, source_url=source_url,
            )

        sep = "&" if "?" in ref.url else "?"
        url = f"{ref.url}{sep}checkIn={checkin.isoformat()}&checkOut={checkout.isoformat()}&minOccupancy={guests}"

        page = self._session.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            BrowserSession.decline_cookies(page)
            page.wait_for_timeout(1000)

            body_text = page.inner_text("body")
            low = body_text.lower()

            if "not available" in low or "sold out" in low or "no longer available" in low:
                return _err(Outcome.UNAVAILABLE, "page indicates no availability", page.url)
            if "minimum stay" in low or "minimum nights" in low:
                return _err(Outcome.MIN_STAY, "page indicates a minimum-stay restriction", page.url)

            lines = body_text.splitlines()
            subtotal = fees = total = tax = None
            for i, line in enumerate(lines):
                l = line.lower().strip()
                if l == "subtotal" or l.startswith("subtotal"):
                    subtotal = parse_money(line) or (parse_money(lines[i + 1]) if i + 1 < len(lines) else None)
                elif l == "fees" or l.startswith("fees") or "cleaning" in l:
                    fees = (fees or 0) + (parse_money(line) or (parse_money(lines[i + 1]) if i + 1 < len(lines) else 0) or 0)
                elif l == "total" or l.startswith("total"):
                    total = parse_money(line) or (parse_money(lines[i + 1]) if i + 1 < len(lines) else None)
                elif "tax" in l:
                    tax = parse_money(line) or (parse_money(lines[i + 1]) if i + 1 < len(lines) else None)

            if subtotal is None and total is None:
                return _err(Outcome.ERROR, "could not find Subtotal/Fees/Total on the rendered page", page.url)

            breakdown = PriceBreakdown(
                nightly_total=subtotal if subtotal is not None else total,
                other_mandatory_fees=fees if subtotal is not None else None,
                tourist_tax=tax,
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
                headline_total=total,
                breakdown=breakdown,
                notes="tax handling unconfirmed for Guesty -- reported as found, not excluded/included by rule" if tax else None,
                source_url=page.url,
            )
        finally:
            page.close()
