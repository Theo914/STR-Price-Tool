"""Like-for-like neighbour comps, Airbnb only (per Theo's 22 Sep decision:
one site, filtered to the same offering, so the percentage stays realistic).

Method (verified live on Casa Tatano -> median EUR 5,025 across 6 comps):
1. Search Airbnb with min_bedrooms=ours, room_types[]=Entire home/apt,
   amenities[]=7 (pool), a ~3km map box around our lat/lng.
2. Walk every result page via paginationInfo.pageCursors.
3. Filter in code: bedrooms == ours exactly, bathrooms within +/-1,
   distance <= 3km, drop our own listing and same-house duplicates.
4. Median of survivors; our price as a % vs that median + percentile.
5. If fewer than MIN_COMPS survive, relax to bedrooms +/-1 and flag
   low_confidence.

NOT yet live-tested against this exact codebase (no egress to airbnb.* from
this sandbox) — the search-and-filter recipe itself was manually verified
live during research. The embedded JSON key names in `_iter_search_results`
are the part most likely to have drifted; re-inspect a live
data-deferred-state-0 search-page payload before trusting this on a new run.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from statistics import median
from urllib.parse import urlsplit

import httpx

from parity.adapters.http.airbnb import (
    AIRBNB_NAVIGATION_HEADERS,
    AirbnbAdapter,
    _find_key,
)
from parity.core.models import NeighbourComp, NeighbourReport, PropertyRef

MIN_COMPS = 5
MAX_DISTANCE_KM = 3.0
BATHROOM_TOLERANCE = 1.0
POOL_AMENITY_ID = "7"

@dataclass
class AirbnbSearchTrace:
    """Small, non-sensitive trace explaining a public map-search outcome."""

    radius_km: float
    requests: int = 0
    statuses: list[int] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    content_encodings: list[str] = field(default_factory=list)
    content_types: list[str] = field(default_factory=list)
    raw_results: int = 0
    stop_reason: str = "not_started"
    search_origin: str = "https://www.airbnb.com"


_AIRBNB_DOMAIN_SWITCH_RE = re.compile(
    r'<form[^>]+action=["\'](https://[^"\']+/v2/domain_switch/handoff)["\']',
    re.IGNORECASE,
)
_AIRBNB_HOST_RE = re.compile(r"^(?:www\.)?airbnb\.[a-z]{2,3}(?:\.[a-z]{2})?$")


def _airbnb_domain_switch_origin(html: str) -> str | None:
    """Return an Airbnb-owned regional origin from its explicit hand-off form."""
    match = _AIRBNB_DOMAIN_SWITCH_RE.search(html)
    if not match:
        return None
    parts = urlsplit(match.group(1))
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not _AIRBNB_HOST_RE.fullmatch(host):
        return None
    return f"https://{host}"


def _normalise_airbnb_room_id(value: object) -> tuple[str | None, str | None]:
    """Return (numeric room id, original GraphQL id if one was decoded)."""
    if value is None:
        return None, None
    raw = str(value)
    if re.fullmatch(r"\d+", raw):
        return raw, None
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None, None
    match = re.fullmatch(r"DemandStayListing:(\d+)", decoded)
    if not match:
        return None, None
    return match.group(1), raw


def _flatten_display_text(value: object) -> str:
    """Collect visible strings from Airbnb's drifting structured card data."""
    parts: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for key, child in item.items():
                if key in {"body", "text", "value", "label", "title"}:
                    visit(child)
                elif isinstance(child, (dict, list)):
                    visit(child)

    visit(value)
    return " ".join(parts)


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * r * asin(sqrt(a))


