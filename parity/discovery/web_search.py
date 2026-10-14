"""Licence-first reverse web search for Airbnb and Booking.com candidates.

A manual exact search for a TravelStaytion licence/registration number often
finds the same property on Airbnb and Booking.com, while OTA-internal search
does not.  This provider repeats that manual search in the operator's own
browser profile:

1. Normalise every registration identifier into a few exact-match variants.
2. Run quoted searches restricted to Airbnb room pages and Booking hotel pages
   (plus one unrestricted exact search), then a title/city fallback.
3. Use Bing, DuckDuckGo and Google in turn; a blocked engine is skipped, not
   fatal.  In a visible browser a challenge page is handed to the operator,
   who can solve it in the window; the provider never attempts to solve or
   evade a challenge itself.
4. Extract canonical ``/rooms/<id>`` and ``/hotel/..html`` links, unwrapping
   search-engine redirect links.

Every result is candidate evidence only.  It must pass the normal identity
gate before it can be priced.
"""
from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, quote_plus, unquote, urlsplit

from parity.adapters.browser.booking import canonical_booking_url
from parity.core.models import PropertyRef, ProviderDiscoveryResult
from parity.http_control import RequestDeferred

WEB_SEARCH_ENGINES = ("bing", "duckduckgo", "google")
DEFAULT_WEB_SEARCH_BUDGET = 6

_AIRBNB_HOST_RE = re.compile(r"^(?:[a-z]{2,3}\.)?(?:www\.)?airbnb\.[a-z]{2,3}(?:\.[a-z]{2})?$")
_AIRBNB_ROOM_RE = re.compile(r"^/rooms/(?:plus/)?(\d{3,})(?:/|$)")
_BOOKING_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.)?booking\.com$")
_REGISTRATION_SPLIT_RE = re.compile(r"[\s\-_/.:]+")
_CHALLENGE_MARKERS = (
    "unusual traffic",
    "not a robot",
    "verify you are human",
    "verify you're human",
    "solve the challenge",
    "captcha",
    "are you a robot",
    "our systems have detected",
    "one last step",
    "bots use duckduckgo too",
    "confirm this search was made by a human",
    "select all squares",
)
_NO_RESULTS_MARKERS = (
    "no results found",
    "did not match any documents",
    "there are no results for",
    "no results for",
)


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def registration_variants(subject: PropertyRef) -> list[str]:
    """Return distinct, searchable forms of every published registration id.

    Registration numbers are published inconsistently: ``HUTB-012345`` may
    appear on an OTA as ``HUTB012345`` or ``HUTB 012345``.  Short or purely
    generic values are discarded because they would match unrelated pages.
    """
    raw: list[str] = []
    for value in [*subject.registration_ids, subject.licence_number]:
        item = _clean(value)
        if item:
            raw.append(item)

    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip()
        compact = re.sub(r"[^0-9A-Za-z]", "", value)
        # Require a reasonably distinctive identifier with at least one digit.
        if len(compact) < 5 or not re.search(r"\d", compact):
            return
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            variants.append(value)

    for item in raw:
        add(item)
        parts = [part for part in _REGISTRATION_SPLIT_RE.split(item) if part]
        if len(parts) > 1:
            add("".join(parts))
            add("-".join(parts))
    return variants


@dataclass(frozen=True)
class WebQuery:
    text: str
    kind: str  # "licence" or "title"
    target: str  # "airbnb", "booking" or "any"


def build_web_queries(subject: PropertyRef, max_registrations: int = 2) -> list[WebQuery]:
    """Licence-first queries, then a title/location fallback per channel."""
    queries: list[WebQuery] = []
    for registration in registration_variants(subject)[:max_registrations]:
        exact = f'"{registration}"'
        queries.append(WebQuery(f"{exact} site:airbnb.com", "licence", "airbnb"))
        queries.append(WebQuery(f"{exact} site:booking.com", "licence", "booking"))
        queries.append(WebQuery(exact, "licence", "any"))

    title = _clean(subject.title)
    location = " ".join(part for part in (_clean(subject.city), _clean(subject.country)) if part)
    if title:
        tail = f" {location}" if location else ""
        queries.append(WebQuery(f'"{title}"{tail} site:booking.com', "title", "booking"))
        queries.append(WebQuery(f'"{title}"{tail} site:airbnb.com', "title", "airbnb"))
    return queries


