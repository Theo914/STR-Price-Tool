from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from parity.http_control import (
    FixtureCorruptError,
    FixtureMissingError,
    FixtureStaleError,
    GovernedHttpClient,
    HttpFixtureStore,
    RequestDeferred,
    RequestClass,
    RequestGovernor,
    classify_request,
    parse_retry_after,
)
from parity.storage import EvidenceStore


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_expedia_group_images_use_the_shared_bounded_image_class():
    request_class = classify_request(
        "https://images.trvl-media.com/lodging/1000000/100.jpg"
    )
    assert request_class == RequestClass("expedia_group", "image")
    vrbo_class = classify_request(
        "https://media.vrbo.com/lodging/1000000/100.jpg"
    )
    assert vrbo_class == RequestClass("expedia_group", "image")


def test_travelstaytion_cdn_images_use_the_bounded_image_class():
    request_class = classify_request(
        "https://cdn.travelstaytion.com/file/property/319567/photo.jpg"
    )
    assert request_class == RequestClass("travelstaytion", "image")


def test_recorded_fixture_replays_offline_without_network_and_redacts_tokens(tmp_path):
    calls = 0

    def live_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Set-Cookie": "session=must-not-be-stored",
            },
            text=(
                '<meta name="csrf-token" content="secret-csrf">'
                '<script>{"accessToken":"secret-access"}</script>'
                '<h1>Villa Sa Planeta</h1>'
            ),
        )

    fixture_dir = tmp_path / "fixtures"
    with EvidenceStore(tmp_path / "telemetry.db") as store:
        governor = RequestGovernor(store)
        client = GovernedHttpClient(
            _client(live_handler),
            HttpFixtureStore(fixture_dir),
            governor,
            record_fixtures=True,
        )
        response = client.get("https://www.airbnb.com/rooms/123", params={"adults": 2})
        client.close()

    assert response.status_code == 200
    assert calls == 1
    manifest = json.loads(next(fixture_dir.glob("*.json")).read_text(encoding="utf-8"))
    saved_body = next(fixture_dir.glob("*.body")).read_text(encoding="utf-8")
    assert "set-cookie" not in manifest["headers"]
    assert "secret-csrf" not in saved_body
    assert "secret-access" not in saved_body
    assert saved_body.count("[REDACTED]") == 2

    def forbidden_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("offline replay attempted a network request")

    with EvidenceStore(tmp_path / "offline-telemetry.db") as store:
        governor = RequestGovernor(store)
        client = GovernedHttpClient(
            _client(forbidden_handler),
            HttpFixtureStore(fixture_dir),
            governor,
            offline=True,
        )
        replay = client.get("https://www.airbnb.com/rooms/123", params={"adults": 2})
        client.close()
        summary = governor.summary_lines()

    assert replay.status_code == 200
    assert "Villa Sa Planeta" in replay.text
    assert calls == 1
    assert any("network requests: 0/3, fixture hits: 1" in line for line in summary)


def test_offline_missing_fixture_fails_closed_without_network(tmp_path):
    def forbidden_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("missing fixture attempted a network request")

    with EvidenceStore(tmp_path / "telemetry.db") as store:
        governor = RequestGovernor(store)
        client = GovernedHttpClient(
            _client(forbidden_handler),
            HttpFixtureStore(tmp_path / "fixtures"),
            governor,
            offline=True,
        )
        with pytest.raises(FixtureMissingError, match="OFFLINE: no saved response"):
            client.get("https://www.airbnb.com/rooms/404")
        client.close()

    assert not governor.network_counts