def _iter_search_results(
    client: httpx.Client,
    lat: float,
    lng: float,
    bedrooms: int,
    max_pages: int = 6,
    max_distance_km: float = MAX_DISTANCE_KM,
    amenity_ids: list[str] | None = None,
    trace: AirbnbSearchTrace | None = None,
    search_origin: str = "https://www.airbnb.com",
):
    """Yields raw Airbnb search-result dicts across all pages."""
    trace = trace or AirbnbSearchTrace(radius_km=max_distance_km)
    trace.search_origin = search_origin.rstrip("/")
    cursor = None
    delta = max_distance_km / 111.0  # rough km -> degrees at mid-latitudes
    params_base = {
        "min_bedrooms": bedrooms,
        "room_types[]": "Entire home/apt",
        "ne_lat": lat + delta,
        "ne_lng": lng + delta,
        "sw_lat": lat - delta,
        "sw_lng": lng - delta,
        "search_by_map": "true",
    }
    if amenity_ids:
        params_base["amenities[]"] = amenity_ids
    for _ in range(max_pages):
        params = dict(params_base)
        if cursor:
            params["cursor"] = cursor
        def _fetch():
            response = client.get(
                f"{trace.search_origin}/s/homes",
                params=params,
                headers=AIRBNB_NAVIGATION_HEADERS,
            )
            trace.requests += 1
            trace.statuses.append(response.status_code)
            host = response.url.host
            if host not in trace.hosts:
                trace.hosts.append(host)
            content_encoding = response.headers.get("content-encoding", "identity").lower()
            content_type = response.headers.get("content-type", "unknown").lower()
            if content_encoding not in trace.content_encodings:
                trace.content_encodings.append(content_encoding)
            if content_type not in trace.content_types:
                trace.content_types.append(content_type)
            return response

        resp = _fetch()
        content_encoding = resp.headers.get("content-encoding", "identity").lower()
        if content_encoding not in {"identity", "gzip", "deflate"}:
            trace.stop_reason = "unsupported_content_encoding"
            raise RuntimeError(
                "Airbnb map search returned an unsupported content encoding "
                f"({content_encoding}); the response was not parsed as text"
            )
        content_type = resp.headers.get("content-type", "").lower()
        if "html" in content_type and b"<" not in resp.content[:1024]:
            trace.stop_reason = "binary_response"
            raise RuntimeError(
                "Airbnb map search returned a binary-looking body labelled as HTML; "
                f"content-encoding={content_encoding}, bytes={len(resp.content)}"
            )
        regional_origin = _airbnb_domain_switch_origin(resp.text)
        if regional_origin and regional_origin != trace.search_origin:
            trace.search_origin = regional_origin
            resp = _fetch()
            content_encoding = resp.headers.get("content-encoding", "identity").lower()
            if content_encoding not in {"identity", "gzip", "deflate"}:
                trace.stop_reason = "unsupported_content_encoding"
                raise RuntimeError(
                    "Airbnb regional map search returned an unsupported content encoding "
                    f"({content_encoding}); the response was not parsed as text"
                )
            content_type = resp.headers.get("content-type", "").lower()
            if "html" in content_type and b"<" not in resp.content[:1024]:
                trace.stop_reason = "binary_response"
                raise RuntimeError(
                    "Airbnb regional map search returned a binary-looking body labelled as HTML; "
                    f"content-encoding={content_encoding}, bytes={len(resp.content)}"
                )
        if resp.status_code == 429:
            trace.stop_reason = "rate_limited"
            retry_after = resp.headers.get("Retry-After")
            suffix = f"; Retry-After={retry_after}" if retry_after else ""
            raise RuntimeError(f"Airbnb map search returned HTTP 429{suffix}; stop and wait")
        if resp.status_code in (401, 403):
            trace.stop_reason = "access_denied"
            raise RuntimeError(
                f"Airbnb map search returned HTTP {resp.status_code}; access denied, do not retry immediately"
            )
        if resp.status_code >= 400:
            trace.stop_reason = f"http_{resp.status_code}"
            raise RuntimeError(f"Airbnb map search returned HTTP {resp.status_code}")
        state, parse_error = AirbnbAdapter._extract_deferred_state(resp.text)
        if not state:
            trace.stop_reason = "missing_or_invalid_embedded_state"
            diagnosis = AirbnbAdapter._diagnose(resp.text, parse_error)
            raise RuntimeError(f"Airbnb map search could not read embedded page data: {diagnosis}")

        results = _find_key(state, "searchResults")
        if results is None:
            trace.stop_reason = "schema_mismatch"
            raise RuntimeError(
                "Airbnb page data was present but the searchResults field was absent; response schema may have changed"
            )
        if not isinstance(results, list):
            trace.stop_reason = "schema_mismatch"
            raise RuntimeError("Airbnb searchResults field was not a list")
        if not results:
            trace.stop_reason = "no_results"
            return
        for r in results:
            trace.raw_results += 1
            yield r

        cursors = _find_key(state, "pageCursors")
        if not cursors or len(cursors) < 2:
            trace.stop_reason = "last_page"
            return
        next_cursor = cursors[1]
        if not next_cursor or next_cursor == cursor:
            trace.stop_reason = "last_page"
            return
        cursor = next_cursor
    trace.stop_reason = "page_budget_reached"


