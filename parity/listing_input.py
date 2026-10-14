"""Strict TravelStaytion listing-input normalisation for UI and CLI entry points."""
from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlparse


TRAVELSTAYTION_STAY_ORIGIN = "https://www.travelstaytion.com/stay"
MAX_LISTING_INPUTS = 10
_NUMERIC_ID_RE = re.compile(r"^[1-9]\d*$")
_STAY_PATH_RE = re.compile(r"^/stay/([1-9]\d*)/?$")


def normalise_travelstaytion_listing(value: object) -> tuple[str, str]:
    """Return ``(listing_id, canonical_url)`` for a bare ID or TS stay URL."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("enter a TravelStaytion listing ID or /stay/<id> URL")
    raw = value.strip()
    if _NUMERIC_ID_RE.fullmatch(raw):
        listing_id = raw
    else:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").casefold()
        match = _STAY_PATH_RE.fullmatch(parsed.path)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or host not in {"travelstaytion.com", "www.travelstaytion.com"}
            or match is None
        ):
            raise ValueError(
                f"{raw!r} is not a TravelStaytion listing ID or /stay/<id> URL"
            )
        listing_id = match.group(1)
    return listing_id, f"{TRAVELSTAYTION_STAY_ORIGIN}/{listing_id}"


def normalise_listing_inputs(
    values: Iterable[object],
    *,
    maximum: int = MAX_LISTING_INPUTS,
) -> list[tuple[str, str]]:
    """Normalise, deduplicate and bound a single or portfolio listing request."""
    normalised: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in values:
        listing = normalise_travelstaytion_listing(value)
        if listing[0] in seen:
            continue
        seen.add(listing[0])
        normalised.append(listing)
    if not normalised:
        raise ValueError("enter at least one TravelStaytion listing ID")
    if len(normalised) > maximum:
        raise ValueError(
            f"received {len(normalised)} TravelStaytion listings; maximum is {maximum}"
        )
    return normalised
