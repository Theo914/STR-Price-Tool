from __future__ import annotations

import json
import shutil
from datetime import date

import pytest

from parity.cli import main
from parity.core.date_policy import DateWindow, MultiChannelMinimumStaySelection
from parity.core.models import Outcome, PriceResult, PropertyRef
from parity.portfolio import expand_portfolio_jobs, load_portfolio_config
from tests.test_cli_quote import (
    BOOKING_URL,
    CANDIDATE_URL,
    SUBJECT_URL,
    _save_booking_mapping,
    _save_booking_quote_snapshot,
    _save_mapping,
    _save_quote_fixture,
    _save_travelstaytion_quote_fixture,
)


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def test_portfolio_rejects_more_than_ten_listings(tmp_path):
    config_path = tmp_path / "portfolio.json"
    _write_json(config_path, {
        "host": "Too Large",
        "listings": [{} for _ in range(11)],
    })

    with pytest.raises(ValueError, match="maximum is 10"):
        load_portfolio_config(config_path)


def test_portfolio_expands_one_shared_automatic_listing_to_both_channels(tmp_path):
    config_path = tmp_path / "portfolio.json"
    _write_json(config_path, {
        "host": "Unsafe Dates",
        "listings": [{
            "name": "One",
            "subject": SUBJECT_URL,
            "currency": "EUR",
            "auto_minimum_stay": True,
            "channels": [
                {
                    "channel": "airbnb",
                    "candidate_url": CANDIDATE_URL,
                    "db": "airbnb.db",
                },
                {
                    "channel": "booking",
                    "candidate_url": BOOKING_URL,
                    "db": "booking.db",
                },
            ],
        }],
    })

    config = load_portfolio_config(config_path)
    listings, jobs = expand_portfolio_jobs(config, config_path=config_path)

    assert len(listings) == 1
    assert listings[0]["auto_minimum_stay"] is True
    assert [job["channel"] for job in jobs] == ["airbnb", "booking"]
    assert all(job["checkin"] is None and job["checkout"] is None for job in jobs)


