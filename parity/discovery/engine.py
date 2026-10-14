from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from parity.core.models import (
    DiscoveryCandidate,
    DiscoveryReport,
    PropertyRef,
    ProviderDiscoveryResult,
)


class DiscoveryProvider(Protocol):
    name: str

    def discover(
        self, subject: PropertyRef
    ) -> list[PropertyRef] | ProviderDiscoveryResult: ...


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = re.sub(r"/+", "/", parts.path).rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path, "", ""))


def _normalise_title(value: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (value or "").lower()))


def score_candidate(subject: PropertyRef, candidate: PropertyRef) -> tuple[float, list[str]]:
    """Cheap ranking only. The score never verifies identity."""
    score = 0.0
    reasons: list[str] = []

    if candidate.extra.get("verified_mapping") is True:
        score += 100
        reasons.append("fresh previously verified mapping (+100)")

    if candidate.extra.get("historical_verified_candidate") is True:
        score += 8
        reasons.append("previously verified local candidate; recheck required (+8)")
    elif candidate.extra.get("historical_discovery_candidate") is True:
        score += 2
        reasons.append("previously discovered local candidate (+2)")

    if candidate.extra.get("licence_search_hit") is True:
        score += 40
        reasons.append("found by exact licence web search; identity still unproven (+40)")
    elif candidate.extra.get("web_search_hits"):
        score += 5
        reasons.append("found by title web search (+5)")

    card_matches = candidate.extra.get("card_photo_matches")
    if isinstance(card_matches, int) and card_matches > 0:
        points = min(card_matches, 3) * 40
        score += points
        reasons.append(f"{card_matches} search-card photo(s) match the subject (+{points})")

    map_rank = candidate.extra.get("booking_map_rank")
    if isinstance(map_rank, int) and 1 <= map_rank <= 10:
        points = 11 - map_rank
        score += points
        reasons.append(f"Booking distance order #{map_rank} (+{points})")

    subject_title = _normalise_title(subject.title)
    candidate_title = _normalise_title(candidate.title)
    if subject_title and candidate_title:
        similarity = SequenceMatcher(None, subject_title, candidate_title).ratio()
        title_points = round(similarity * 30.0, 1)
        score += title_points
        reasons.append(f"title similarity {similarity:.2f} (+{title_points})")

    if subject.bedrooms is not None and candidate.bedrooms is not None:
        if subject.bedrooms == candidate.bedrooms:
            score += 20
            reasons.append("bedrooms exact (+20)")
        else:
            score -= 25
            reasons.append("bedrooms conflict (-25)")

    if subject.bathrooms is not None and candidate.bathrooms is not None:
        difference = abs(subject.bathrooms - candidate.bathrooms)
        if difference == 0:
            score += 15
            reasons.append("bathrooms exact (+15)")
        elif difference <= 1:
            score += 5
            reasons.append("bathrooms within one (+5)")
        else:
            score -= 15
            reasons.append("bathrooms conflict (-15)")

    if subject.capacity is not None and candidate.capacity is not None:
        if subject.capacity == candidate.capacity:
            score += 15
            reasons.append("capacity exact (+15)")
        else:
            score -= 10
            reasons.append("capacity conflict (-10)")

    if (
        subject.property_type
        and candidate.property_type
        and subject.property_type.casefold() == candidate.property_type.casefold()
    ):
        score += 10
        reasons.append("property type exact (+10)")

    if None not in (subject.lat, subject.lng, candidate.lat, candidate.lng):
        from parity.core.neighbours import _haversine_km

        distance = _haversine_km(subject.lat, subject.lng, candidate.lat, candidate.lng)
        if distance <= 0.1:
            score += 30
            reasons.append(f"location within {distance:.2f}km (+30)")
        elif distance <= 0.5:
            score += 20
            reasons.append(f"location within {distance:.2f}km (+20)")
        elif distance <= 2:
            score += 8
            reasons.append(f"location within {distance:.2f}km (+8)")
        else:
            score -= 20
            reasons.append(f"location {distance:.2f}km away (-20)")

    return round(score, 1), reasons


class DiscoveryEngine:
    def __init__(self, providers: list[DiscoveryProvider], limit_per_site: int = 5):
        self.providers = providers
        self.limit_per_site = limit_per_site

    def discover(self, subject: PropertyRef) -> DiscoveryReport:
        merged: dict[str, DiscoveryCandidate] = {}
        diagnostics: dict[str, str] = {}
        errors: dict[str, str] = {}
        for provider in self.providers:
            try:
                outcome = provider.discover(subject)
            except Exception as exc:
                errors[provider.name] = str(exc)
                continue
            if isinstance(outcome, ProviderDiscoveryResult):
                refs = outcome.refs
                diagnostics[provider.name] = outcome.diagnostic
            else:
                refs = outcome
                diagnostics[provider.name] = f"completed; returned {len(refs)} candidate(s)"
            for ref in refs:
                key = canonicalize_url(ref.url)
                score, reasons = score_candidate(subject, ref)
                existing = merged.get(key)
                if existing is None:
                    merged[key] = DiscoveryCandidate(
                        ref=ref,
                        sources=[provider.name],
                        score=score,
                        reasons=reasons,
                    )
                else:
                    if provider.name not in existing.sources:
                        existing.sources.append(provider.name)
                    if score > existing.score:
                        existing.ref = ref
                        existing.score = score
                        existing.reasons = reasons

        by_site: dict[str, list[DiscoveryCandidate]] = defaultdict(list)
        for candidate in merged.values():
            by_site[candidate.ref.site].append(candidate)

        selected: list[DiscoveryCandidate] = []
        for site in sorted(by_site):
            ranked = sorted(by_site[site], key=lambda item: (-item.score, item.ref.url))
            selected.extend(ranked[: self.limit_per_site])
        selected.sort(key=lambda item: (item.ref.site, -item.score, item.ref.url))
        return DiscoveryReport(
            subject=subject,
            candidates=selected,
            provider_diagnostics=diagnostics,
            provider_errors=errors,
        )
