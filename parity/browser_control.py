"""Safe, structured fixtures for browser-rendered public evidence.

Unlike HTTP fixtures, browser fixtures do not retain a raw rendered page.  The
adapter first extracts the small set of public fields required for identity or
price evidence, then this store persists only that JSON snapshot. Cookies,
request headers, session identifiers, scripts and arbitrary page HTML never
enter the fixture.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from parity.http_control import (
    DEFAULT_FIXTURE_TTL_DAYS,
    FixtureCorruptError,
    FixtureMissingError,
    FixtureStaleError,
)


BROWSER_FIXTURE_VERSION = 1


def _snapshot_key(platform: str, kind: str, request_url: str) -> str:
    material = f"{platform}\n{kind}\n{request_url}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class BrowserSnapshotStore:
    """Checksummed, expiring structured browser snapshots."""

    def __init__(self, root: str | Path, ttl_days: int = DEFAULT_FIXTURE_TTL_DAYS):
        self.root = Path(root)
        self.ttl_days = ttl_days

    def _paths(self, platform: str, kind: str, request_url: str) -> tuple[Path, Path]:
        key = _snapshot_key(platform, kind, request_url)
        return self.root / f"{key}.browser.json", self.root / f"{key}.browser.body"

    def save(self, platform: str, kind: str, request_url: str, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        manifest_path, body_path = self._paths(platform, kind, request_url)
        body = (
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        captured_at = datetime.now(timezone.utc)
        manifest = {
            "fixture_version": BROWSER_FIXTURE_VERSION,
            "platform": platform,
            "kind": kind,
            "request_url": request_url,
            "captured_at": captured_at.isoformat(),
            "expires_at": (captured_at + timedelta(days=self.ttl_days)).isoformat(),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_bytes": len(body),
            "privacy": (
                "structured public evidence only; no HTML, scripts, cookies, request "
                "headers or session identifiers retained"
            ),
        }
        body_path.write_bytes(body)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def load(self, platform: str, kind: str, request_url: str) -> dict:
        manifest_path, body_path = self._paths(platform, kind, request_url)
        if not manifest_path.is_file() or not body_path.is_file():
            raise FixtureMissingError(
                f"OFFLINE: no saved browser snapshot for {platform} {kind} {request_url}"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise FixtureCorruptError(
                f"OFFLINE: browser fixture manifest is unreadable: {error}"
            ) from error
        if manifest.get("fixture_version") != BROWSER_FIXTURE_VERSION:
            raise FixtureCorruptError("OFFLINE: browser fixture version is unsupported")
        if (
            manifest.get("platform") != platform
            or manifest.get("kind") != kind
            or manifest.get("request_url") != request_url
        ):
            raise FixtureCorruptError("OFFLINE: browser fixture request identity does not match")
        try:
            expires_at = datetime.fromisoformat(str(manifest["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError) as error:
            raise FixtureCorruptError("OFFLINE: browser fixture expiry is invalid") from error
        if expires_at <= datetime.now(timezone.utc):
            raise FixtureStaleError(
                f"OFFLINE: browser snapshot expired at {expires_at.astimezone(timezone.utc).isoformat()}"
            )
        try:
            body = body_path.read_bytes()
        except OSError as error:
            raise FixtureCorruptError(
                f"OFFLINE: browser fixture body is unreadable: {error}"
            ) from error
        if len(body) != manifest.get("body_bytes"):
            raise FixtureCorruptError("OFFLINE: browser fixture byte count does not match")
        if hashlib.sha256(body).hexdigest() != manifest.get("body_sha256"):
            raise FixtureCorruptError("OFFLINE: browser fixture checksum does not match")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise FixtureCorruptError(
                f"OFFLINE: browser fixture body is not valid UTF-8 JSON: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise FixtureCorruptError("OFFLINE: browser fixture body is not an object")
        return payload
