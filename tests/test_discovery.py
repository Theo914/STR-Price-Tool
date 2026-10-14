import json
from urllib.parse import parse_qs, urlsplit

import httpx

from parity.core.models import PropertyRef
from parity.core.neighbours import (
    AirbnbSearchTrace,
    _airbnb_domain_switch_origin,
    _iter_search_results,
    _parse_result,
)
from parity.discovery.engine import DiscoveryEngine, canonicalize_url, score_candidate
from parity.discovery.providers import AirbnbMapProvider, BookingSearchProvider


class _Provider:
    def __init__(self, name, refs=None, error=None):
        self.name = name
        self.refs = refs or []
        self.error = error

    def discover(self, subject):
        if self.error:
            raise RuntimeError(self.error)
        return self.refs


def _subject():
    return PropertyRef(
        site="travelstaytion",
        raw_id="742313",
        url="https://www.travelstaytion.com/stay/742313",
        title="Villa Sa Planeta in Sant Josep de Sa Talaia",
        lat=38.9,
        lng=1.3,
        bedrooms=3,
        bathrooms=2,
        capacity=6,
        property_type="Villa",
    )


def test_structurally_consistent_candidate_ranks_above_conflicting_candidate():
    subject = _subject()
    consistent = PropertyRef(
        site="airbnb",
        raw_id="1",
        url="https://www.airbnb.com/rooms/1",
        title="Villa Sa Planeta",
        lat=38.9002,
        lng=1.3002,
        bedrooms=3,
        bathrooms=2,
        capacity=6,
        property_type="Villa",
    )
    conflicting = PropertyRef(
        site="airbnb",
        raw_id="2",
        url="https://www.airbnb.com/rooms/2",
        title="Villa Sa Planeta",
        lat=39.1,
        lng=1.5,
        bedrooms=1,
        bathrooms=1,
        capacity=2,
    )

    good_score, _ = score_candidate(subject, consistent)
    bad_score, _ = score_candidate(subject, conflicting)

    assert good_score > bad_score


def test_engine_deduplicates_tracking_urls_and_merges_sources():
    first = PropertyRef(
        site="airbnb",
        raw_id="1",
        url="https://www.airbnb.com/rooms/1?source=search",
        title="Villa Sa Planeta",
        bedrooms=3,
    )
    second = PropertyRef(
        site="airbnb",
        raw_id="1",
        url="https://www.airbnb.com/rooms/1?check_in=2026-10-01",
        title="Villa Sa Planeta",
        bedrooms=3,
    )
    report = DiscoveryEngine([
        _Provider("source_a", [first]),
        _Provider("source_b", [second]),
    ]).discover(_subject())

    assert len(report.candidates) == 1
    assert report.candidates[0].sources == ["source_a", "source_b"]


def test_provider_failure_does_not_destroy_other_results():
    candidate = PropertyRef(
        site="airbnb",
        raw_id="1",
        url="https://www.airbnb.com/rooms/1",
    )
    report = DiscoveryEngine([
        _Provider("broken", error="temporarily unavailable"),
        _Provider("working", [candidate]),
    ]).discover(_subject())

    assert len(report.candidates) == 1
    assert report.provider_errors == {"broken": "temporarily unavailable"}


def test_canonical_url_removes_query_and_fragment():
    assert canonicalize_url("HTTPS://WWW.AIRBNB.COM/rooms/1/?x=1#photos") == (
        "https://www.airbnb.com/rooms/1"
    )


def test_airbnb_provider_expands_radius_only_when_first_pass_is_small(monkeypatch):
    provider = AirbnbMapProvider(client=object(), minimum_candidates=2)
    calls = []

    def fake_search(subject, radius):
        calls.append(radius)
        trace = AirbnbSearchTrace(radius_km=radius, requests=1, statuses=[200])
        if radius == provider.initial_radius_km:
            return (
                [PropertyRef(site="airbnb", raw_id="1", url="https://www.airbnb.com/rooms/1")],
                trace,
                0,
            )
        return (
            [
                PropertyRef(site="airbnb", raw_id="1", url="https://www.airbnb.com/rooms/1"),
                PropertyRef(site="airbnb", raw_id="2", url="https://www.airbnb.com/rooms/2"),
            ],
            trace,
            0,
        )

    monkeypatch.setattr(provider, "_search", fake_search)
    result = provider.discover(_subject())

    assert calls == [provider.initial_radius_km, provider.expanded_radius_km]
    assert [ref.raw_id for ref in result.refs] == ["1", "2"]
    assert "2 unique candidate(s)" in result.diagnostic


