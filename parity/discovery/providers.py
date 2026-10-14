from __future__ import annotations

import re
import time
from urllib.parse import urlencode, urlsplit

import httpx

from parity.adapters.browser.booking import canonical_booking_url
from parity.adapters.browser.browser_session import BrowserSession
from parity.core.models import PropertyRef, ProviderDiscoveryResult
from parity.core.neighbours import AirbnbSearchTrace, _iter_search_results, _parse_result
from parity.http_control import RequestDeferred
from parity.storage import EvidenceStore


class KnownMappingsProvider:
    name = "known_verified_mapping"

    def __init__(self, store: EvidenceStore):
        self.store = store

    def discover(self, subject: PropertyRef) -> ProviderDiscoveryResult:
        refs = self.store.load_verified_refs(subject)
        for ref in refs:
            ref.extra["verified_mapping"] = True
        return ProviderDiscoveryResult(
            refs=refs,
            diagnostic=f"completed; loaded {len(refs)} fresh verified mapping(s)",
        )


class SeedProvider:
    name = "operator_seed"

    def __init__(self, refs: list[PropertyRef]):
        self.refs = refs

    def discover(self, subject: PropertyRef) -> list[PropertyRef]:
        return list(self.refs)


class HistoricalEvidenceProvider:
    """Offer prior local mappings/candidates for fresh identity verification."""

    name = "historical_local_evidence"

    def __init__(self, refs: list[PropertyRef]):
        self.refs = refs

    def discover(self, subject: PropertyRef) -> ProviderDiscoveryResult:
        return ProviderDiscoveryResult(
            refs=list(self.refs),
            diagnostic=(
                f"completed; loaded {len(self.refs)} prior candidate(s) for "
                "fresh verification"
            ),
        )


class AirbnbMapProvider:
    """Generate nearby Airbnb candidates without assuming a licence exists."""

    name = "airbnb_map_search"

    def __init__(
        self,
        client: httpx.Client,
        initial_radius_km: float = 1.0,
        expanded_radius_km: float = 3.0,
        minimum_candidates: int = 5,
        max_pages_per_radius: int = 2,
    ):
        self.client = client
        self.initial_radius_km = initial_radius_km
        self.expanded_radius_km = expanded_radius_km
        self.minimum_candidates = minimum_candidates
        self.max_pages_per_radius = max_pages_per_radius
        self.search_origin = "https://www.airbnb.com"

    def _search(
        self, subject: PropertyRef, radius_km: float
    ) -> tuple[list[PropertyRef], AirbnbSearchTrace, int]:
        refs: list[PropertyRef] = []
        parse_failures = 0
        trace = AirbnbSearchTrace(radius_km=radius_km)
        for raw in _iter_search_results(
            self.client,
            subject.lat,
            subject.lng,
            subject.bedrooms,
            max_pages=self.max_pages_per_radius,
            max_distance_km=radius_km,
            amenity_ids=None,
            trace=trace,
            search_origin=self.search_origin,
        ):
            # Candidate discovery needs identity fields, not a dated price.
            # A valid listing must not disappear merely because the map card
            # omits a price.
            parsed = _parse_result(raw, require_price=False)
            if parsed is not None:
                refs.append(parsed.ref)
            else:
                parse_failures += 1
        self.search_origin = trace.search_origin
        return refs, trace, parse_failures

    def discover(self, subject: PropertyRef) -> ProviderDiscoveryResult:
        if subject.lat is None or subject.lng is None or subject.bedrooms is None:
            raise ValueError("Airbnb map discovery requires subject coordinates and bedrooms")
        refs, first_trace, first_failures = self._search(subject, self.initial_radius_km)
        traces = [first_trace]
        parse_failures = first_failures
        if len({ref.raw_id for ref in refs}) < self.minimum_candidates:
            expanded, expanded_trace, expanded_failures = self._search(
                subject, self.expanded_radius_km
            )
            refs.extend(expanded)
            traces.append(expanded_trace)
            parse_failures += expanded_failures
        deduped: dict[str, PropertyRef] = {}
        for ref in refs:
            deduped[ref.raw_id] = ref
        trace_text = "; ".join(
            f"{trace.radius_km:g}km: {trace.requests} request(s), "
            f"HTTP {','.join(map(str, trace.statuses)) or '--'}, "
            f"host(s) {','.join(trace.hosts) or '--'}, "
            f"encoding(s) {','.join(trace.content_encodings) or '--'}, "
            f"{trace.raw_results} raw result(s), stopped={trace.stop_reason}"
            for trace in traces
        )
        diagnostic = (
            f"completed; {trace_text}; {parse_failures} unparseable result(s); "
            f"{len(deduped)} unique candidate(s)"
        )
        return ProviderDiscoveryResult(refs=list(deduped.values()), diagnostic=diagnostic)


