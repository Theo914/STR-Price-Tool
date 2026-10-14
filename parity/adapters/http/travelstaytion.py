"""TravelStaytion adapter — plain HTTP JSON APIs, no browser needed.

Recipe (from parity-price-verification-2026-09-22.md, manually verified live):
- Listing page embeds `window.property = {...}` with licenseNumber, lat/lng,
  bedrooms, capacity and gallery[].url.
- Quote: GET /platform/api/calendar/quote/{id}?currency=&checkin=&checkout=&guests=
  `price.totalNoDiscountDecimal` is the comparable price (nightly + cleaning,
  tourist tax already excluded relative to the discounted `totalDecimal`).
  Confirmed rule: TS comparable price = totalNoDiscountDecimal; TS cleaning
  fee is included in the price already.

NOT yet live-tested against this exact codebase (device_bash's sandbox has no
egress to travelstaytion.com) — the recipe itself was manually verified live
during research. Verify the exact JSON field names against a real response
the first time this runs on Theo's machine, and adjust `_extract_property`
/ `_extract_quote` if the shape has drifted.
"""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlparse

import httpx

from parity.adapters.base import HttpOnly
from parity.core.models import (
    Outcome,
    PriceBreakdown,
    PriceLineItem,
    PriceResult,
    PropertyRef,
)
from parity.core.pricing import comparable_total

_ID_RE = re.compile(r"/stay/(\d+)")
_PROPERTY_JSON_RE = re.compile(r"window\.property\s*=\s*(\{.*?\});", re.DOTALL)
_CALENDAR_JSON_RE = re.compile(
    r"window\.calendar\s*=\s*JSON\.parse\('((?:\\.|[^'])*)'\)\s*;",
    re.DOTALL,
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}


