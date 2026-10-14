"""Small local evidence store for discovery and verified mappings.

SQLite keeps the CLI self-contained. It stores stable identity work and
request/circuit telemetry; a price check still performs a fresh quote for the
requested stay.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from parity.core.matching import PHash
from parity.core.models import DiscoveryCandidate, IdentityDecision, MatchResult, PropertyRef


@dataclass(frozen=True)
class CircuitStatus:
    domain: str
    reason: str
    open_until: datetime

    def remaining_seconds(self, now: datetime | None = None) -> float:
        return max(
            0.0,
            (self.open_until - (now or datetime.now(timezone.utc))).total_seconds(),
        )


class EvidenceStore:
    def __init__(self, path: str | Path = ".parity/parity.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _create_schema(self) -> None:
        self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS discovery_candidates (
                subject_site TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                candidate_site TEXT NOT NULL,
                candidate_url TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                score REAL NOT NULL,
                reasons_json TEXT NOT NULL,
                discovered_at TEXT NOT NULL,
                PRIMARY KEY (subject_site, subject_id, candidate_url)
            );
            CREATE TABLE IF NOT EXISTS verified_mappings (
                subject_site TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                candidate_site TEXT NOT NULL,
                candidate_url TEXT NOT NULL,
                candidate_raw_id TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (subject_site, subject_id, candidate_url)
            );
            CREATE TABLE IF NOT EXISTS request_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                status_code INTEGER,
                outcome TEXT NOT NULL,
                latency_ms REAL,
                retry_after_seconds REAL,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS circuit_state (
                domain TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                open_until TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS photo_hashes (
                image_url TEXT PRIMARY KEY,
                full_hash_hex TEXT NOT NULL,
                crop_hash_hex TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
        self._db.commit()

    def save_candidates(self, subject: PropertyRef, candidates: list[DiscoveryCandidate]) -> None:
        rows = [
            (
                subject.site,
                subject.raw_id,
                candidate.ref.site,
                candidate.ref.url,
                json.dumps(candidate.sources, ensure_ascii=False),
                candidate.score,
                json.dumps(candidate.reasons, ensure_ascii=False),
                candidate.discovered_at.isoformat(),
            )
            for candidate in candidates
        ]
        self._db.executemany(
            """
            INSERT INTO discovery_candidates (
                subject_site, subject_id, candidate_site, candidate_url,
                sources_json, score, reasons_json, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_site, subject_id, candidate_url) DO UPDATE SET
                sources_json=excluded.sources_json,
                score=excluded.score,
                reasons_json=excluded.reasons_json,
                discovered_at=excluded.discovered_at
            """,
            rows,
        )
        self._db.commit()

    def save_verified_mapping(
        self,
        subject: PropertyRef,
        match: MatchResult,
        ttl_days: int = 30,
    ) -> None:
        if match.decision != IdentityDecision.VERIFIED:
            raise ValueError("only VERIFIED identity decisions may be persisted as mappings")
        now = datetime.now(timezone.utc)
        evidence = [
            {
                "kind": item.kind.value,
                "detail": item.detail,
                "source_url": item.source_url,
                "observed_at": item.observed_at.isoformat(),
            }
            for item in match.evidence
        ]
        self._db.execute(
            """
            INSERT INTO verified_mappings (
                subject_site, subject_id, candidate_site, candidate_url,
                candidate_raw_id, evidence_json, verified_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_site, subject_id, candidate_url) DO UPDATE SET
                candidate_site=excluded.candidate_site,
                candidate_raw_id=excluded.candidate_raw_id,
                evidence_json=excluded.evidence_json,
                verified_at=excluded.verified_at,
                expires_at=excluded.expires_at
            """,
            (
                subject.site,
                subject.raw_id,
                match.ref.site,
                match.ref.url,
                match.ref.raw_id,
                json.dumps(evidence, ensure_ascii=False),
                now.isoformat(),
                (now + timedelta(days=ttl_days)).isoformat(),
            ),
        )
        self._db.commit()

    def load_verified_refs(self, subject: PropertyRef) -> list[PropertyRef]:
        now = datetime.now(timezone.utc).isoformat()
        rows = self._db.execute(
            """
            SELECT candidate_site, candidate_url, candidate_raw_id
            FROM verified_mappings
            WHERE subject_site=? AND subject_id=? AND expires_at>?
            ORDER BY candidate_site, candidate_url
            """,
            (subject.site, subject.raw_id, now),
        ).fetchall()
        return [
            PropertyRef(
                site=row["candidate_site"],
                raw_id=row["candidate_raw_id"],
                url=row["candidate_url"],
            )
            for row in rows
        ]

    def save_photo_hash(self, image_url: str, image_hash: PHash, ttl_days: int = 30) -> None:
        """Persist one image fingerprint without storing the image itself."""
        now = datetime.now(timezone.utc)
        self._db.execute(
            """
            INSERT INTO photo_hashes (
                image_url, full_hash_hex, crop_hash_hex, observed_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(image_url) DO UPDATE SET
                full_hash_hex=excluded.full_hash_hex,
                crop_hash_hex=excluded.crop_hash_hex,
                observed_at=excluded.observed_at,
                expires_at=excluded.expires_at
            """,
            (
                image_url,
                f"{image_hash.full:016x}",
                f"{image_hash.crop:016x}",
                now.isoformat(),
                (now + timedelta(days=ttl_days)).isoformat(),
            ),
        )
        self._db.commit()

    def load_photo_hash(self, image_url: str) -> PHash | None:
        """Return a non-expired cached fingerprint for an exact image URL."""
        row = self._db.execute(
            """
            SELECT full_hash_hex, crop_hash_hex, expires_at
            FROM photo_hashes
            WHERE image_url=?
            """,
            (image_url,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= datetime.now(timezone.utc).isoformat():
            self._db.execute("DELETE FROM photo_hashes WHERE image_url=?", (image_url,))
            self._db.commit()
            return None
        return PHash(
            full=int(row["full_hash_hex"], 16),
            crop=int(row["crop_hash_hex"], 16),
        )

    def record_request(
        self,
        domain: str,
        outcome: str,
        status_code: int | None = None,
        latency_ms: float | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO request_events (
                domain, status_code, outcome, latency_ms,
                retry_after_seconds, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                domain,
                status_code,
                outcome,
                latency_ms,
                retry_after_seconds,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._db.commit()

    def open_circuit(self, domain: str, reason: str, seconds: float) -> None:
        now = datetime.now(timezone.utc)
        self._db.execute(
            """
            INSERT INTO circuit_state (domain, reason, open_until, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET
                reason=excluded.reason,
                open_until=excluded.open_until,
                updated_at=excluded.updated_at
            """,
            (domain, reason, (now + timedelta(seconds=seconds)).isoformat(), now.isoformat()),
        )
        self._db.commit()

    def circuit_reason(self, domain: str) -> str | None:
        status = self.circuit_status(domain)
        return status.reason if status is not None else None

    def circuit_status(self, domain: str) -> CircuitStatus | None:
        row = self._db.execute(
            "SELECT reason, open_until FROM circuit_state WHERE domain=?",
            (domain,),
        ).fetchone()
        if row is None:
            return None
        if row["open_until"] <= datetime.now(timezone.utc).isoformat():
            self._db.execute("DELETE FROM circuit_state WHERE domain=?", (domain,))
            self._db.commit()
            return None
        return CircuitStatus(
            domain=domain,
            reason=str(row["reason"]),
            open_until=datetime.fromisoformat(row["open_until"]),
        )