def test_stale_and_corrupt_fixtures_are_rejected(tmp_path):
    fixture_store = HttpFixtureStore(tmp_path / "fixtures")
    request = httpx.Request("GET", "https://www.airbnb.com/rooms/123")
    response = httpx.Response(200, text="<h1>Villa</h1>", request=request)
    fixture_store.save(request, response)

    manifest_path = next((tmp_path / "fixtures").glob("*.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FixtureStaleError, match="expired"):
        fixture_store.load(request)

    manifest["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    next((tmp_path / "fixtures").glob("*.body")).write_bytes(b"tampered")
    with pytest.raises(FixtureCorruptError, match="checksum"):
        fixture_store.load(request)


def test_page_budget_stops_before_extra_network_request(tmp_path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="ok")

    with EvidenceStore(tmp_path / "telemetry.db") as store:
        governor = RequestGovernor(store, budgets={("airbnb", "page"): 1})
        client = GovernedHttpClient(
            _client(handler),
            HttpFixtureStore(tmp_path / "fixtures"),
            governor,
        )
        assert client.get("https://www.airbnb.com/rooms/1").status_code == 200
        with pytest.raises(RequestDeferred, match=r"budget exhausted \(1/1\)"):
            client.get("https://www.airbnb.com/rooms/2")
        client.close()

    assert calls == 1


def test_airbnb_pricing_has_an_independent_one_request_budget(tmp_path):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="ok")

    with EvidenceStore(tmp_path / "telemetry.db") as store:
        governor = RequestGovernor(store)
        client = GovernedHttpClient(
            _client(handler),
            HttpFixtureStore(tmp_path / "fixtures"),
            governor,
        )
        assert client.post_pricing("https://www.airbnb.com/api/v3/quote/1", json={}).status_code == 200
        with pytest.raises(RequestDeferred, match=r"pricing request budget exhausted \(1/1\)"):
            client.post_pricing("https://www.airbnb.com/api/v3/quote/2", json={})
        client.close()

    assert calls == 1
    assert governor.network_counts[("airbnb", "pricing")] == 1
    assert governor.network_counts[("airbnb", "page")] == 0


def test_booking_pricing_browser_navigation_has_its_own_one_request_budget(tmp_path):
    with EvidenceStore(tmp_path / "telemetry.db") as store:
        governor = RequestGovernor(store)
        request_class = RequestClass("booking", "pricing")

        governor.before_network_request(request_class)
        with pytest.raises(RequestDeferred, match=r"pricing request budget exhausted \(1/1\)"):
            governor.before_network_request(request_class)

    assert governor.network_counts[("booking", "pricing")] == 1
    assert governor.network_counts[("booking", "page")] == 0


def test_429_opens_persistent_circuit_and_is_not_recorded_as_fixture(tmp_path):
    calls = 0
    db_path = tmp_path / "telemetry.db"
    fixture_dir = tmp_path / "fixtures"

    def rate_limited(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "120"}, text="slow down")

    with EvidenceStore(db_path) as store:
        governor = RequestGovernor(store)
        client = GovernedHttpClient(
            _client(rate_limited),
            HttpFixtureStore(fixture_dir),
            governor,
            record_fixtures=True,
        )
        assert client.get("https://www.airbnb.com/rooms/1").status_code == 429
        client.close()
        status = store.circuit_status("airbnb")
        assert status is not None
        assert 0 < status.remaining_seconds() <= 120

    assert calls == 1
    assert not fixture_dir.exists() or not list(fixture_dir.iterdir())

    def forbidden_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("persistent circuit allowed a network request")

    with EvidenceStore(db_path) as store:
        governor = RequestGovernor(store)
        client = GovernedHttpClient(
            _client(forbidden_handler),
            HttpFixtureStore(fixture_dir),
            governor,
        )
        with pytest.raises(RequestDeferred, match="cooldown remaining"):
            client.get("https://www.airbnb.com/rooms/2")
        client.close()


def test_airbnb_circuit_does_not_stop_travelstaytion(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, text="ok")

    with EvidenceStore(tmp_path / "telemetry.db") as store:
        store.open_circuit("airbnb", "test cooldown", seconds=60)
        governor = RequestGovernor(store)
        client = GovernedHttpClient(
            _client(handler),
            HttpFixtureStore(tmp_path / "fixtures"),
            governor,
        )
        with pytest.raises(RequestDeferred):
            client.get("https://www.airbnb.com/rooms/1")
        assert client.get("https://www.travelstaytion.com/stay/742313").status_code == 200
        client.close()

    assert calls == ["www.travelstaytion.com"]


def test_challenge_page_opens_circuit_and_is_not_saved(tmp_path):
    fixture_dir = tmp_path / "fixtures"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<html><body>Verify you are a human</body></html>",
        )

    with EvidenceStore(tmp_path / "telemetry.db") as store:
        governor = RequestGovernor(store)
        client = GovernedHttpClient(
            _client(handler),
            HttpFixtureStore(fixture_dir),
            governor,
            record_fixtures=True,
        )
        assert client.get("https://www.airbnb.com/rooms/1").status_code == 200
        client.close()
        assert store.circuit_status("airbnb") is not None

    assert not fixture_dir.exists() or not list(fixture_dir.iterdir())


def test_retry_after_supports_seconds_and_http_dates():
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    assert parse_retry_after("90", now=now) == 90
    assert parse_retry_after("Wed, 23 Sep 2026 12:02:00 GMT", now=now) == 120
    assert parse_retry_after("nonsense", now=now) is None


def test_record_mode_does_not_persist_gallery_bytes(tmp_path):
    fixture_dir = tmp_path / "fixtures"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=b"jpeg")

    with EvidenceStore(tmp_path / "telemetry.db") as store:
        client = GovernedHttpClient(
            _client(handler),
            HttpFixtureStore(fixture_dir),
            RequestGovernor(store),
            record_fixtures=True,
        )
        assert client.get("https://a0.muscache.com/im/pictures/test.jpeg").status_code == 200
        client.close()

    assert not fixture_dir.exists() or not list(fixture_dir.iterdir())


def test_booking_browser_challenge_opens_persistent_booking_circuit(tmp_path):
    with EvidenceStore(tmp_path / "telemetry.db") as store:
        governor = RequestGovernor(store, budgets={("booking", "page"): 1})
        request_class = RequestClass("booking", "page")
        governor.before_network_request(request_class)
        governor.record_browser_navigation(
            request_class,
            latency_ms=50,
            outcome="browser_challenge",
            challenge=True,
        )

        assert store.circuit_status("booking") is not None
        with pytest.raises(RequestDeferred, match="booking cooldown remaining"):
            governor.before_network_request(request_class)


def test_bstatic_gallery_is_classified_as_booking_image():
    classified = classify_request(
        "https://cf.bstatic.com/xdata/images/hotel/max1024x768/123.jpg?k=public"
    )
    assert classified == RequestClass("booking", "image")
