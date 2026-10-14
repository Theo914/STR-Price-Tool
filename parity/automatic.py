"""Pure helpers for the bounded ID-first automatic workflow."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from parity.core.models import (
    DiscoveryCandidate,
    DiscoveryReport,
    IdentityDecision,
    MatchResult,
    PropertyRef,
)
from parity.discovery.engine import canonicalize_url


_CURRENCY_BY_COUNTRY = {
    "au": "AUD",
    "australia": "AUD",
    "gb": "GBP",
    "uk": "GBP",
    "united kingdom": "GBP",
    "england": "GBP",
    "scotland": "GBP",
    "wales": "GBP",
    "northern ireland": "GBP",
    "us": "USD",
    "usa": "USD",
    "united states": "USD",
    "united states of america": "USD",
    "ca": "CAD",
    "canada": "CAD",
    "nz": "NZD",
    "new zealand": "NZD",
    "ch": "CHF",
    "switzerland": "CHF",
}


def infer_listing_currency(subject: PropertyRef, override: str | None = None) -> str:
    """Infer a same-currency quote request, with an explicit override winning."""
    if override and override.casefold() != "auto":
        value = override.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("currency override must be AUTO or a three-letter code")
        return value
    country = " ".join(str(subject.country or "").casefold().split())
    if country in _CURRENCY_BY_COUNTRY:
        return _CURRENCY_BY_COUNTRY[country]
    # TravelStaytion's largest supported European markets use EUR.  The quote
    # endpoint still fails closed if the live response reports another code.
    return "EUR"


def best_candidate_per_channel(
    report: DiscoveryReport,
    channels: tuple[str, ...] = ("airbnb", "booking"),
) -> dict[str, DiscoveryCandidate]:
    """Choose the highest-ranked candidate per channel without verifying it."""
    selected: dict[str, DiscoveryCandidate] = {}
    for candidate in report.candidates:
        site = candidate.ref.site
        if site not in channels or site in selected:
            continue
        selected[site] = candidate
    return selected


def ranked_candidates_per_channel(
    report: DiscoveryReport,
    channels: tuple[str, ...] = ("airbnb", "booking"),
) -> dict[str, list[DiscoveryCandidate]]:
    """Keep the report's score order while grouping bounded candidates."""
    grouped = {channel: [] for channel in channels}
    for candidate in report.candidates:
        if candidate.ref.site in grouped:
            grouped[candidate.ref.site].append(candidate)
    return grouped


def first_verified_candidate(
    candidates: list[DiscoveryCandidate],
    matches_by_url: dict[str, MatchResult],
) -> tuple[DiscoveryCandidate, MatchResult | None] | None:
    """Return the first proven candidate, never a merely likely one."""
    for candidate in candidates:
        match = matches_by_url.get(canonicalize_url(candidate.ref.url))
        if candidate.ref.extra.get("verified_mapping") is True:
            return candidate, match
        if match is not None and match.decision == IdentityDecision.VERIFIED:
            return candidate, match
    return None


def best_review_candidate(
    candidates: list[DiscoveryCandidate],
    matches_by_url: dict[str, MatchResult],
) -> tuple[DiscoveryCandidate, MatchResult | None] | None:
    """Prefer a candidate with partial identity evidence for manual review."""
    if not candidates:
        return None
    decision_priority = {
        IdentityDecision.PROBABLE_REVIEW_REQUIRED: 0,
        IdentityDecision.INSUFFICIENT_EVIDENCE: 1,
        IdentityDecision.REJECTED: 2,
    }
    ranked = []
    for position, candidate in enumerate(candidates):
        match = matches_by_url.get(canonicalize_url(candidate.ref.url))
        priority = decision_priority.get(match.decision, 3) if match else 3
        ranked.append((priority, position, candidate, match))
    _, _, candidate, match = min(ranked, key=lambda item: (item[0], item[1]))
    return candidate, match


def evidence_root_for(work_dir: Path) -> Path:
    """Return the nearest .parity evidence root for an automatic work folder."""
    resolved = work_dir.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name == ".parity":
            return candidate
    return resolved.parent


def _candidate_raw_id(site: str, url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    if site == "airbnb" and "/rooms/" in path:
        return path.rsplit("/rooms/", 1)[-1].split("/", 1)[0]
    if site == "booking" and path.endswith(".html"):
        return path.rsplit("/", 1)[-1][:-5]
    return path.rsplit("/", 1)[-1] or url


def load_historical_candidate_refs(
    root: Path,
    subject: PropertyRef,
    *,
    exclude: set[Path] | None = None,
    maximum_databases: int = 300,
) -> list[PropertyRef]:
    """Read prior local evidence as candidates, never as automatic proof.

    A previously verified mapping receives ranking priority, but it must pass
    the current identity gate again before the automatic workflow can price it.
    """
    excluded = {path.resolve() for path in (exclude or set())}
    selected: dict[str, tuple[int, PropertyRef]] = {}
    database_paths = sorted(root.rglob("*.db"))[:maximum_databases]
    now = datetime.now(timezone.utc).isoformat()
    for path in database_paths:
        if path.resolve() in excluded or not path.is_file():
            continue
        try:
            connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "verified_mappings" in tables:
                rows = connection.execute(
                    """
                    SELECT candidate_site, candidate_url, candidate_raw_id
                    FROM verified_mappings
                    WHERE subject_site=? AND subject_id=? AND expires_at>?
                      AND candidate_site IN ('airbnb', 'booking')
                    ORDER BY verified_at DESC
                    """,
                    (subject.site, subject.raw_id, now),
                ).fetchall()
                for row in rows:
                    ref = PropertyRef(
                        site=row["candidate_site"],
                        raw_id=row["candidate_raw_id"],
                        url=row["candidate_url"],
                        extra={"historical_verified_candidate": True},
                    )
                    selected[canonicalize_url(ref.url)] = (2, ref)
            if "discovery_candidates" in tables:
                rows = connection.execute(
                    """
                    SELECT candidate_site, candidate_url
                    FROM discovery_candidates
                    WHERE subject_site=? AND subject_id=?
                      AND candidate_site IN ('airbnb', 'booking')
                    ORDER BY discovered_at DESC, score DESC
                    LIMIT 40
                    """,
                    (subject.site, subject.raw_id),
                ).fetchall()
                for row in rows:
                    key = canonicalize_url(row["candidate_url"])
                    if key in selected and selected[key][0] >= 1:
                        continue
                    ref = PropertyRef(
                        site=row["candidate_site"],
                        raw_id=_candidate_raw_id(
                            row["candidate_site"], row["candidate_url"]
                        ),
                        url=row["candidate_url"],
                        extra={"historical_discovery_candidate": True},
                    )
                    selected[key] = (1, ref)
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    return [item[1] for item in selected.values()]
