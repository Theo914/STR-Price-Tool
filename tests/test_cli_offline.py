from __future__ import annotations

import json

import httpx

from parity.cli import main
from parity.core.matching import PHash
from parity.http_control import HttpFixtureStore
from parity.storage import EvidenceStore


SUBJECT_URL = "https://www.travelstaytion.com/stay/742313"
CANDIDATE_URL = "https://www.airbnb.com.au/rooms/1099791623724969319"


def _save_get_fixture(store: HttpFixtureStore, url: str, body: str, params=None) -> None:
    request = httpx.Request("GET", url, params=params)
    response = httpx.Response(
        200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        text=body,
        request=request,
    )
    store.save(request, response)


def _run_offline_verify(tmp_path, capsys) -> tuple[int, dict, str]:
    output = tmp_path / "identity.json"
    exit_code = main([
        "verify",
        SUBJECT_URL,
        "--against",
        CANDIDATE_URL,
        "--offline",
        "--fixtures-dir",
        str(tmp_path / "fixtures"),
        "--governor-db",
        str(tmp_path / "governor.db"),
        "--hash-cache",
        str(tmp_path / "hashes.db"),
        "--db",
        str(tmp_path / "parity.db"),
        "--format",
        "json",
        "--output",
        str(output),
    ])
    captured = capsys.readouterr()
    return exit_code, json.loads(output.read_text(encoding="utf-8")), captured.err


def test_cli_offline_positive_uses_page_fixtures_and_cached_hashes_only(tmp_path, capsys):
    fixture_store = HttpFixtureStore(tmp_path / "fixtures")
    subject_images = [
        "https://images.example/subject-one.jpg",
        "https://images.example/subject-two.jpg",
    ]
    candidate_images = [
        "https://a0.muscache.com/im/pictures/prohost-api/Hosting-1099791623724969319/original/one.jpeg?im_w=720",
        "https://a0.muscache.com/im/pictures/prohost-api/Hosting-1099791623724969319/original/two.jpeg?im_w=720",
    ]
    subject_payload = {
        "id": 742313,
        "title": "Villa Sa Planeta",
        "capacity": 6,
        "bedrooms": 3,
        "bathrooms": 2,
        "gallery": [{"url": url} for url in subject_images],
    }
    _save_get_fixture(
        fixture_store,
        SUBJECT_URL,
        f"<script>window.property = {json.dumps(subject_payload)};</script>",
    )
    candidate_html = """
    <html><body>
      <h1>Villa Sa Planeta</h1>
      <div>Entire villa in Ibiza</div>
      <div>6 guests · 3 bedrooms · 2 bathrooms</div>
      <img src="https://a0.muscache.com/im/pictures/prohost-api/Hosting-1099791623724969319/original/one.jpeg?im_w=720">
      <img src="https://a0.muscache.com/im/pictures/prohost-api/Hosting-1099791623724969319/original/two.jpeg?im_w=720">
    </body></html>
    """
    _save_get_fixture(
        fixture_store,
        CANDIDATE_URL,
        candidate_html,
        params={
            "check_in": "2026-11-01",
            "check_out": "2026-11-04",
            "adults": 2,
            "currency": "EUR",
        },
    )
    with EvidenceStore(tmp_path / "hashes.db") as cache:
        cache.save_photo_hash(subject_images[0], PHash(full=0, crop=0))
        cache.save_photo_hash(candidate_images[0], PHash(full=0, crop=0))
        cache.save_photo_hash(subject_images[1], PHash(full=(1 << 64) - 1, crop=(1 << 64) - 1))
        cache.save_photo_hash(candidate_images[1], PHash(full=(1 << 64) - 1, crop=(1 << 64) - 1))

    exit_code, report, stderr = _run_offline_verify(tmp_path, capsys)

    assert exit_code == 0
    assert report["matches"][0]["decision"] == "verified"
    assert "network requests: 0/3, fixture hits: 1" in stderr
    assert "cached" in stderr


def test_cli_offline_hard_negative_rejects_before_image_access(tmp_path, capsys):
    fixture_store = HttpFixtureStore(tmp_path / "fixtures")
    subject_payload = {
        "id": 742313,
        "title": "Villa Sa Planeta",
        "capacity": 6,
        "bedrooms": 3,
        "bathrooms": 2,
        "gallery": [{"url": "https://images.example/subject.jpg"}],
    }
    _save_get_fixture(
        fixture_store,
        SUBJECT_URL,
        f"<script>window.property = {json.dumps(subject_payload)};</script>",
    )
    candidate_html = """
    <html><body>
      <h1>Nearby Wrong Villa</h1>
      <div>Entire villa in Ibiza</div>
      <div>8 guests · 4 bedrooms · 2 bathrooms</div>
      <img src="https://a0.muscache.com/im/pictures/prohost-api/Hosting-1099791623724969319/original/wrong.jpeg?im_w=720">
    </body></html>
    """
    _save_get_fixture(
        fixture_store,
        CANDIDATE_URL,
        candidate_html,
        params={
            "check_in": "2026-11-01",
            "check_out": "2026-11-04",
            "adults": 2,
            "currency": "EUR",
        },
    )

    exit_code, report, stderr = _run_offline_verify(tmp_path, capsys)

    assert exit_code == 0
    assert report["matches"][0]["decision"] == "rejected"
    assert "bedrooms conflict" in report["matches"][0]["evidence"][0]["detail"]
    assert "capacity conflict" in report["matches"][0]["evidence"][0]["detail"]
    assert "skipping photo downloads" in stderr
    assert "images.example" not in stderr