def test_discovery_parser_keeps_candidate_without_price():
    raw = {
        "demandStayListing": {
            "id": "1099791623724969319",
            "title": "Villa Sa Planeta",
            "personCapacity": 6,
            "location": {"coordinate": {"latitude": 38.87, "longitude": 1.30}},
        },
        "structuredContent": {"primaryLine": "3 beds · 2 baths"},
    }

    assert _parse_result(raw) is None
    candidate = _parse_result(raw, require_price=False)
    assert candidate is not None
    assert candidate.ref.raw_id == "1099791623724969319"
    assert candidate.comparable_total is None


def test_discovery_parser_decodes_graphql_room_id_and_nested_structure():
    raw = {
        "demandStayListing": {
            "id": "RGVtYW5kU3RheUxpc3Rpbmc6NjM4MjE1ODY5MDIxNTY4MDgz",
            "location": {"coordinate": {"latitude": 38.87, "longitude": 1.30}},
        },
        "structuredContent": {
            "primaryLine": [{"body": "3 bedrooms"}, {"body": "2 baths"}],
        },
    }

    candidate = _parse_result(raw, require_price=False)

    assert candidate is not None
    assert candidate.ref.raw_id == "638215869021568083"
    assert candidate.ref.url == "https://www.airbnb.com/rooms/638215869021568083"
    assert candidate.ref.bedrooms == 3
    assert candidate.ref.bathrooms == 2
    assert candidate.ref.extra["graphql_id"].startswith("RGVtYW5k")


def test_airbnb_search_reports_missing_embedded_state():
    def handler(request):
        return httpx.Response(200, text="<html><title>Homes</title><body>ordinary page</body></html>")

    trace = AirbnbSearchTrace(radius_km=1)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        try:
            list(_iter_search_results(client, 38.87, 1.30, 3, trace=trace))
        except RuntimeError as error:
            assert "could not read embedded page data" in str(error)
        else:
            raise AssertionError("missing search state was silently treated as zero results")
    assert trace.stop_reason == "missing_or_invalid_embedded_state"


def test_airbnb_search_records_honest_zero_results():
    state = {"searchResults": []}
    html = f'<script id="data-deferred-state-0">{json.dumps(state)}</script>'

    def handler(request):
        assert request.headers["user-agent"].startswith("Mozilla/5.0")
        assert "br" not in request.headers["accept-encoding"]
        return httpx.Response(200, text=html)

    trace = AirbnbSearchTrace(radius_km=1)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert list(_iter_search_results(client, 38.87, 1.30, 3, trace=trace)) == []
    assert trace.requests == 1
    assert trace.raw_results == 0
    assert trace.stop_reason == "no_results"


def test_airbnb_search_follows_owned_regional_domain_handoff_once():
    state = {"searchResults": []}
    result_html = f'<script id="data-deferred-state-0">{json.dumps(state)}</script>'
    handoff_html = (
        '<form method="POST" '
        'action="https://www.airbnb.com.au/v2/domain_switch/handoff"></form>'
    )
    requested_hosts = []

    def handler(request):
        requested_hosts.append(request.url.host)
        if request.url.host == "www.airbnb.com":
            return httpx.Response(200, text=handoff_html, request=request)
        return httpx.Response(200, text=result_html, request=request)

    trace = AirbnbSearchTrace(radius_km=1)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert list(_iter_search_results(client, 38.87, 1.30, 3, trace=trace)) == []

    assert requested_hosts == ["www.airbnb.com", "www.airbnb.com.au"]
    assert trace.search_origin == "https://www.airbnb.com.au"
    assert trace.requests == 2
    assert trace.stop_reason == "no_results"


def test_domain_handoff_rejects_non_airbnb_host():
    malicious = (
        '<form action="https://www.airbnb.example.com/v2/domain_switch/handoff"></form>'
    )
    assert _airbnb_domain_switch_origin(malicious) is None


