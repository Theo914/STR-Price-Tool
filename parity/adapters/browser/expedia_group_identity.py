"""Structured identity evidence shared by Vrbo and Expedia.

Phase 7 enables identity before pricing.  Rendered pages are reduced to a
small allow-listed JSON snapshot; raw HTML, scripts, cookies, request headers
and session data are never persisted.  A snapshot is useful only when it has a
real title and either an explicit registration identifier or at least two
public property-gallery images.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

from parity.core.matching import find_licence_numbers
from parity.core.models import PropertyRef


IDENTITY_SNAPSHOT_VERSION = 1
_VRBO_HOST_RE = re.compile(r"^(?:www\.)?vrbo\.com$", re.IGNORECASE)
_EXPEDIA_HOST_RE = re.compile(r"^(?:www\.)?expedia\.[a-z.]+$", re.IGNORECASE)
_VRBO_ID_PATTERNS = (
    re.compile(r"/(\d{5,})ha(?:/|$)", re.IGNORECASE),
    re.compile(r"/(?:p|property/)(\d{5,})(?:/|$)", re.IGNORECASE),
)
_EXPEDIA_ID_RE = re.compile(r"(?:^|[./])h(\d{5,})(?:[./]|$)", re.IGNORECASE)
_PROPERTY_TYPES = {
    "apartment", "house", "lodgingbusiness", "resort", "vacationrental",
    "villa",
}
_IMAGE_HOST_SUFFIXES = (
    "trvl-media.com",
    "mediaim.expedia.com",
    "media.vrbo.com",
)


def canonical_vrbo_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not _VRBO_HOST_RE.fullmatch(host):
        raise ValueError("Vrbo URL must use the public vrbo.com host")
    raw_id = None
    for pattern in _VRBO_ID_PATTERNS:
        match = pattern.search(parts.path)
        if match:
            raw_id = match.group(1)
            break
    if raw_id is None:
        raise ValueError("Vrbo identity requires a recognisable numeric property URL")
    return f"https://www.vrbo.com/{raw_id}ha"


def canonical_expedia_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not _EXPEDIA_HOST_RE.fullmatch(host):
        raise ValueError("Expedia URL must use a public expedia.* host")
    match = _EXPEDIA_ID_RE.search(parts.path)
    if match is None:
        raise ValueError("Expedia identity requires a recognisable h<id> property URL")
    return f"https://www.expedia.com/x.h{match.group(1)}.Hotel-Information"


def raw_id_from_url(platform: str, canonical_url: str) -> str:
    if platform == "vrbo_expedia":
        for pattern in _VRBO_ID_PATTERNS:
            match = pattern.search(urlsplit(canonical_url).path)
            if match:
                return match.group(1)
    elif platform == "expedia":
        match = _EXPEDIA_ID_RE.search(urlsplit(canonical_url).path)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract a {platform} property identifier")


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _one_number(text: str, patterns: tuple[str, ...], *, integer: bool):
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


def _clean_image_url(value: str) -> str | None:
    value = value.replace(r"\u002F", "/").replace(r"\/", "/")
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not any(host == suffix or host.endswith("." + suffix) for suffix in _IMAGE_HOST_SUFFIXES):
        return None
    if not parts.path or parts.path == "/":
        return None
    return urlunsplit(("https", host, parts.path, "", ""))


def _scoped_gallery(platform: str, raw_id: str, values) -> list[str]:
    gallery: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        cleaned = _clean_image_url(str(item))
        if not cleaned or cleaned in seen:
            continue
        # Expedia pages commonly append recommendation-card photos after the
        # actual property gallery.  Its own property images carry the numeric
        # hotel id as a path segment, so retain only that explicit scope.
        if platform == "expedia" and f"/{raw_id}/" not in urlsplit(cleaned).path:
            continue
        seen.add(cleaned)
        gallery.append(cleaned)
    return gallery


def build_identity_snapshot(
    *,
    platform: str,
    canonical_url: str,
    page_title: str,
    headings: list[str],
    body_text: str,
    json_ld_texts: list[str],
    dom_image_values: list[str],
    meta_image_values: list[str],
) -> dict:
    if platform not in {"vrbo_expedia", "expedia"}:
        raise ValueError("unsupported Expedia Group identity platform")
    nodes: list[dict] = []
    for raw in json_ld_texts:
        try:
            nodes.extend(_lodging_nodes(json.loads(raw)))
        except (TypeError, ValueError):
            continue
    node = nodes[0] if nodes else {}

    title = str(node.get("name") or "").strip()
    if not title:
        title = next((str(item).strip() for item in headings if str(item).strip()), "")
    if not title:
        title = re.split(
            r"\s+(?:[|–-]\s*)?(?:Vrbo|Expedia)(?:\.|\s|$)",
            page_title,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

    address = node.get("address") if isinstance(node.get("address"), dict) else {}
    geo = node.get("geo") if isinstance(node.get("geo"), dict) else {}
    country_value = address.get("addressCountry")
    if isinstance(country_value, dict):
        country_value = country_value.get("name") or country_value.get("addressCountry")

    raw_images: list[str] = []
    raw_images.extend(_images_from_value(node.get("image")))
    raw_images.extend(meta_image_values)
    raw_images.extend(dom_image_values)
    raw_id = raw_id_from_url(platform, canonical_url)
    gallery = _scoped_gallery(platform, raw_id, raw_images)

    amenities: list[str] = []
    raw_amenities = node.get("amenityFeature")
    if isinstance(raw_amenities, list):
        for item in raw_amenities:
            if isinstance(item, dict) and item.get("name"):
                amenities.append(str(item["name"]).strip())

    registrations = list(find_licence_numbers(body_text))
    bedrooms = _one_number(
        body_text,
        (
            r"\b(\d+)\s+bedrooms?\b",
            r"\b(\d+)\s+camere? da letto\b",
            r"\b(\d+)\s+habitaciones?\b",
        ),
        integer=True,
    )
    bathrooms = _one_number(
        body_text,
        (
            r"\b(\d+(?:\.\d+)?)\s+bathrooms?\b",
            r"\b(\d+(?:\.\d+)?)\s+bagni?\b",
            r"\b(\d+(?:\.\d+)?)\s+baños?\b",
        ),
        integer=False,
    )
    capacity = _one_number(
        body_text,
        (
            r"\bsleeps?\s+(\d+)\b",
            r"\b(?:max(?:imum)?\s+)?(?:guests?|adults?)\D{0,8}(\d+)\b",
            r"\b(\d+)\s+(?:ospiti|personas?)\b",
        ),
        integer=True,
    )

    raw_type = node.get("@type")
    if isinstance(raw_type, list):
        raw_type = next(
            (item for item in raw_type if str(item).casefold() in _PROPERTY_TYPES),
            None,
        )

    return {
        "snapshot_version": IDENTITY_SNAPSHOT_VERSION,
        "platform": platform,
        "canonical_url": canonical_url,
        "raw_id": raw_id,
        "title": title or None,
        "registration_ids": registrations,
        "lat": _as_float(geo.get("latitude")),
        "lng": _as_float(geo.get("longitude")),
        "country": str(country_value).strip() if country_value else None,
        "city": str(address.get("addressLocality") or "").strip() or None,
        "postcode": str(address.get("postalCode") or "").strip() or None,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "capacity": capacity,
        "property_type": str(raw_type) if raw_type else None,
        "amenities": list(dict.fromkeys(item for item in amenities if item)),
        "gallery": gallery[:80],
        "identity_source": "rendered_public_expedia_group_page",
    }


def property_from_snapshot(snapshot: dict, *, platform: str) -> PropertyRef:
    if snapshot.get("snapshot_version") != IDENTITY_SNAPSHOT_VERSION:
        raise ValueError(f"{platform} identity snapshot version is unsupported")
    if snapshot.get("platform") != platform:
        raise ValueError(f"{platform} identity snapshot platform does not match")
    title = str(snapshot.get("title") or "").strip()
    registrations = [str(item) for item in snapshot.get("registration_ids") or [] if item]
    canonical_url = str(snapshot.get("canonical_url") or "")
    raw_id = str(snapshot.get("raw_id") or raw_id_from_url(platform, canonical_url))
    gallery = _scoped_gallery(platform, raw_id, snapshot.get("gallery") or [])
    if not title:
        raise ValueError(f"{platform} identity rejected: property title is missing")
    if not registrations and len(gallery) < 2:
        raise ValueError(
            f"{platform} identity rejected: no registration identifier and fewer than "
            "two property-gallery images were extracted"
        )
    expected_url = (
        canonical_vrbo_url(canonical_url)
        if platform == "vrbo_expedia"
        else canonical_expedia_url(canonical_url)
    )
    if canonical_url != expected_url:
        raise ValueError(f"{platform} identity snapshot URL is not canonical")
    return PropertyRef(
        site=platform,
        raw_id=raw_id,
        url=canonical_url,
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
        },
    )
