"""Offline HTTP fixtures and conservative persistent request governance.

The wrappers in this module deliberately sit around the one shared ``httpx``
client created by the CLI.  Adapters therefore cannot accidentally bypass
offline mode merely because a new endpoint was added later.

Fixtures are inert response bytes plus a small JSON manifest.  They are never
opened in a browser and response headers are allow-listed so cookies and other
session material are not persisted.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from parity.storage import CircuitStatus, EvidenceStore


FIXTURE_VERSION = 1
DEFAULT_FIXTURE_TTL_DAYS = 30
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 6 * 60 * 60
DEFAULT_ACCESS_DENIED_COOLDOWN_SECONDS = 24 * 60 * 60

TRANSIENT_RETRY_DELAY_SECONDS = 2.0
_TRANSIENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)

DEFAULT_REQUEST_BUDGETS: dict[tuple[str, str], int] = {
    ("airbnb", "search"): 2,
    ("airbnb", "page"): 3,
    ("airbnb", "pricing"): 1,
    ("airbnb", "image"): 40,
    ("booking", "page"): 1,
    ("booking", "search"): 1,
    ("booking", "pricing"): 1,
    ("booking", "image"): 40,
    ("travelstaytion", "pricing"): 8,
    ("travelstaytion", "page"): 1,
    ("travelstaytion", "image"): 40,
    ("vrbo_expedia", "page"): 1,
    ("vrbo_expedia", "pricing"): 1,
    ("expedia", "page"): 1,
    ("expedia", "pricing"): 1,
    ("expedia_group", "image"): 40,
    ("marriott", "page"): 1,
    ("marriott", "image"): 40,
    # Licence-first reverse web search (parity/discovery/web_search.py).
    ("bing", "search"): 8,
    ("duckduckgo", "search"): 8,
    ("google", "search"): 8,
}

_AIRBNB_HOST_RE = re.compile(r"^(?:www\.)?airbnb\.[a-z]{2,3}(?:\.[a-z]{2})?$")
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "cookie",
    "csrf",
    "password",
    "secret",
    "session",
    "token",
}
_SAFE_RESPONSE_HEADERS = {
    "content-language",
    "content-type",
    "etag",
    "last-modified",
    "location",
    "retry-after",
}
_CHALLENGE_MARKERS = (
    "verify you are a human",
    "verify you're human",
    "unusual traffic",
    "press & hold",
    "bot or not",
)
_SENSITIVE_META_RE = re.compile(
    rb'(<meta\b(?=[^>]*(?:csrf|auth|token))[^>]*\bcontent=["\'])([^"\']*)(["\'])',
    re.IGNORECASE,
)
_SENSITIVE_JSON_RE = re.compile(
    rb'(["\'](?:csrf[_-]?token|access[_-]?token|auth[_-]?token|client[_-]?secret)'
    rb'["\']\s*:\s*["\'])([^"\']+)(["\'])',
    re.IGNORECASE,
)


class OfflineFixtureError(RuntimeError):
    """Offline mode could not safely replay the requested response."""


class FixtureMissingError(OfflineFixtureError):
    pass


class FixtureStaleError(OfflineFixtureError):
    pass


class FixtureCorruptError(OfflineFixtureError):
    pass


class RequestDeferred(RuntimeError):
    """A persistent circuit or per-run budget prevented a live request."""


@dataclass(frozen=True)
class RequestClass:
    platform: str
    kind: str

    @property
    def telemetry_key(self) -> str:
        return f"{self.platform}:{self.kind}"


def classify_request(url: httpx.URL | str) -> RequestClass:
    parts = urlsplit(str(url))
    host = (parts.hostname or "unknown").lower()
    path = parts.path.lower()

    if _AIRBNB_HOST_RE.fullmatch(host):
        kind = "search" if path.startswith("/s/") else "page"
        return RequestClass("airbnb", kind)
    if host == "a0.muscache.com" or host.endswith(".muscache.com"):
        return RequestClass("airbnb", "image")
    if host == "bstatic.com" or host.endswith(".bstatic.com"):
        return RequestClass("booking", "image")
    if (
        host == "trvl-media.com"
        or host.endswith(".trvl-media.com")
        or host == "mediaim.expedia.com"
        or host == "media.vrbo.com"
    ):
        return RequestClass("expedia_group", "image")
    if (
        host == "cache.marriott.com"
        or host.endswith(".marriottassets.com")
        or host == "scene7.com"
        or host.endswith(".scene7.com")
    ):
        return RequestClass("marriott", "image")
    if host == "www.travelstaytion.com" or host.endswith(".travelstaytion.com"):
        if path.endswith((".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp")):
            kind = "image"
        else:
            kind = "pricing" if "/calendar/quote/" in path else "page"
        return RequestClass("travelstaytion", kind)
    image_suffixes = (".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp")
    return RequestClass(host, "image" if path.endswith(image_suffixes) else "page")


def _redacted_url(url: httpx.URL | str) -> str:
    parts = urlsplit(str(url))
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def _request_key(request: httpx.Request) -> str:
    body_digest = hashlib.sha256(request.content or b"").hexdigest()
    material = f"{request.method.upper()}\n{request.url}\n{body_digest}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _sanitise_response_body(body: bytes, content_type: str) -> tuple[bytes, str]:
    textual = (
        "html" in content_type
        or "json" in content_type
        or content_type.startswith("text/")
        or body.lstrip().startswith((b"<", b"{", b"["))
    )
    if not textual:
        return body, "binary response retained; response headers allow-listed"
    sanitised = _SENSITIVE_META_RE.sub(rb"\1[REDACTED]\3", body)
    sanitised = _SENSITIVE_JSON_RE.sub(rb"\1[REDACTED]\3", sanitised)
    return (
        sanitised,
        "common token fields redacted; response headers allow-listed; "
        "cookies and request headers omitted",
    )


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - (now or datetime.now(timezone.utc))).total_seconds()
    return max(0.0, seconds)


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


class HttpFixtureStore:
    """File-backed inert HTTP response fixtures keyed by exact requests."""

    def __init__(self, root: str | Path, ttl_days: int = DEFAULT_FIXTURE_TTL_DAYS):
        self.root = Path(root)
        self.ttl_days = ttl_days

    def _paths(self, request: httpx.Request) -> tuple[Path, Path]:
        key = _request_key(request)
        return self.root / f"{key}.json", self.root / f"{key}.body"

    def save(self, request: httpx.Request, response: httpx.Response) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        manifest_path, body_path = self._paths(request)
        body, sanitisation = _sanitise_response_body(
            response.content,
            response.headers.get("content-type", "").lower(),
        )
        captured_at = datetime.now(timezone.utc)
        manifest = {
            "fixture_version": FIXTURE_VERSION,
            "request_key": _request_key(request),
            "request_method": request.method.upper(),
            "request_url": _redacted_url(request.url),
            "response_url": _redacted_url(response.url),
            "status_code": response.status_code,
            "headers": {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in _SAFE_RESPONSE_HEADERS
            },
            "captured_at": captured_at.isoformat(),
            "expires_at": (captured_at + timedelta(days=self.ttl_days)).isoformat(),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_bytes": len(body),
            "sanitisation": (
                sanitisation
                + "; content-encoding omitted because stored bytes are already decoded"
            ),
        }
        body_path.write_bytes(body)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def load(self, request: httpx.Request) -> httpx.Response:
        manifest_path, body_path = self._paths(request)
        if not manifest_path.is_file() or not body_path.is_file():
            raise FixtureMissingError(
                "OFFLINE: no saved response for "
                f"{request.method.upper()} {_redacted_url(request.url)}"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise FixtureCorruptError(f"OFFLINE: fixture manifest is unreadable: {error}") from error
        if manifest.get("fixture_version") != FIXTURE_VERSION:
            raise FixtureCorruptError("OFFLINE: fixture version is unsupported")
        if manifest.get("request_key") != _request_key(request):
            raise FixtureCorruptError("OFFLINE: fixture request checksum does not match")
        try:
            expires_at = _parse_timestamp(str(manifest["expires_at"]))
        except (KeyError, TypeError, ValueError) as error:
            raise FixtureCorruptError("OFFLINE: fixture expiry is invalid") from error
        if expires_at <= datetime.now(timezone.utc):
            raise FixtureStaleError(
                "OFFLINE: saved response expired at "
                f"{expires_at.astimezone(timezone.utc).isoformat()}"
            )
        try:
            body = body_path.read_bytes()
        except OSError as error:
            raise FixtureCorruptError(f"OFFLINE: fixture body is unreadable: {error}") from error
        if hashlib.sha256(body).hexdigest() != manifest.get("body_sha256"):
            raise FixtureCorruptError("OFFLINE: fixture body checksum does not match")

        response_url = str(manifest.get("response_url") or request.url)
        replay_request = httpx.Request(request.method, response_url, content=request.content)
        return httpx.Response(
            int(manifest["status_code"]),
            headers=dict(manifest.get("headers") or {}),
            content=body,
            request=replay_request,
        )


class RequestGovernor:
    """Per-run budgets plus persistent platform circuits backed by SQLite."""

    def __init__(
        self,
        store: EvidenceStore,
        budgets: dict[tuple[str, str], int] | None = None,
        rate_limit_cooldown_seconds: float = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
        access_denied_cooldown_seconds: float = DEFAULT_ACCESS_DENIED_COOLDOWN_SECONDS,
    ):
        self.store = store
        self.budgets = dict(DEFAULT_REQUEST_BUDGETS if budgets is None else budgets)
        self.rate_limit_cooldown_seconds = rate_limit_cooldown_seconds
        self.access_denied_cooldown_seconds = access_denied_cooldown_seconds
        self.network_counts: Counter[tuple[str, str]] = Counter()
        self.fixture_counts: Counter[tuple[str, str]] = Counter()
        self.deferred: dict[str, str] = {}

    def before_network_request(self, request_class: RequestClass) -> None:
        circuit = self.store.circuit_status(request_class.platform)
        if circuit is not None:
            remaining = circuit.remaining_seconds()
            detail = (
                f"DEFERRED — {request_class.platform} cooldown remaining "
                f"{format_duration(remaining)} (until {circuit.open_until.isoformat()}): "
                f"{circuit.reason}"
            )
            self.deferred[request_class.platform] = detail
            raise RequestDeferred(detail)

        key = (request_class.platform, request_class.kind)
        limit = self.budgets.get(key)
        if limit is not None and self.network_counts[key] >= limit:
            detail = (
                f"DEFERRED — {request_class.platform} {request_class.kind} request "
                f"budget exhausted ({self.network_counts[key]}/{limit})"
            )
            self.deferred[request_class.platform] = detail
            raise RequestDeferred(detail)
        self.network_counts[key] += 1

    def record_fixture_hit(self, request_class: RequestClass) -> None:
        self.fixture_counts[(request_class.platform, request_class.kind)] += 1

    def record_response(
        self,
        request_class: RequestClass,
        response: httpx.Response,
        latency_ms: float,
    ) -> bool:
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        outcome = "http_ok" if response.status_code < 400 else f"http_{response.status_code}"
        self.store.record_request(
            request_class.telemetry_key,
            outcome,
            status_code=response.status_code,
            latency_ms=latency_ms,
            retry_after_seconds=retry_after,
        )

        if response.status_code == 429:
            seconds = retry_after if retry_after is not None else self.rate_limit_cooldown_seconds
            reason = "HTTP 429 rate limited"
            self.store.open_circuit(request_class.platform, reason, seconds)
            circuit = self.store.circuit_status(request_class.platform)
            if circuit is not None:
                self.deferred[request_class.platform] = self._circuit_line(circuit)
            return False
        elif response.status_code in (401, 403):
            reason = f"HTTP {response.status_code} access denied"
            self.store.open_circuit(
                request_class.platform,
                reason,
                self.access_denied_cooldown_seconds,
            )
            circuit = self.store.circuit_status(request_class.platform)
            if circuit is not None:
                self.deferred[request_class.platform] = self._circuit_line(circuit)
            return False
        elif self._looks_like_challenge(response):
            reason = "challenge page detected"
            self.store.open_circuit(
                request_class.platform,
                reason,
                self.access_denied_cooldown_seconds,
            )
            circuit = self.store.circuit_status(request_class.platform)
            if circuit is not None:
                self.deferred[request_class.platform] = self._circuit_line(circuit)
            return False
        return response.status_code < 400

    def record_error(
        self,
        request_class: RequestClass,
        latency_ms: float,
        error: Exception,
    ) -> None:
        self.store.record_request(
            request_class.telemetry_key,
            f"network_error:{type(error).__name__}",
            latency_ms=latency_ms,
        )

    def record_browser_navigation(
        self,
        request_class: RequestClass,
        latency_ms: float,
        *,
        outcome: str = "browser_ok",
        challenge: bool = False,
    ) -> None:
        """Record a governed top-level browser navigation.

        Subresource requests are not counted as separate navigations.  A
        recognised challenge opens the same persistent platform circuit used
        by governed HTTP requests.
        """
        self.store.record_request(
            request_class.telemetry_key,
            outcome,
            latency_ms=latency_ms,
        )
        if challenge:
            reason = "browser challenge page detected"
            self.store.open_circuit(
                request_class.platform,
                reason,
                self.access_denied_cooldown_seconds,
            )
            circuit = self.store.circuit_status(request_class.platform)
            if circuit is not None:
                self.deferred[request_class.platform] = self._circuit_line(circuit)

    @staticmethod
    def _looks_like_challenge(response: httpx.Response) -> bool:
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type and not response.content[:256].lstrip().startswith(b"<"):
            return False
        text = response.text[:500_000].lower()
        return any(marker in text for marker in _CHALLENGE_MARKERS)

    @staticmethod
    def _circuit_line(circuit: CircuitStatus) -> str:
        return (
            f"DEFERRED — cooldown remaining {format_duration(circuit.remaining_seconds())} "
            f"(until {circuit.open_until.isoformat()}): {circuit.reason}"
        )

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        keys = sorted(set(self.network_counts) | set(self.fixture_counts) | set(self.budgets))
        for platform, kind in keys:
            network = self.network_counts[(platform, kind)]
            fixtures = self.fixture_counts[(platform, kind)]
            limit = self.budgets.get((platform, kind))
            if network == 0 and fixtures == 0:
                continue
            budget_text = f"/{limit}" if limit is not None else ""
            fixture_text = f", fixture hits: {fixtures}" if fixtures else ""
            lines.append(
                f"[{platform}] {kind} network requests: {network}{budget_text}{fixture_text}"
            )
        for platform, detail in sorted(self.deferred.items()):
            lines.append(f"[{platform}] {detail}")
        return lines


class GovernedHttpClient:
    """Small ``httpx.Client`` proxy with offline replay and live governance."""

    def __init__(
        self,
        client: httpx.Client,
        fixture_store: HttpFixtureStore,
        governor: RequestGovernor,
        *,
        offline: bool = False,
        record_fixtures: bool = False,
    ):
        if offline and record_fixtures:
            raise ValueError("--offline and --record-fixtures cannot be used together")
        self._client = client
        self.fixture_store = fixture_store
        self.governor = governor
        self.offline = offline
        self.record_fixtures = record_fixtures

    def request(
        self,
        method: str,
        url: str | httpx.URL,
        *,
        request_class: RequestClass | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        build_kwargs = dict(kwargs)
        build_kwargs.pop("follow_redirects", None)
        request = self._client.build_request(method, url, **build_kwargs)
        classified = request_class or classify_request(request.url)

        if self.offline:
            response = self.fixture_store.load(request)
            self.governor.record_fixture_hit(classified)
            return response

        self.governor.before_network_request(classified)
        started = time.monotonic()
        try:
            try:
                response = self._client.request(method, url, **kwargs)
            except _TRANSIENT_ERRORS:
                # One retry for idempotent GETs: a single dropped TLS handshake
                # (seen live) otherwise discarded a whole discovery search.
                if method.upper() != "GET":
                    raise
                time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
                response = self._client.request(method, url, **kwargs)
        except Exception as error:
            self.governor.record_error(
                classified,
                latency_ms=(time.monotonic() - started) * 1000,
                error=error,
            )
            raise
        safe_to_record = self.governor.record_response(
            classified,
            response,
            latency_ms=(time.monotonic() - started) * 1000,
        )
        # Gallery bytes are deliberately not retained.  Their derived pHashes
        # already live in the expiring hash cache and are sufficient for normal
        # identity replay.
        if self.record_fixtures and safe_to_record and classified.kind != "image":
            self.fixture_store.save(request, response)
        return response

    def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def get_pricing(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        """Make an Airbnb quote request under its independent one-call budget."""
        return self.request(
            "GET",
            url,
            request_class=RequestClass("airbnb", "pricing"),
            **kwargs,
        )

    def post_pricing(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        """Make one Airbnb checkout API call under the pricing budget."""
        return self.request(
            "POST",
            url,
            request_class=RequestClass("airbnb", "pricing"),
            **kwargs,
        )

    def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GovernedHttpClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)