def test_airbnb_search_rejects_binary_body_labelled_as_html():
    def handler(request):
        return httpx.Response(
            200,
            content=b"\xfd\x07\x00\x00\x81\x82\x83" * 200,
            headers={"Content-Type": "text/html"},
            request=request,
        )

    trace = AirbnbSearchTrace(radius_km=1)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        try:
            list(_iter_search_results(client, 38.87, 1.30, 3, trace=trace))
        except RuntimeError as error:
            assert "binary-looking body" in str(error)
        else:
            raise AssertionError("binary response was parsed as HTML")
    assert trace.stop_reason == "binary_response"


def test_booking_search_url_uses_subject_identity_fields():
    url = BookingSearchProvider._search_url(_subject())

    assert url.startswith("https://www.booking.com/searchresults.en-gb.html?")
    assert "Villa+Sa+Planeta" in url
    assert "selected_currency=EUR" in url


def test_booking_search_builds_bounded_current_identity_variants():
    subject = _subject()
    subject.city = "Sant Josep"
    subject.country = "Spain"
    subject.postcode = "07830"
    subject.registration_ids = ["ETV-1234-E"]

    queries = BookingSearchProvider._query_variants(subject)

    assert queries == [
        "Villa Sa Planeta in Sant Josep de Sa Talaia Sant Josep Spain",
        "Villa Sa Planeta in Sant Josep de Sa Talaia 07830",
        "ETV-1234-E Sant Josep",
        "Villa Sa Planeta in Sant Josep de Sa Talaia Sant Josep",
    ]


def test_booking_search_never_falls_back_to_city_only_without_registration():
    subject = _subject()
    subject.city = "London"
    subject.country = "United Kingdom"
    subject.postcode = "SW6 6UB"
    subject.registration_ids = []
    subject.licence_number = None

    queries = BookingSearchProvider._query_variants(subject)

    assert "London" not in queries
    assert queries == [
        "Villa Sa Planeta in Sant Josep de Sa Talaia London United Kingdom",
        "Villa Sa Planeta in Sant Josep de Sa Talaia SW6 6UB",
        "Villa Sa Planeta in Sant Josep de Sa Talaia London",
    ]


def test_booking_search_replays_multiple_queries_and_deduplicates_candidates():
    subject = _subject()
    subject.city = "Sant Josep"
    subject.country = "Spain"
    subject.postcode = "07830"
    subject.registration_ids = ["ETV-1234-E"]

    class OfflineSession:
        offline = True

        def __init__(self):
            self.queries = []

        def load_snapshot(self, platform, kind, request_url):
            query = parse_qs(urlsplit(request_url).query)["ss"][0]
            self.queries.append(query)
            suffix = "exact" if query.startswith("ETV-") else "possible"
            return {
                "query": query,
                "candidates": [
                    {
                        "url": "https://www.booking.com/hotel/es/villa-sa-planeta.en-gb.html",
                        "title": "Villa Sa Planeta",
                        "query": query,
                    },
                    {
                        "url": f"https://www.booking.com/hotel/es/{suffix}.en-gb.html",
                        "title": suffix,
                        "query": query,
                    },
                ],
            }

    session = OfflineSession()
    result = BookingSearchProvider(session).discover(subject)

    assert len(session.queries) == 4
    assert len(result.refs) == 3
    shared = next(ref for ref in result.refs if ref.raw_id == "villa-sa-planeta.en-gb")
    assert len(shared.extra["booking_search_queries"]) == 4
    assert "4/4 bounded identity search(es)" in result.diagnostic


def test_booking_search_snapshot_keeps_only_canonical_property_urls():
    refs = BookingSearchProvider._snapshot_refs({
        "candidates": [
            {
                "url": "https://www.booking.com/hotel/es/villa-sa-planeta.en-gb.html?aid=1",
                "title": " Villa Sa Planeta ",
            },
            {"url": "https://www.booking.com/searchresults.html", "title": "noise"},
        ]
    })

    assert len(refs) == 1
    assert refs[0].site == "booking"
    assert refs[0].url == (
        "https://www.booking.com/hotel/es/villa-sa-planeta.en-gb.html"
    )
    assert refs[0].title == "Villa Sa Planeta"