def _parse_result(raw: dict, require_price: bool = True) -> NeighbourComp | None:
    listing = _find_key(raw, "demandStayListing") or _find_key(raw, "listing") or {}
    if not isinstance(listing, dict):
        listing = {}
    raw_room_id = listing.get("id") or listing.get("roomId") or _find_key(raw, "roomId")
    room_id, graphql_id = _normalise_airbnb_room_id(raw_room_id)
    coord = listing.get("location", {}).get("coordinate", {}) if isinstance(listing.get("location"), dict) else {}
    if not coord:
        coord = _find_key(raw, "coordinate") or {}
    lat, lng = coord.get("latitude"), coord.get("longitude")

    structured_content = _find_key(raw, "structuredContent") or {}
    primary_line = _flatten_display_text(structured_content)
    if not primary_line:
        primary_line = _flatten_display_text(_find_key(raw, "primaryLine") or "")
    bedrooms = bathrooms = None
    if isinstance(primary_line, str):
        bed_m = re.search(r"(\d+)\s*(?:bed|bedroom)", primary_line, re.I)
        bath_m = re.search(r"([\d.]+)\s*bath", primary_line, re.I)
        bedrooms = int(bed_m.group(1)) if bed_m else None
        bathrooms = float(bath_m.group(1)) if bath_m else None

    price = _find_key(raw, "amount") or _find_key(raw, "total")
    if isinstance(price, dict):
        price = price.get("amount")

    if room_id is None or require_price and price is None:
        return None

    title = listing.get("title") or listing.get("name")
    capacity = listing.get("personCapacity") or listing.get("guestCapacity")
    extra = {"graphql_id": graphql_id} if graphql_id else {}
    ref = PropertyRef(
        site="airbnb",
        raw_id=str(room_id),
        url=f"https://www.airbnb.com/rooms/{room_id}",
        lat=lat,
        lng=lng,
        bedrooms=bedrooms,
        bathrooms=bathrooms,
        capacity=int(capacity) if isinstance(capacity, (int, float)) else None,
        title=title if isinstance(title, str) else None,
        extra=extra,
    )
    comparable_total = None
    if price is not None:
        try:
            comparable_total = float(price)
        except (TypeError, ValueError):
            if require_price:
                return None
    return NeighbourComp(ref=ref, comparable_total=comparable_total, bedrooms=bedrooms,
                          bathrooms=bathrooms, distance_km=None)


def find_neighbours(
    client: httpx.Client,
    our_price: float | None,
    our_lat: float,
    our_lng: float,
    our_bedrooms: int,
    exclude_raw_ids: set[str] | None = None,
) -> NeighbourReport:
    exclude_raw_ids = exclude_raw_ids or set()

    def _collect(bedroom_tolerance: int) -> list[NeighbourComp]:
        seen_ids: set[str] = set()
        comps: list[NeighbourComp] = []
        for raw in _iter_search_results(
            client,
            our_lat,
            our_lng,
            max(our_bedrooms - bedroom_tolerance, 0),
            amenity_ids=[POOL_AMENITY_ID],
        ):
            comp = _parse_result(raw)
            if comp is None or comp.ref.raw_id in exclude_raw_ids or comp.ref.raw_id in seen_ids:
                continue
            if comp.bedrooms is None or abs(comp.bedrooms - our_bedrooms) > bedroom_tolerance:
                continue
            if comp.bathrooms is not None:
                pass  # bathroom filter applied below once we trust the parsed value
            if comp.ref.lat is None or comp.ref.lng is None:
                continue
            dist = _haversine_km(our_lat, our_lng, comp.ref.lat, comp.ref.lng)
            if dist > MAX_DISTANCE_KM:
                continue
            comp.distance_km = round(dist, 2)
            seen_ids.add(comp.ref.raw_id)
            comps.append(comp)
        return comps

    comps = _collect(bedroom_tolerance=0)
    low_confidence = False
    filters_used = {"bedrooms": "exact", "bathrooms": f"+/-{BATHROOM_TOLERANCE}",
                     "amenity": "pool", "radius_km": MAX_DISTANCE_KM}

    if len(comps) < MIN_COMPS:
        relaxed = _collect(bedroom_tolerance=1)
        if len(relaxed) > len(comps):
            comps = relaxed
            filters_used["bedrooms"] = "+/-1 (relaxed, exact-match sample too small)"
        low_confidence = True

    prices = [c.comparable_total for c in comps if c.comparable_total is not None]
    med = median(prices) if prices else None

    pct_vs_median = None
    percentile = None
    if med and our_price is not None:
        pct_vs_median = round((our_price - med) / med * 100, 1)
        if prices:
            more_expensive = sum(1 for p in prices if p > our_price)
            percentile = round(more_expensive / len(prices) * 100, 1)

    return NeighbourReport(
        comps=comps,
        median=round(med, 2) if med else None,
        our_price=our_price,
        pct_vs_median=pct_vs_median,
        percentile=percentile,
        low_confidence=low_confidence or len(comps) < MIN_COMPS,
        filters_used=filters_used,
    )