class BookingSearchProvider:
    """Find Booking property-page candidates from the public search results.

    Search results are candidate generation only.  A returned URL remains
    unverified and cannot be priced until the normal licence/photo identity
    gate succeeds.
    """

    name = "booking_public_search"

    def __init__(
        self,
        session: BrowserSession,
        max_results: int = 8,
        max_queries: int = 4,
    ):
        self.session = session
        self.max_results = max_results
        self.max_queries = max_queries

    @staticmethod
    def _query_variants(subject: PropertyRef) -> list[str]:
        """Build bounded, independent searches from current subject evidence.

        Booking's public search is inconsistent: some properties resolve from a
        title and city, while others only surface with a postcode or published
        registration identifier.  These variants are candidate generation
        only; every returned property still passes the normal identity gate.
        """

        def clean(value: object) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip()

        title = clean(subject.title)
        city = clean(subject.city)
        country = clean(subject.country)
        postcode = clean(subject.postcode)
        registrations = []
        for value in [*subject.registration_ids, subject.licence_number]:
            item = clean(value)
            if item and item.casefold() not in {entry.casefold() for entry in registrations}:
                registrations.append(item)

        raw = [
            " ".join(part for part in (title, city, country) if part),
            " ".join(part for part in (title, postcode) if part),
        ]
        if registrations:
            raw.append(" ".join(part for part in (registrations[0], city) if part))
        raw.append(" ".join(part for part in (title, city) if part))
        if not title:
            raw.extend([
                " ".join(part for part in (postcode, city, country) if part),
                " ".join(part for part in (city, country) if part),
            ])

        queries: list[str] = []
        seen: set[str] = set()
        for query in raw:
            key = query.casefold()
            if query and key not in seen:
                queries.append(query)
                seen.add(key)
        if not queries:
            raise ValueError("Booking discovery requires a TravelStaytion title or location")
        return queries

    @classmethod
    def _query(cls, subject: PropertyRef) -> str:
        """Backward-compatible primary query used by older callers/tests."""
        return cls._query_variants(subject)[0]

    @staticmethod
    def _search_url_for_query(query: str) -> str:
        return "https://www.booking.com/searchresults.en-gb.html?" + urlencode({
            "ss": query,
            "lang": "en-gb",
            "selected_currency": "EUR",
        })

    @classmethod
    def _search_url(cls, subject: PropertyRef) -> str:
        return cls._search_url_for_query(cls._query(subject))

    @staticmethod
    def _snapshot_refs(snapshot: dict) -> list[PropertyRef]:
        values = snapshot.get("candidates")
        if not isinstance(values, list):
            raise ValueError("Booking discovery snapshot has no candidates array")
        refs: list[PropertyRef] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            url = value.get("url")
            title = value.get("title")
            if not isinstance(url, str):
                continue
            try:
                canonical = canonical_booking_url(url)
            except ValueError:
                continue
            refs.append(PropertyRef(
                site="booking",
                raw_id=urlsplit(canonical).path.rsplit("/", 1)[-1][:-5],
                url=canonical,
                title=title.strip() if isinstance(title, str) and title.strip() else None,
                extra={
                    "booking_search_query": str(value.get("query") or "").strip()
                },
            ))
        return refs

    def _live_search(self, query: str) -> tuple[list[PropertyRef], str]:
        search_url = self._search_url_for_query(query)
        self.session.before_navigation("booking", "search")
        started = time.monotonic()
        page = self.session.new_page()
        recorded = False
        try:
            response = page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
            BrowserSession.decline_cookies(page)
            page.wait_for_timeout(1400)
            title = page.title()
            body_text = page.inner_text("body")
            if BrowserSession.is_bot_challenge(f"{title}\n{body_text}"):
                self.session.record_navigation(
                    "booking",
                    "search",
                    (time.monotonic() - started) * 1000,
                    outcome="browser_challenge",
                    challenge=True,
                )
                recorded = True
                raise RequestDeferred(
                    "DEFERRED — Booking.com search challenge detected; persistent cooldown opened"
                )
            status = response.status if response is not None else None
            if status is not None and status >= 400:
                raise ValueError(f"Booking.com search returned HTTP {status}")
            raw = page.locator(
                '[data-testid="property-card"], a[href*="/hotel/"]'
            ).evaluate_all(
                """elements => elements.map(element => {
                    const card = element.matches('[data-testid="property-card"]')
                        ? element : element.closest('[data-testid="property-card"]');
                    const link = element.matches('a[href*="/hotel/"]')
                        ? element : (card && card.querySelector('a[href*="/hotel/"]'));
                    const titleNode = card && (
                        card.querySelector('[data-testid="title"]') ||
                        card.querySelector('div[data-testid="title"]')
                    );
                    return {
                        url: link ? link.href : '',
                        title: titleNode ? (titleNode.textContent || '') :
                            (link ? (link.textContent || '') : '')
                    };
                }).filter(item => item.url)"""
            )
            candidates = []
            seen = set()
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    canonical = canonical_booking_url(str(item.get("url") or ""))
                except ValueError:
                    continue
                if canonical in seen:
                    continue
                seen.add(canonical)
                candidates.append({
                    "url": canonical,
                    "title": re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:240],
                    "query": query,
                })
                if len(candidates) >= self.max_results:
                    break
            snapshot = {
                "query": query,
                "candidates": candidates,
            }
            refs = self._snapshot_refs(snapshot)
            self.session.record_navigation(
                "booking", "search", (time.monotonic() - started) * 1000
            )
            recorded = True
            self.session.save_snapshot("booking", "search", search_url, snapshot)
            return refs, search_url
        except (RequestDeferred, ValueError):
            if not recorded:
                self.session.record_navigation(
                    "booking",
                    "search",
                    (time.monotonic() - started) * 1000,
                    outcome="browser_error",
                )
            raise
        except Exception as error:
            if not recorded:
                self.session.record_navigation(
                    "booking",
                    "search",
                    (time.monotonic() - started) * 1000,
                    outcome=f"browser_error:{type(error).__name__}",
                )
            raise
        finally:
            page.close()

    def discover(self, subject: PropertyRef) -> ProviderDiscoveryResult:
        queries = self._query_variants(subject)[: self.max_queries]
        deduped: dict[str, PropertyRef] = {}
        failures: list[str] = []
        completed = 0
        for query in queries:
            search_url = self._search_url_for_query(query)
            try:
                if self.session.offline:
                    snapshot = self.session.load_snapshot("booking", "search", search_url)
                    refs = self._snapshot_refs(snapshot)
                else:
                    refs, _ = self._live_search(query)
                completed += 1
            except RequestDeferred:
                raise
            except Exception as error:
                failures.append(f"{query!r}: {error}")
                continue
            for ref in refs:
                key = canonical_booking_url(ref.url)
                existing = deduped.get(key)
                if existing is None:
                    deduped[key] = ref
                    continue
                prior = existing.extra.get("booking_search_queries") or [
                    existing.extra.get("booking_search_query")
                ]
                current = ref.extra.get("booking_search_query")
                existing.extra["booking_search_queries"] = [
                    item for item in dict.fromkeys([*prior, current]) if item
                ]

        if not deduped and failures:
            raise ValueError(
                "Booking discovery produced no candidates; " + " | ".join(failures)
            )
        mode = "replayed" if self.session.offline else "completed"
        failure_text = f"; {len(failures)} failed" if failures else ""
        return ProviderDiscoveryResult(
            refs=list(deduped.values()),
            diagnostic=(
                f"{mode}; {completed}/{len(queries)} bounded identity search(es) "
                f"completed{failure_text}; {len(deduped)} unique property candidate(s)"
            ),
        )
