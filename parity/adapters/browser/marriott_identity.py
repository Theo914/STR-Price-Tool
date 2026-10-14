"""Structured Homes & Villas by Marriott Bonvoy identity evidence."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from parity.core.matching import find_licence_numbers
from parity.core.models import PropertyRef


MARRIOTT_IDENTITY_SNAPSHOT_VERSION = 1
_MARRIOTT_HOST = "homes-and-villas.marriott.com"
_PROPERTY_ID_RE = re.compile(r"/(?:[a-z]{2}/)?properties/(\d+)(?:[-/]|$)", re.I)
_PROPERTY_TYPES = {
    "apartment", "house", "lodgingbusiness", "resort", "vacationrental", "villa",
}
_IMAGE_HOST_SUFFIXES = ("marriott.com", "marriottassets.com", "scene7.com")


def canonical_marriott_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != _MARRIOTT_HOST:
        raise ValueError("Marriott identity URL must use homes-and-villas.marriott.com")
    match = _PROPERTY_ID_RE.search(parts.path)
    if match is None:
        raise ValueError("Marriott identity requires a recognisable numeric property URL")
    return f"https://{_MARRIOTT_HOST}/en/properties/{match.group(1)}"


def raw_id_from_marriott_url(url: str) -> str:
    match = _PROPERTY_ID_RE.search(urlsplit(url).path)
    if match is None:
        raise ValueError("Could not extract a Marriott property identifier")
    return match.group(1)


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _one_number(text: str, patterns: tuple[str, ...], *, integer: bool):
    values: set[float] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
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
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [image for item in value for image in _images_from_value(item)]
    if isinstance(value, dict):
        return [
            image
            for key in ("url", "contentUrl", "thumbnailUrl")
            for image in _images_from_value(value.get(key))
        ]
    return []


def _clean_image_url(value: str) -> str | None:
    try:
        parts = urlsplit(value.replace(r"\u002F", "/").replace(r"\/", "/"))
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host == _MARRIOTT_HOST:
        return None
    if not any(host == suffix or host.endswith("." + suffix) for suffix in _IMAGE_HOST_SUFFIXES):
        return None
    if not parts.path or parts.path == "/":
        return None
    return f"https://{host}{parts.path}"


def build_marriott_identity_snapshot(
    *,
    canonical_url: str,
    page_title: str,
    headings: list[str],
    body_text: str,
    json_ld_texts: list[str],
    dom_image_values: list[str],
    meta_image_values: list[str],
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
        title = next((str(item).strip() for item in headings if str(item).strip()), "")
    if not title:
        title = re.split(r"\s+[|–-]\s+", page_title, maxsplit=1)[0].strip()

    address = node.get("address") if isinstance(node.get("address"), dict) else {}
    geo = node.get("geo") if isinstance(node.get("geo"), dict) else {}
    country = address.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name") or country.get("addressCountry")

    raw_images = _images_from_value(node.get("image")) + meta_image_values + dom_image_values
    gallery: list[str] = []
    seen: set[str] = set()
    for raw in raw_images:
        cleaned = _clean_image_url(str(raw))
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            gallery.append(cleaned)

    raw_type = node.get("@type")
    if isinstance(raw_type, list):
        raw_type = next(
            (item for item in raw_type if str(item).casefold() in _PROPERTY_TYPES),
            None,
        )
    registrations = list(find_licence_numbers(body_text))
    return {
        "snapshot_version": MARRIOTT_IDENTITY_SNAPSHOT_VERSION,
        "platform": "marriott",
        "canonical_url": canonical_url,
        "raw_id": raw_id_from_marriott_url(canonical_url),
        "title": title or None,
        "registration_ids": registrations,
        "lat": _as_float(geo.get("latitude")),
        "lng": _as_float(geo.get("longitude")),
        "country": str(country).strip() if country else None,
        "city": str(address.get("addressLocality") or "").strip() or None,
        "postcode": str(address.get("postalCode") or "").strip() or None,
        "bedrooms": _one_number(body_text, (r"\b(\d+)\s+bedrooms?\b",), integer=True),
        "bathrooms": _one_number(
            body_text, (r"\b(\d+(?:\.\d+)?)\s+bathrooms?\b",), integer=False
        ),
        "capacity": _one_number(
            body_text, (r"\bsleeps?\s+(\d+)\b", r"\b(\d+)\s+guests?\b"), integer=True
        ),
        "property_type": str(raw_type) if raw_type else None,
        "gallery": gallery[:80],
        "identity_source": "rendered_public_marriott_property_page",
    }


def property_from_marriott_snapshot(snapshot: dict) -> PropertyRef:
    if snapshot.get("snapshot_version") != MARRIOTT_IDENTITY_SNAPSHOT_VERSION:
        raise ValueError("Marriott identity snapshot version is unsupported")
    if snapshot.get("platform") != "marriott":
        raise ValueError("Marriott identity snapshot platform does not match")
    canonical_url = str(snapshot.get("canonical_url") or "")
    if canonical_url != canonical_marriott_url(canonical_url):
        raise ValueError("Marriott identity snapshot URL is not canonical")
    title = str(snapshot.get("title") or "").strip()
    registrations = [str(item) for item in snapshot.get("registration_ids") or [] if item]
    gallery = [
        cleaned
        for item in snapshot.get("gallery") or []
        if (cleaned := _clean_image_url(str(item)))
    ]
    gallery = list(dict.fromkeys(gallery))
    if not title:
        raise ValueError("Marriott identity rejected: property title is missing")
    if not registrations and len(gallery) < 2:
        raise ValueError(
            "Marriott identity rejected: no registration identifier and fewer than "
            "two property-gallery images were extracted"
        )
    return PropertyRef(
        site="marriott",
        raw_id=str(snapshot.get("raw_id") or raw_id_from_marriott_url(canonical_url)),
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
        title=title,
        gallery=gallery,
        extra={
            "identity_source": snapshot.get("identity_source"),
            "snapshot_version": snapshot.get("snapshot_version"),
        },
    )
