from __future__ import annotations

import json

import httpx

from parity.browser_control import BrowserSnapshotStore
from parity.cli import main
from parity.http_control import HttpFixtureStore


SUBJECT_URL = "https://www.travelstaytion.com/stay/210890"
MARRIOTT_URL = "https://homes-and-villas.marriott.com/en/properties/78114516"
LICENCE = "15063044EXT0379"


def test_marriott_exact_registration_replays_offline_without_browser(tmp_path, capsys):
    subject_payload = {
        "id": 210890,
        "title": "Casa Tatano",
        "licenseNumber": LICENCE,
        "bedrooms": 3,
        "bathrooms": 2,
        "capacity": 6,
        "gallery": [],
    }
    request = httpx.Request("GET", SUBJECT_URL)
    response = httpx.Response(
        200,
        text=f"<script>window.property = {json.dumps(subject_payload)};</script>",
        request=request,
    )
    fixture_dir = tmp_path / "fixtures"
    HttpFixtureStore(fixture_dir).save(request, response)
    BrowserSnapshotStore(fixture_dir).save(
        "marriott",
        "page",
        MARRIOTT_URL,
        {
            "snapshot_version": 1,
            "platform": "marriott",
            "canonical_url": MARRIOTT_URL,
            "raw_id": "78114516",
            "title": "Captivating Idyllic House with a Stunning Sea View",
            "registration_ids": [LICENCE],
            "lat": 40.61,
            "lng": 14.34,
            "country": "Italy",
            "city": "Massa Lubrense",
            "postcode": "80061",
            "bedrooms": 3,
            "bathrooms": 2,
            "capacity": 6,
            "property_type": "VacationRental",
            "gallery": [],
            "identity_source": "rendered_public_marriott_property_page",
        },
    )

    output = tmp_path / "identity-marriott.json"
    exit_code = main([
        "verify",
        SUBJECT_URL,
        "--against",
        MARRIOTT_URL + "?tracking=must-not-be-saved",
        "--offline",
        "--fixtures-dir",
        str(fixture_dir),
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
    assert report["matches"][0]["ref"]["url"] == MARRIOTT_URL
    assert report["matches"][0]["evidence"][0]["kind"] == "registration_exact"
    assert "exact registration identifier 15063044EXT0379" in stderr
    assert "[marriott] page network requests: 0/1, fixture hits: 1" in stderr
    assert "launching signed browser" not in stderr
    assert "hashing photo" not in stderr
