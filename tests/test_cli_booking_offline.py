from __future__ import annotations

import json

import httpx

from parity.browser_control import BrowserSnapshotStore
from parity.cli import main
from parity.core.matching import PHash
from parity.http_control import HttpFixtureStore
from parity.storage import EvidenceStore


SUBJECT_URL = "https://www.travelstaytion.com/stay/742313"
BOOKING_URL = "https://www.booking.com/hotel/es/villa-sa-planeta.en-gb.html"


def test_booking_identity_positive_replays_offline_with_cached_hashes(tmp_path, capsys):
    subject_images = [
        "https://images.example/subject-one.jpg",
        "https://images.example/subject-two.jpg",
    ]
    booking_images = [
        "https://cf.bstatic.com/xdata/images/hotel/max1024x768/one.jpg",
        "https://cf.bstatic.com/xdata/images/hotel/max1024x768/two.jpg",
    ]
    payload = {
        "id": 742313,
        "title": "Villa Sa Planeta",
        "capacity": 6,
        "bedrooms": 3,
        "bathrooms": 2,
        "gallery": [{"url": url} for url in subject_images],
    }
    request = httpx.Request("GET", SUBJECT_URL)
    response = httpx.Response(
        200,
        text=f"<script>window.property = {json.dumps(payload)};</script>",
        request=request,
    )
    HttpFixtureStore(tmp_path / "fixtures").save(request, response)
    BrowserSnapshotStore(tmp_path / "fixtures").save(
        "booking",
        "page",
        BOOKING_URL,
        {
            "snapshot_version": 1,
            "canonical_url": BOOKING_URL,
            "raw_id": "villa-sa-planeta",
            "title": "Villa Sa Planeta",
            "registration_ids": [],
            "lat": 38.9,
            "lng": 1.3,
            "country": "Spain",
            "city": "Sant Josep de sa Talaia",
            "postcode": "07830",
            "bedrooms": 3,
            "bathrooms": 2,
            "capacity": 6,
            "property_type": "Villa",
            "amenities": ["Pool"],
            "gallery": booking_images,
            "identity_source": "rendered_public_booking_page",
        },
    )
    with EvidenceStore(tmp_path / "hashes.db") as cache:
        cache.save_photo_hash(subject_images[0], PHash(full=0, crop=0))
        cache.save_photo_hash(booking_images[0], PHash(full=0, crop=0))
        cache.save_photo_hash(subject_images[1], PHash(full=(1 << 64) - 1, crop=(1 << 64) - 1))
        cache.save_photo_hash(booking_images[1], PHash(full=(1 << 64) - 1, crop=(1 << 64) - 1))

    output = tmp_path / "identity.json"
    exit_code = main([
        "verify",
        SUBJECT_URL,
        "--against",
        BOOKING_URL + "?aid=356980&sid=must-not-be-saved",
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
    stderr = capsys.readouterr().err
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["matches"][0]["decision"] == "verified"
    assert report["matches"][0]["ref"]["url"] == BOOKING_URL
    assert "[booking] page network requests: 0/1, fixture hits: 1" in stderr
    assert "cached" in stderr


def test_booking_hard_negative_rejects_offline_before_any_photo_access(tmp_path, capsys):
    subject_payload = {
        "id": 742313,
        "title": "Villa Sa Planeta",
        "capacity": 6,
        "bedrooms": 3,
        "bathrooms": 2,
        "gallery": [{"url": "https://images.example/subject.jpg"}],
    }
    request = httpx.Request("GET", SUBJECT_URL)
    response = httpx.Response(
        200,
        text=f"<script>window.property = {json.dumps(subject_payload)};</script>",
        request=request,
    )
    HttpFixtureStore(tmp_path / "fixtures").save(request, response)
    BrowserSnapshotStore(tmp_path / "fixtures").save(
        "booking",
        "page",
        BOOKING_URL,
        {
            "snapshot_version": 1,
            "canonical_url": BOOKING_URL,
            "raw_id": "wrong-villa",
            "title": "Nearby Wrong Villa",
            "registration_ids": [],
            "lat": 38.9,
            "lng": 1.3,
            "country": "Spain",
            "city": "Sant Josep de sa Talaia",
            "postcode": "07830",
            "bedrooms": 4,
            "bathrooms": 2,
            "capacity": 8,
            "property_type": "Villa",
            "amenities": [],
            "gallery": [
                "https://cf.bstatic.com/xdata/images/hotel/max1024x768/wrong-1.jpg",
                "https://cf.bstatic.com/xdata/images/hotel/max1024x768/wrong-2.jpg",
            ],
            "identity_source": "rendered_public_booking_page",
        },
    )

    output = tmp_path / "identity-negative.json"
    exit_code = main([
        "verify",
        SUBJECT_URL,
        "--against",
        BOOKING_URL,
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
    stderr = capsys.readouterr().err
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["matches"][0]["decision"] == "rejected"
    assert "bedrooms conflict" in report["matches"][0]["evidence"][0]["detail"]
    assert "capacity conflict" in report["matches"][0]["evidence"][0]["detail"]
    assert "skipping photo downloads" in stderr
    assert "hashing photo" not in stderr