def search_url(engine: str, query: str) -> str:
    encoded = quote_plus(query)
    if engine == "bing":
        return f"https://www.bing.com/search?q={encoded}&setlang=en-GB&count=30"
    if engine == "duckduckgo":
        return f"https://html.duckduckgo.com/html/?q={encoded}&kl=uk-en"
    if engine == "google":
        return f"https://www.google.com/search?q={encoded}&hl=en&num=30&filter=0"
    raise ValueError(f"unsupported web search engine: {engine}")


def _decode_bing_redirect(value: str) -> str | None:
    # Bing wraps results as /ck/a?...&u=a1<base64url(target)>
    if not value.startswith("a1"):
        return None
    payload = value[2:]
    payload += "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(payload).decode("utf-8", errors="strict")
    except (ValueError, UnicodeDecodeError):
        return None


def unwrap_result_url(href: str) -> str:
    """Return the destination behind a search-engine redirect link."""
    href = _clean(href)
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urlsplit(href)
    except ValueError:
        return href
    host = (parts.hostname or "").lower()
    params = parse_qs(parts.query)
    if host.endswith("duckduckgo.com") and parts.path.startswith("/l/") and params.get("uddg"):
        return unquote(params["uddg"][0])
    if host.endswith("bing.com") and parts.path.startswith("/ck/") and params.get("u"):
        decoded = _decode_bing_redirect(params["u"][0])
        if decoded:
            return decoded
    if (host.endswith("google.com") or not host) and parts.path == "/url":
        for key in ("q", "url"):
            if params.get(key):
                return params[key][0]
    return href


def canonical_airbnb_room_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not _AIRBNB_HOST_RE.fullmatch(host):
        raise ValueError("not an Airbnb host")
    match = _AIRBNB_ROOM_RE.match(re.sub(r"/{2,}", "/", parts.path))
    if not match:
        raise ValueError("not an Airbnb room page")
    return f"https://www.airbnb.com/rooms/{match.group(1)}"


