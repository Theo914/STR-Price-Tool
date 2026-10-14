import base64
from urllib.parse import quote

import pytest

from parity.core.models import PropertyRef
from parity.discovery.engine import DiscoveryEngine, score_candidate
from parity.discovery.web_search import (
    PrecomputedProvider,
    WebLicenceSearchProvider,
    build_web_queries,
    classify_result_url,
    registration_variants,
    search_url,
    unwrap_result_url,
)
from parity.http_control import RequestDeferred


def _subject(**overrides):
    values = dict(
        site="travelstaytion",
        raw_id="1069699",
        url="https://www.travelstaytion.com/stay/1069699",
        title="Villa Sa Planeta",
        city="Sant Josep de sa Talaia",
        country="Spain",
        licence_number="ET-0123-E",
        registration_ids=["ET-0123-E", "ESFCTU0000070230001234560000000000000000ET-0123-E5"],
        bedrooms=3,
    )
    values.update(overrides)
    return PropertyRef(**values)


class _FakeSession:
    """Offline session replaying per-engine search snapshots."""

    offline = True
    headless = True

    def __init__(self, pages):
        # pages: {(engine, query_text): snapshot | Exception}
        self.pages = pages
        self.requests = []

    def load_snapshot(self, platform, kind, url):
        self.requests.append((platform, url))
        for (engine, query), value in self.pages.items():
            if engine == platform and search_url(engine, query) == url:
                if isinstance(value, Exception):
                    raise value
                return value
        return {"hrefs": [], "text": "No results found for this search"}


def _bing_wrap(url):
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc&u=a1{token}&ntb=1"


def test_registration_variants_normalise_separators_and_drop_generic_values():
    subject = _subject(licence_number="HUTB 012345", registration_ids=["HUTB 012345", "N/A", "123"])
    assert registration_variants(subject) == ["HUTB 012345", "HUTB012345", "HUTB-012345"]


def test_queries_are_licence_first_then_title_per_channel():
    queries = build_web_queries(_subject(registration_ids=["ET-0123-E"]))
    texts = [query.text for query in queries]
    assert texts[0] == '"ET-0123-E" site:airbnb.com'
    assert texts[1] == '"ET-0123-E" site:booking.com'
    assert texts[2] == '"ET-0123-E"'
    assert all(query.kind == "licence" for query in queries[:6])
    assert queries[-1].kind == "title" and "site:airbnb.com" in queries[-1].text


def test_subject_without_licence_still_gets_title_queries():
    queries = build_web_queries(_subject(licence_number=None, registration_ids=[]))
    assert [query.kind for query in queries] == ["title", "title"]


@pytest.mark.parametrize(
    "href, expected",
    [
        (_bing_wrap("https://www.airbnb.co.uk/rooms/12345678?adults=2"), "https://www.airbnb.co.uk/rooms/12345678?adults=2"),
        (
            "https://duckduckgo.com/l/?uddg=" + quote("https://www.booking.com/hotel/es/sa-planeta.html", safe="") + "&rut=x",
            "https://www.booking.com/hotel/es/sa-planeta.html",
        ),
        ("https://www.google.com/url?q=https://www.airbnb.com/rooms/42&sa=U", "https://www.airbnb.com/rooms/42"),
        ("https://example.com/page", "https://example.com/page"),
    ],
)
def test_unwrap_result_url(href, expected):
    assert unwrap_result_url(href) == expected


@pytest.mark.parametrize(
    "href, expected",
    [
        ("https://www.airbnb.co.uk/rooms/12345678?check_in=2027-01-01", ("airbnb", "https://www.airbnb.com/rooms/12345678")),
        ("https://es.airbnb.com/rooms/plus/987654", ("airbnb", "https://www.airbnb.com/rooms/987654")),
        ("https://www.booking.com/hotel/es/sa-planeta.en-gb.html?aid=1", ("booking", "https://www.booking.com/hotel/es/sa-planeta.html")),
        ("https://m.booking.com/hotel/es/sa-planeta.html", ("booking", "https://www.booking.com/hotel/es/sa-planeta.html")),
        ("https://www.airbnb.com/s/Ibiza/homes", None),
        ("https://www.booking.com/searchresults.html?ss=Ibiza", None),
        ("https://www.airbnb.evil.example/rooms/12345", None),
        ("https://booking.com.evil.example/hotel/es/x.html", None),
    ],
)
def test_classify_result_url(href, expected):
    assert classify_result_url(href) == expected