def test_portfolio_offline_runs_two_listings_and_resumes_without_fixtures(
    tmp_path,
    capsys,
):
    airbnb_db = tmp_path / "airbnb.db"
    booking_db = tmp_path / "booking.db"
    airbnb_fixtures = tmp_path / "airbnb-fixtures"
    booking_fixtures = tmp_path / "booking-fixtures"
    _save_mapping(airbnb_db)
    _save_booking_mapping(booking_db)
    _save_quote_fixture(airbnb_fixtures)
    _save_travelstaytion_quote_fixture(airbnb_fixtures)
    _save_booking_quote_snapshot(booking_fixtures)
    _save_travelstaytion_quote_fixture(
        booking_fixtures,
        checkin="2027-02-01",
        checkout="2027-02-09",
        displayed_total=2200.0,
    )

    config_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "results.json"
    work_dir = tmp_path / "work"
    _write_json(config_path, {
        "host": "Proof Host",
        "listings": [
            {
                "name": "Airbnb proof",
                "subject": SUBJECT_URL,
                "currency": "EUR",
                "checkin": "2026-10-04",
                "checkout": "2026-10-11",
                "channels": [{
                    "channel": "airbnb",
                    "candidate_url": CANDIDATE_URL,
                    "db": airbnb_db.name,
                    "fixtures_dir": airbnb_fixtures.name,
                }],
            },
            {
                "name": "Booking proof",
                "subject": SUBJECT_URL,
                "currency": "EUR",
                "checkin": "2027-02-01",
                "checkout": "2027-02-09",
                "channels": [{
                    "channel": "booking",
                    "candidate_url": BOOKING_URL,
                    "db": booking_db.name,
                    "fixtures_dir": booking_fixtures.name,
                }],
            },
        ],
    })

    command = [
        "portfolio",
        str(config_path),
        "--offline",
        "--work-dir",
        str(work_dir),
        "--output",
        str(output_path),
    ]
    assert main(command) == 0
    first_stderr = capsys.readouterr().err
    manifest = json.loads(output_path.read_text(encoding="utf-8"))

    assert manifest["host"] == "Proof Host"
    assert manifest["listing_count"] == 2
    assert manifest["comparison_count"] == 2
    assert manifest["status_counts"] == {"PRICED": 2}
    assert [result["channel"] for result in manifest["results"]] == [
        "airbnb",
        "booking",
    ]
    assert [result["ota_guest_total"] for result in manifest["results"]] == [
        760.0,
        2150.0,
    ]
    assert [result["travelstaytion_guest_total"] for result in manifest["results"]] == [
        800.0,
        2200.0,
    ]
    assert all(result["resumed"] is False for result in manifest["results"])
    assert output_path.with_suffix(".csv").is_file()
    assert "PORTFOLIO: 2 listing(s), 2 comparison(s); PRICED=2" in first_stderr

    airbnb_quote = work_dir / "quotes" / "01-airbnb-proof-airbnb.json"
    airbnb_quote.write_text(
        airbnb_quote.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    assert main(command) == 0
    tamper_stderr = capsys.readouterr().err
    tamper_run = json.loads(output_path.read_text(encoding="utf-8"))
    assert [result["resumed"] for result in tamper_run["results"]] == [False, True]
    assert tamper_stderr.count("portfolio resume hit") == 1

    shutil.rmtree(airbnb_fixtures)
    shutil.rmtree(booking_fixtures)
    assert main(command) == 0
    second_stderr = capsys.readouterr().err
    resumed = json.loads(output_path.read_text(encoding="utf-8"))
    assert all(result["resumed"] is True for result in resumed["results"])
    assert second_stderr.count("portfolio resume hit") == 2
    assert "pricing network requests" not in second_stderr


def test_portfolio_records_identity_failure_without_dropping_the_row(tmp_path):
    config_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "results.json"
    _write_json(config_path, {
        "host": "Missing Mapping",
        "listings": [{
            "name": "Unknown",
            "subject": SUBJECT_URL,
            "currency": "EUR",
            "checkin": "2027-02-01",
            "checkout": "2027-02-08",
            "channels": [{
                "channel": "airbnb",
                "candidate_url": CANDIDATE_URL,
                "db": "empty.db",
                "fixtures_dir": "missing-fixtures",
            }],
        }],
    })

    exit_code = main([
        "portfolio",
        str(config_path),
        "--offline",
        "--work-dir",
        str(tmp_path / "work"),
        "--output",
        str(output_path),
    ])
    manifest = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert manifest["status_counts"] == {"IDENTITY_FAILED": 1}
    assert manifest["results"][0]["status"] == "IDENTITY_FAILED"
    assert "no unexpired verified airbnb mapping" in manifest["results"][0]["error"]


def test_portfolio_shared_selector_writes_two_same_stay_quotes_and_resumes(
    tmp_path,
    capsys,
    monkeypatch,
):
    subject_url = "https://www.travelstaytion.com/stay/1018084"
    airbnb_url = "https://www.airbnb.co.uk/rooms/1676081196282726495"
    booking_url = "https://www.booking.com/hotel/au/quiet-amp-comfortable-a-sepal-stay.en-gb.html"
    checkin = date(2027, 2, 15)
    checkout = date(2027, 2, 20)
    subject = PropertyRef(
        site="travelstaytion", raw_id="1018084", url=subject_url
    )
    airbnb = PropertyRef(
        site="airbnb", raw_id="1676081196282726495", url=airbnb_url
    )
    booking = PropertyRef(
        site="booking", raw_id="quiet-amp-comfortable-a-sepal-stay", url=booking_url
    )

    def priced(ref, total):
        return PriceResult(
            site=ref.site,
            ref=ref,
            outcome=Outcome.PRICED,
            checkin=checkin,
            checkout=checkout,
            guests=2,
            currency="AUD",
            comparable_total=total,
            headline_total=total,
        )

    calls = []

    def fake_selector(listing, jobs, args, work_dir):
        calls.append(listing["listing_id"])
        return {
            "selection": MultiChannelMinimumStaySelection(
                window=DateWindow(checkin, checkout),
                selected_index=1,
                minimum_stay_nights=5,
                subject_minimum_stay_nights=2,
                candidate_minimum_stay_nights={"airbnb": 3, "booking": 5},
                probes=(),
                subject_price=priced(subject, 1000.0),
                candidate_prices={
                    "airbnb": priced(airbnb, 900.0),
                    "booking": priced(booking, 1100.0),
                },
            ),
            "subject": subject,
            "candidates": {"airbnb": airbnb, "booking": booking},
            "fixtures_dir": str(tmp_path / "shared-fixtures"),
        }

    monkeypatch.setattr("parity.cli._select_portfolio_shared_minimum", fake_selector)
    config_path = tmp_path / "portfolio.json"
    output_path = tmp_path / "results.json"
    work_dir = tmp_path / "work"
    _write_json(config_path, {
        "host": "Shared proof",
        "listings": [{
            "name": "A Sepal Stay",
            "subject": subject_url,
            "currency": "AUD",
            "auto_minimum_stay": True,
            "channels": [
                {"channel": "airbnb", "candidate_url": airbnb_url, "db": "proof.db"},
                {"channel": "booking", "candidate_url": booking_url, "db": "proof.db"},
            ],
        }],
    })
    command = [
        "portfolio", str(config_path),
        "--work-dir", str(work_dir),
        "--output", str(output_path),
    ]

    assert main(command) == 0
    first = json.loads(output_path.read_text(encoding="utf-8"))
    assert first["status_counts"] == {"PRICED": 2}
    assert {row["channel"] for row in first["results"]} == {"airbnb", "booking"}
    assert {row["checkin"] for row in first["results"]} == {"2027-02-15"}
    assert {row["checkout"] for row in first["results"]} == {"2027-02-20"}
    assert {row["nights"] for row in first["results"]} == {5}
    assert calls == ["01-a-sepal-stay"]
    capsys.readouterr()

    assert main(command) == 0
    second_stderr = capsys.readouterr().err
    second = json.loads(output_path.read_text(encoding="utf-8"))
    assert all(row["resumed"] is True for row in second["results"])
    assert second_stderr.count("portfolio resume hit") == 2
    assert calls == ["01-a-sepal-stay"]