class TravelStaytionAdapter(HttpOnly):
    site = "travelstaytion"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(headers=_HEADERS, timeout=20.0, follow_redirects=True)
        self._calendar_cache: dict[str, dict] = {}

    def identify(self, url: str) -> PropertyRef:
        match = _ID_RE.search(urlparse(url).path)
        if not match:
            raise ValueError(f"Not a recognizable TravelStaytion listing URL: {url}")
        raw_id = match.group(1)

        resp = self._client.get(url)
        resp.raise_for_status()
        prop = self._extract_property(resp.text)

        embedded_id = prop.get("id")
        if embedded_id is not None and str(embedded_id) != raw_id:
            raise ValueError(
                f"TravelStaytion payload id {embedded_id!r} did not match URL id {raw_id!r}"
            )

        gallery = [g.get("url") for g in prop.get("gallery", []) if isinstance(g, dict) and g.get("url")]
        registration_ids = []
        for value in (
            prop.get("licenseNumber"),
            prop.get("licenceNumber"),
            prop.get("nationalIdentificationCode"),
        ):
            if value and value not in registration_ids:
                registration_ids.append(str(value))

        amenity_names = []
        for amenity in prop.get("amenities", []):
            if isinstance(amenity, str):
                name = amenity
            elif isinstance(amenity, dict):
                name = amenity.get("title") or amenity.get("name")
            else:
                name = None
            if name and name not in amenity_names:
                amenity_names.append(str(name))

        return PropertyRef(
            site=self.site,
            raw_id=raw_id,
            url=url,
            licence_number=registration_ids[0] if registration_ids else None,
            registration_ids=registration_ids,
            lat=prop.get("lat") if prop.get("lat") is not None else prop.get("latitude"),
            lng=prop.get("lng") if prop.get("lng") is not None else prop.get("longitude"),
            country=prop.get("country"),
            city=prop.get("city"),
            postcode=prop.get("postcode"),
            bedrooms=prop.get("bedrooms"),
            bathrooms=prop.get("bathrooms"),
            capacity=prop.get("capacity"),
            title=prop.get("name") or prop.get("title"),
            property_type=prop.get("propertyType"),
            amenities=amenity_names,
            gallery=gallery,
            extra={},
        )

    @staticmethod
    def _extract_property(html: str) -> dict:
        m = _PROPERTY_JSON_RE.search(html)
        if not m:
            raise ValueError("TravelStaytion page had no embedded window.property payload")
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise ValueError(f"TravelStaytion property payload was invalid JSON: {e}") from e

    @staticmethod
    def _extract_calendar(html: str) -> dict:
        match = _CALENDAR_JSON_RE.search(html)
        if not match:
            raise ValueError("TravelStaytion page had no embedded window.calendar payload")
        payload = match.group(1).replace(r"\'", "'")
        try:
            calendar = json.loads(payload)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"TravelStaytion calendar payload was invalid JSON: {error}"
            ) from error
        if not isinstance(calendar, dict):
            raise ValueError("TravelStaytion calendar payload was not an object")
        return calendar

    def _calendar_for(self, ref: PropertyRef) -> dict:
        calendar = self._calendar_cache.get(ref.raw_id)
        if calendar is None:
            response = self._client.get(ref.url)
            response.raise_for_status()
            calendar = self._extract_calendar(response.text)
            self._calendar_cache[ref.raw_id] = calendar
        return calendar

    def open_checkin_dates(self, ref: PropertyRef) -> list[date]:
        """Dates the public calendar marks as bookable check-ins."""
        days: list[date] = []
        for item in self._calendar_for(ref).get("checkins") or []:
            if not isinstance(item, dict) or item.get("canCheckin") is not True:
                continue
            try:
                days.append(date.fromisoformat(str(item.get("date"))))
            except ValueError:
                continue
        return days

    def minimum_stay_for_checkin(self, ref: PropertyRef, checkin: date) -> int | None:
        """Read the exact date-level restriction from the public calendar.

        The listing page is cached for this adapter instance, so an automatic
        weekly search does not repeatedly fetch the same large document.
        ``None`` means the calendar did not provide trustworthy restriction
        evidence for that date; callers must not reinterpret it as one night.
        """
        calendar = self._calendar_for(ref)

        default = calendar.get("defaultMinStay")
        for item in calendar.get("checkins") or []:
            if not isinstance(item, dict) or item.get("date") != checkin.isoformat():
                continue
            if item.get("canCheckin") is not True:
                return None
            raw_minimum = item.get("minStay")
            if raw_minimum is None:
                raw_minimum = default
            if isinstance(raw_minimum, bool):
                return None
            try:
                minimum = int(raw_minimum)
            except (TypeError, ValueError):
                return None
            return minimum if minimum > 0 else None
        return None

    def get_price(self, ref: PropertyRef, checkin: date, checkout: date, guests: int) -> PriceResult:
        url = f"https://www.travelstaytion.com/platform/api/calendar/quote/{ref.raw_id}"
        requested_currency = str(ref.extra.get("quote_currency") or "EUR").upper()
        params = {
            "currency": requested_currency,
            "checkin": checkin.isoformat(),
            "checkout": checkout.isoformat(),
            "guests": guests,
        }

        def _err(
            outcome: Outcome,
            notes: str,
            source_url: str | None = None,
            *,
            minimum_stay_nights: int | None = None,
        ) -> PriceResult:
            return PriceResult(
                site=self.site, ref=ref, outcome=outcome, checkin=checkin, checkout=checkout,
                guests=guests, notes=notes, source_url=source_url,
                minimum_stay_nights=minimum_stay_nights,
            )

        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as e:
            return _err(Outcome.ERROR, str(e))

        if resp.status_code == 404:
            return _err(Outcome.NOT_FOUND, "quote endpoint 404", str(resp.url))
        if resp.status_code >= 400:
            return _err(Outcome.ERROR, f"HTTP {resp.status_code}", str(resp.url))

        try:
            envelope = resp.json()
        except ValueError:
            return _err(Outcome.ERROR, "non-JSON response from quote endpoint", str(resp.url))

        if envelope.get("error"):
            return _err(Outcome.ERROR, f"quote endpoint returned error: {envelope}", str(resp.url))

        # Live response wraps everything one level deeper than the doc's
        # example: {"error": false, "data": {...actual fields...}}. Confirmed
        # live 22 Sep against Casa Tatano -- totalNoDiscountDecimal 3839.01
        # matches the researched figure exactly once unwrapped.
        data = envelope.get("data", envelope)

        raw_min_stay = data.get("minStay") or data.get("minimumStay")
        min_stay = None
        if not isinstance(raw_min_stay, bool):
            try:
                parsed_min_stay = int(raw_min_stay)
            except (TypeError, ValueError):
                parsed_min_stay = 0
            if parsed_min_stay > 0:
                min_stay = parsed_min_stay

        available = data.get("available", True)
        if not available:
            reason = str(data.get("reason") or data.get("availabilityError") or "")
            normalized_reason = reason.lower().replace("-", " ")
            if (
                "minimum stay" in normalized_reason
                or "min stay" in normalized_reason
                or (min_stay and "min" in normalized_reason)
            ):
                return _err(
                    Outcome.MIN_STAY,
                    reason or f"min stay {min_stay} nights",
                    str(resp.url),
                    minimum_stay_nights=min_stay,
                )
            return _err(Outcome.UNAVAILABLE, reason or "unavailable", str(resp.url))

        # The quote echoes the stay it actually priced; a silent change of
        # dates or guests would make the comparison meaningless.
        stay = data.get("stay")
        if isinstance(stay, dict):
            echoed = (stay.get("checkin"), stay.get("checkout"), stay.get("guests"))
            expected = (checkin.isoformat(), checkout.isoformat(), guests)
            if any(value is not None for value in echoed) and (
                str(echoed[0]) != expected[0]
                or str(echoed[1]) != expected[1]
                or str(echoed[2]) != str(expected[2])
            ):
                return _err(
                    Outcome.INCOMPLETE_EVIDENCE,
                    f"TravelStaytion quoted a different stay {echoed} than requested {expected}",
                    str(resp.url),
                )

        price = data.get("price") or {}
        if not isinstance(price, dict):
            return _err(Outcome.ERROR, "quote price block was not an object", str(resp.url))
        numeric_keys = (
            "totalNoDiscountDecimal", "totalDecimal", "cleaningDecimal", "cleaning",
            "discountDecimal", "discount", "securityDepositDecimal", "securityDeposit",
        )
        for key in numeric_keys:
            value = price.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                return _err(
                    Outcome.ERROR,
                    f"quote price.{key} was not numeric ({type(value).__name__})",
                    str(resp.url),
                )
        total_no_discount = price.get("totalNoDiscountDecimal")
        if total_no_discount is None:
            return _err(Outcome.ERROR, "quote had no price.totalNoDiscountDecimal", str(resp.url))

        cleaning = (
            price.get("cleaningDecimal")
            if price.get("cleaningDecimal") is not None
            else price.get("cleaning")
        )
        discount = (
            price.get("discountDecimal")
            if price.get("discountDecimal") is not None
            else price.get("discount")
        ) or 0.0
        deposit = (
            price.get("securityDepositDecimal")
            if price.get("securityDepositDecimal") is not None
            else price.get("securityDeposit")
        )
        headline = price.get("totalDecimal")

        tourist_tax = None
        if headline is not None:
            expected_before_tax = total_no_discount - discount
            diff = round(headline - expected_before_tax, 2)
            if diff > 0.02:
                tourist_tax = diff
            elif diff < -0.02:
                return _err(
                    Outcome.INCOMPLETE_EVIDENCE,
                    "TravelStaytion discounted total did not reconcile with "
                    "price.totalNoDiscountDecimal and price.discountDecimal",
                    str(resp.url),
                )

        nightly = round(total_no_discount - (cleaning or 0.0), 2)
        breakdown = PriceBreakdown(
            nightly_total=nightly,
            discount=discount,
            cleaning=cleaning,
            tourist_tax=tourist_tax,
            deposit=deposit,
        )
        if not data.get("currency"):
            return _err(
                Outcome.INCOMPLETE_EVIDENCE,
                "TravelStaytion quote did not state its currency",
                str(resp.url),
            )
        currency = str(data["currency"]).upper()
        if currency != requested_currency:
            return _err(
                Outcome.INCOMPLETE_EVIDENCE,
                "TravelStaytion quote currency mismatch: "
                f"requested {requested_currency}, received {currency}",
                str(resp.url),
            )
        nights = (checkout - checkin).days
        line_items = [PriceLineItem(
            label=f"{nights} nights accommodation",
            amount=nightly,
            category="accommodation",
            currency=currency,
            included_in_displayed_total=True,
        )]
        if discount:
            line_items.append(PriceLineItem(
                label="TravelStaytion discount",
                amount=-discount,
                category="discount",
                currency=currency,
                included_in_displayed_total=True,
            ))
        if cleaning:
            line_items.append(PriceLineItem(
                label="Cleaning fee",
                amount=cleaning,
                category="cleaning",
                currency=currency,
                included_in_displayed_total=True,
            ))
        if tourist_tax:
            line_items.append(PriceLineItem(
                label="Tourist tax",
                amount=tourist_tax,
                category="tax",
                currency=currency,
                included_in_displayed_total=True,
            ))

        cancellation = data.get("cancellationInfo") or {}
        # Real shape (confirmed live 22 Sep): {"key": "strict", "title": "Strict",
        # "policyDescription": "This reservation is non-refundable.<br>"} --
        # no "type"/"policy" field as the doc's shorthand implied. Prefer the
        # plain-language policyDescription for the refundable verdict since
        # it says so explicitly; fall back to the key/title otherwise.
        cancellation_type = (
            cancellation.get("title") or cancellation.get("key") or
            cancellation.get("type") or cancellation.get("policy")
        )
        refundable = None
        description = str(cancellation.get("policyDescription") or "")
        if "non-refundable" in description.lower() or "non refundable" in description.lower():
            refundable = False
        elif "refund" in description.lower():
            refundable = True
        elif cancellation_type:
            low = str(cancellation_type).lower()
            refundable = "non" not in low and "strict" not in low

        return PriceResult(
            site=self.site,
            ref=ref,
            outcome=Outcome.PRICED,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            currency=currency,
            comparable_total=comparable_total(breakdown),
            headline_total=headline,
            breakdown=breakdown,
            line_items=line_items,
            reconciliation_difference=(
                round(comparable_total(breakdown) + (tourist_tax or 0.0) - headline, 2)
                if headline is not None
                else None
            ),
            refundable=refundable,
            cancellation_type=str(cancellation_type) if cancellation_type else None,
            minimum_stay_nights=min_stay,
            source_url=str(resp.url),
        )