def test_licence_search_finds_both_channels_and_stops():
    subject = _subject(registration_ids=["ET-0123-E"])
    session = _FakeSession({
        ("bing", '"ET-0123-E" site:airbnb.com'): {
            "hrefs": [
                _bing_wrap("https://www.airbnb.co.uk/rooms/555666"),
                "https://www.bing.com/images?q=x",
            ],
            "titles": {_bing_wrap("https://www.airbnb.co.uk/rooms/555666"): "Villa Sa Planeta - Airbnb"},
            "text": "results",
        },
        ("bing", '"ET-0123-E" site:booking.com'): {
            "hrefs": ["https://www.booking.com/hotel/es/sa-planeta.en-gb.html"],
            "text": "results",
        },
    })
    provider = WebLicenceSearchProvider(session)
    result = provider.discover(subject)

    urls = sorted(ref.url for ref in result.refs)
    assert urls == [
        "https://www.airbnb.com/rooms/555666",
        "https://www.booking.com/hotel/es/sa-planeta.html",
    ]
    assert all(ref.extra["licence_search_hit"] for ref in result.refs)
    airbnb = next(ref for ref in result.refs if ref.site == "airbnb")
    assert airbnb.title == "Villa Sa Planeta - Airbnb"
    # Stops after both channels are found: no unrestricted or title searches.
    assert len(session.requests) == 2
    assert "2 search(es)" in result.diagnostic


def test_site_restricted_query_ignores_misleading_other_channel_links():
    subject = _subject(registration_ids=["ET-0123-E"])
    session = _FakeSession({
        ("bing", '"ET-0123-E" site:airbnb.com'): {
            "hrefs": ["https://www.booking.com/hotel/es/unrelated.html"],
            "text": "results",
        },
    })
    result = WebLicenceSearchProvider(session).discover(subject)
    assert not any(ref.url.endswith("unrelated.html") for ref in result.refs)


def test_blocked_engine_falls_through_to_next_engine():
    subject = _subject(registration_ids=["ET-0123-E"])
    session = _FakeSession({
        ("bing", '"ET-0123-E" site:airbnb.com'): RequestDeferred("DEFERRED — bing cooldown"),
        ("duckduckgo", '"ET-0123-E" site:airbnb.com'): {
            "hrefs": ["https://duckduckgo.com/l/?uddg=" + quote("https://www.airbnb.com/rooms/777", safe="")],
            "text": "results",
        },
    })
    provider = WebLicenceSearchProvider(session)
    result = provider.discover(subject)
    assert [ref.url for ref in result.refs] == ["https://www.airbnb.com/rooms/777"]
    statuses = [(o.engine, o.status) for o in provider.outcomes]
    assert ("bing", "deferred") in statuses
    assert ("duckduckgo", "hits") in statuses
    # Bing is not retried after it was blocked.
    assert not any(platform == "bing" for platform, _ in session.requests[1:])


def test_challenge_snapshot_marks_engine_blocked():
    subject = _subject(registration_ids=["ET-0123-E"])
    session = _FakeSession({
        ("bing", '"ET-0123-E" site:airbnb.com'): {"hrefs": [], "text": "Please solve the challenge: are you a robot?"},
    })
    provider = WebLicenceSearchProvider(session)
    provider.discover(subject)
    assert provider.outcomes[0].status == "challenge"
    assert all(o.engine != "bing" for o in provider.outcomes[1:])


def test_every_engine_failing_is_reported_as_an_error_not_zero_results():
    subject = _subject(registration_ids=["ET-0123-E"], title=None)
    failure = RequestDeferred("DEFERRED")
    pages = {}
    for engine in ("bing", "duckduckgo", "google"):
        for query in build_web_queries(subject):
            pages[(engine, query.text)] = failure
    with pytest.raises(RuntimeError, match="failed on every engine"):
        WebLicenceSearchProvider(_FakeSession(pages)).discover(subject)


def test_honest_zero_results_are_distinguished():
    subject = _subject(registration_ids=["ET-0123-E"], title=None)
    provider = WebLicenceSearchProvider(_FakeSession({}))
    result = provider.discover(subject)
    assert result.refs == []
    assert {o.status for o in provider.outcomes} == {"zero_results"}


def test_title_fallback_runs_only_for_channel_licence_search_missed():
    subject = _subject(registration_ids=["ET-0123-E"])
    session = _FakeSession({
        ("bing", '"ET-0123-E" site:booking.com'): {
            "hrefs": ["https://www.booking.com/hotel/es/sa-planeta.html"],
            "text": "results",
        },
        ("bing", '"Villa Sa Planeta" Sant Josep de sa Talaia Spain site:airbnb.com'): {
            "hrefs": ["https://www.airbnb.com/rooms/9001"],
            "text": "results",
        },
    })
    provider = WebLicenceSearchProvider(session)
    result = provider.discover(subject)
    by_url = {ref.url: ref for ref in result.refs}
    assert by_url["https://www.booking.com/hotel/es/sa-planeta.html"].extra["licence_search_hit"] is True
    assert "licence_search_hit" not in by_url["https://www.airbnb.com/rooms/9001"].extra
    queried = [url for _, url in session.requests]
    assert not any("site%3Abooking.com" in url and "Villa" in url for url in queried)