def _canonical_booking(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not _BOOKING_HOST_RE.fullmatch(host):
        raise ValueError("not a Booking.com host")
    path = re.sub(r"/{2,}", "/", parts.path)
    # Localised pages look like /hotel/es/name.en-gb.html; identity uses the
    # language-neutral property page.
    path = re.sub(r"\.[a-z]{2}(?:-[a-z]{2})?\.html$", ".html", path)
    return canonical_booking_url(f"https://www.booking.com{path}")


def classify_result_url(href: str) -> tuple[str, str] | None:
    """Map a result link to ``(site, canonical_url)`` or ``None``."""
    target = unwrap_result_url(href)
    for site, canonical in (("airbnb", canonical_airbnb_room_url), ("booking", _canonical_booking)):
        try:
            return site, canonical(target)
        except ValueError:
            continue
    return None


def looks_like_challenge(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


@dataclass
class QueryOutcome:
    engine: str
    query: str
    status: str  # hits | zero_results | no_ota_links | challenge | deferred | error | skipped
    links: int = 0
    candidates: int = 0
    detail: str = ""

    def line(self) -> str:
        detail = f" ({self.detail})" if self.detail else ""
        return (
            f"{self.engine} {self.query!r}: {self.status}, {self.links} link(s), "
            f"{self.candidates} OTA candidate(s){detail}"
        )


@dataclass
class SearchPage:
    """What one results page yielded: every href plus visible page text."""

    hrefs: list[str]
    text: str
    titles: dict[str, str] = field(default_factory=dict)


class WebLicenceSearchProvider:
    """Exact licence (then title) web search producing OTA candidates.

    ``session`` is a :class:`BrowserSession`.  In offline mode, saved
    structured snapshots are replayed instead of opening pages.
    """

    name = "web_licence_search"

    def __init__(
        self,
        session,
        *,
        engines: tuple[str, ...] = WEB_SEARCH_ENGINES,
        max_queries: int = 8,
        stop_when_both_found: bool = True,
        challenge_wait_seconds: float = 180.0,
        interactive: bool = True,
        pause_seconds: float = 2.5,
        sleep: Callable[[float], None] = time.sleep,
        notify: Callable[[str], None] | None = None,
        skip_sites: set[str] | frozenset[str] = frozenset(),
    ):
        self.session = session
        self.engines = engines
        self.max_queries = max_queries
        self.stop_when_both_found = stop_when_both_found
        self.challenge_wait_seconds = challenge_wait_seconds
        self.interactive = interactive
        self.pause_seconds = pause_seconds
        self.sleep = sleep
        self.notify = notify or (lambda message: None)
        # Channels already matched elsewhere; their queries are not run.
        self.skip_sites = set(skip_sites)
        self.outcomes: list[QueryOutcome] = []

    # -- page access -------------------------------------------------------

    @staticmethod
    def _snapshot_from_page(page: SearchPage) -> dict:
        return {
            "hrefs": page.hrefs[:400],
            "titles": dict(list(page.titles.items())[:400]),
            # Bounded visible text for challenge/zero-result classification.
            "text": page.text[:4000],
        }

    @staticmethod
    def _page_from_snapshot(snapshot: dict) -> SearchPage:
        hrefs = snapshot.get("hrefs")
        if not isinstance(hrefs, list):
            raise ValueError("web search snapshot has no hrefs array")
        titles = snapshot.get("titles") if isinstance(snapshot.get("titles"), dict) else {}
        return SearchPage(
            hrefs=[str(item) for item in hrefs if isinstance(item, str)],
            text=str(snapshot.get("text") or ""),
            titles={str(k): str(v) for k, v in titles.items()},
        )

    def _wait_for_operator(self, page, engine: str) -> bool:
        """Give the operator time to clear a challenge in the visible window."""
        if not self.interactive or getattr(self.session, "headless", True):
            return False
        shared = getattr(self.session, "wait_for_human", None)
        if shared is not None:
            return bool(shared(page, engine.capitalize() + " search"))
        self.notify(
            f"[parity] {engine} is asking for a human check. Please solve it in the "
            f"browser window; waiting up to {int(self.challenge_wait_seconds)}s..."
        )
        try:
            page.bring_to_front()
        except Exception:
            pass
        deadline = time.monotonic() + self.challenge_wait_seconds
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            try:
                text = page.inner_text("body")
            except Exception:
                continue
            if not looks_like_challenge(f"{page.title()}\n{text[:20000]}"):
                return True
        return False

    def _live_page(self, engine: str, url: str) -> SearchPage:
        self.session.before_navigation(engine, "search")
        started = time.monotonic()
        page = self.session.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            self.session.decline_cookies(page)
            page.wait_for_timeout(1500)
            text = page.inner_text("body")
            if looks_like_challenge(f"{page.title()}\n{text[:20000]}"):
                if not self._wait_for_operator(page, engine):
                    self.session.record_navigation(
                        engine,
                        "search",
                        (time.monotonic() - started) * 1000,
                        outcome="browser_challenge",
                        challenge=False,
                    )
                    raise _Challenge(f"{engine} showed a human-verification page")
                page.wait_for_timeout(1500)
                text = page.inner_text("body")
            raw = page.locator("a[href]").evaluate_all(
                """els => els.map(a => [a.href, (a.textContent || '').trim().slice(0, 200)])"""
            )
            hrefs: list[str] = []
            titles: dict[str, str] = {}
            for item in raw:
                if isinstance(item, list) and item and isinstance(item[0], str):
                    hrefs.append(item[0])
                    if len(item) > 1 and isinstance(item[1], str) and item[1]:
                        titles.setdefault(item[0], item[1])
            self.session.record_navigation(engine, "search", (time.monotonic() - started) * 1000)
            return SearchPage(hrefs=hrefs, text=text, titles=titles)
        finally:
            page.close()

    def _fetch(self, engine: str, query: str) -> SearchPage:
        url = search_url(engine, query)
        if getattr(self.session, "offline", False):
            return self._page_from_snapshot(self.session.load_snapshot(engine, "search", url))
        result = self._live_page(engine, url)
        self.session.save_snapshot(engine, "search", url, self._snapshot_from_page(result))
        return result

    # -- discovery ---------------------------------------------------------

    def discover(self, subject: PropertyRef) -> ProviderDiscoveryResult:
        queries = [
            query for query in build_web_queries(subject) if query.target not in self.skip_sites
        ][: self.max_queries]
        if not queries:
            return ProviderDiscoveryResult(
                refs=[],
                diagnostic="skipped; subject has no usable licence number or title",
            )
        found: dict[str, PropertyRef] = {}
        blocked: set[str] = set()
        self.outcomes = []
        attempts = 0

        for query in queries:
            sites_found = {ref.site for ref in found.values()} | self.skip_sites
            if self.stop_when_both_found and {"airbnb", "booking"} <= sites_found:
                break
            # Title fallbacks only run for a channel licence search missed.
            if query.kind == "title" and query.target in sites_found:
                continue
            # Licence queries targeting an already-found channel are skipped,
            # except the unrestricted search, which can surface both.
            if query.kind == "licence" and query.target != "any" and query.target in sites_found:
                continue
            for engine in self.engines:
                if engine in blocked:
                    continue
                if attempts and not getattr(self.session, "offline", False):
                    self.sleep(self.pause_seconds)
                attempts += 1
                try:
                    page = self._fetch(engine, query.text)
                except _Challenge as error:
                    blocked.add(engine)
                    self.outcomes.append(QueryOutcome(engine, query.text, "challenge", detail=str(error)))
                    continue
                except RequestDeferred as error:
                    blocked.add(engine)
                    self.outcomes.append(QueryOutcome(engine, query.text, "deferred", detail=str(error)))
                    continue
                except Exception as error:  # parser/browser/fixture failure
                    self.outcomes.append(
                        QueryOutcome(engine, query.text, "error", detail=f"{type(error).__name__}: {error}")
                    )
                    continue

                if looks_like_challenge(page.text) and not any(
                    classify_result_url(href) for href in page.hrefs
                ):
                    blocked.add(engine)
                    self.outcomes.append(QueryOutcome(engine, query.text, "challenge"))
                    continue

                new = 0
                for href in page.hrefs:
                    classified = classify_result_url(href)
                    if classified is None:
                        continue
                    site, canonical = classified
                    if query.target != "any" and site != query.target:
                        continue
                    ref = found.get(canonical)
                    if ref is None:
                        raw_id = (
                            canonical.rsplit("/", 1)[-1]
                            if site == "airbnb"
                            else canonical.rsplit("/", 1)[-1][:-5]
                        )
                        ref = PropertyRef(
                            site=site,
                            raw_id=raw_id,
                            url=canonical,
                            title=_clean(page.titles.get(href))[:240] or None,
                            extra={"web_search_hits": []},
                        )
                        found[canonical] = ref
                        new += 1
                    hit = {"engine": engine, "query": query.text, "kind": query.kind}
                    if hit not in ref.extra["web_search_hits"]:
                        ref.extra["web_search_hits"].append(hit)
                    if query.kind == "licence":
                        ref.extra["licence_search_hit"] = True

                low = page.text.lower()
                if new:
                    status = "hits"
                elif any(marker in low for marker in _NO_RESULTS_MARKERS):
                    status = "zero_results"
                elif not page.hrefs:
                    status = "error"
                else:
                    status = "no_ota_links"
                self.outcomes.append(
                    QueryOutcome(
                        engine,
                        query.text,
                        status,
                        links=len(page.hrefs),
                        candidates=new,
                        detail="page had no links; layout may have changed" if status == "error" else "",
                    )
                )
                # Stop at the first engine that surfaced OTA pages; engines
                # index differently (Bing ignores quotes on some licence
                # tokens), so an empty answer falls through to the next one.
                if status == "hits":
                    break

        by_site = {"airbnb": 0, "booking": 0}
        for ref in found.values():
            by_site[ref.site] = by_site.get(ref.site, 0) + 1
        licence_hits = sum(1 for ref in found.values() if ref.extra.get("licence_search_hit"))
        summary = (
            f"completed; {len(self.outcomes)} search(es) across "
            f"{', '.join(self.engines)}; blocked: {', '.join(sorted(blocked)) or 'none'}; "
            f"{by_site['airbnb']} Airbnb and {by_site['booking']} Booking candidate(s) "
            f"({licence_hits} from exact licence search)"
        )
        if self.outcomes:
            summary += " | " + " | ".join(outcome.line() for outcome in self.outcomes)
        if not found and self.outcomes and all(
            outcome.status in {"challenge", "deferred", "error"} for outcome in self.outcomes
        ):
            raise RuntimeError("web licence search failed on every engine: " + summary)
        return ProviderDiscoveryResult(refs=list(found.values()), diagnostic=summary)


class _Challenge(Exception):
    pass


class PrecomputedProvider:
    """Expose an already-run provider's result to the discovery engine."""

    def __init__(self, name: str, result: ProviderDiscoveryResult | None, error: str | None = None):
        self.name = name
        self.result = result
        self.error = error

    def discover(self, subject: PropertyRef) -> ProviderDiscoveryResult:
        if self.error is not None:
            raise RuntimeError(self.error)
        return self.result or ProviderDiscoveryResult()