def test_licence_hit_outranks_title_lookalike_but_is_not_verified():
    subject = _subject()
    licence_hit = PropertyRef(site="booking", raw_id="a", url="https://www.booking.com/hotel/es/a.html",
                              extra={"licence_search_hit": True})
    lookalike = PropertyRef(site="booking", raw_id="b", url="https://www.booking.com/hotel/es/b.html",
                            title="Villa Sa Planeta")
    assert score_candidate(subject, licence_hit)[0] > score_candidate(subject, lookalike)[0]
    report = DiscoveryEngine([
        PrecomputedProvider("web_licence_search", None, error="all engines blocked"),
    ]).discover(subject)
    assert report.provider_errors == {"web_licence_search": "all engines blocked"}
    assert "verified_mapping" not in licence_hit.extra


def test_cli_helper_reports_found_channels_and_skips_when_disabled(monkeypatch):
    from argparse import Namespace

    from parity import cli

    subject = _subject(registration_ids=["ET-0123-E"])
    session = _FakeSession({
        ("bing", '"ET-0123-E" site:booking.com'): {
            "hrefs": ["https://www.booking.com/hotel/es/sa-planeta.html"],
            "text": "results",
        },
    })
    providers = []
    sites = cli._run_web_licence_search(
        Namespace(headless=True, no_web_search=False), subject, session, set(), providers
    )
    assert sites == {"booking"}
    assert providers[0].name == "web_licence_search"
    assert len(providers[0].discover(subject).refs) == 1

    providers = []
    assert cli._run_web_licence_search(
        Namespace(headless=True, no_web_search=True), subject, session, set(), providers
    ) == set()
    assert providers == []


def test_engine_without_ota_links_falls_through_to_next_engine():
    subject = _subject(registration_ids=["ET-0123-E"], title=None)
    session = _FakeSession({
        ("bing", '"ET-0123-E" site:airbnb.com'): {
            "hrefs": ["https://community.example.com/thread/1"],
            "text": "unrelated results",
        },
        ("duckduckgo", '"ET-0123-E" site:airbnb.com'): {
            "hrefs": ["https://www.airbnb.com/rooms/4242"],
            "text": "results",
        },
    })
    provider = WebLicenceSearchProvider(session)
    result = provider.discover(subject)
    assert "https://www.airbnb.com/rooms/4242" in [ref.url for ref in result.refs]
    first_two = [(o.engine, o.status) for o in provider.outcomes[:2]]
    assert first_two == [("bing", "no_ota_links"), ("duckduckgo", "hits")]


def test_duckduckgo_duck_challenge_is_recognised_as_a_block():
    subject = _subject(registration_ids=["ET-0123-E"], title=None)
    session = _FakeSession({
        ("duckduckgo", '"ET-0123-E" site:airbnb.com'): {
            "hrefs": ["https://html.duckduckgo.com/html/"],
            "text": "Unfortunately, bots use DuckDuckGo too. Please complete the following "
                    "challenge to confirm this search was made by a human.",
        },
    })
    provider = WebLicenceSearchProvider(session, engines=("duckduckgo", "google"))
    provider.discover(subject)
    assert (provider.outcomes[0].engine, provider.outcomes[0].status) == ("duckduckgo", "challenge")
    assert all(o.engine != "duckduckgo" for o in provider.outcomes[1:])


def test_already_matched_channel_is_not_searched_again():
    subject = _subject(registration_ids=["ET-0123-E"])
    session = _FakeSession({})
    provider = WebLicenceSearchProvider(session, engines=("bing",), skip_sites={"airbnb"})
    provider.discover(subject)
    searched = [outcome.query for outcome in provider.outcomes]
    assert searched
    assert not any("site:airbnb.com" in query for query in searched)
    # The unrestricted licence search still runs for the missing channel.
    assert '"ET-0123-E"' in searched


def test_browser_session_hands_challenge_to_operator_only_when_interactive(monkeypatch):
    from parity.adapters.browser.browser_session import BrowserSession

    class Page:
        def __init__(self, texts):
            self.texts = list(texts)

        def bring_to_front(self):
            pass

        def wait_for_timeout(self, ms):
            pass

        def title(self):
            return ""

        def inner_text(self, selector):
            return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]

    quiet = BrowserSession(headless=False, offline=True, interactive=False)
    assert quiet.wait_for_human(Page(["Are you a robot?"]), "Booking.com") is False

    headless = BrowserSession(headless=True, offline=True, interactive=True)
    assert headless.wait_for_human(Page(["Are you a robot?"]), "Booking.com") is False

    operator = BrowserSession(headless=False, offline=True, interactive=True)
    page = Page(["Are you a robot?", "Are you a robot?", "Villa results"])
    assert operator.wait_for_human(page, "Booking.com") is True

    ready_calls = []
    page = Page(["Search results page"])
    assert operator.wait_for_human(
        page, "Booking.com search", ready=lambda p: ready_calls.append(1) or len(ready_calls) >= 2
    ) is True
