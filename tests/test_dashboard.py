from __future__ import annotations

import json

import pytest

from parity.cli import main
from parity.dashboard import load_dashboard_data, render_dashboard


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def _quote(channel: str, candidate_total: float, subject_total: float) -> dict:
    candidate_url = (
        "https://www.booking.com/hotel/au/example.en-gb.html"
        if channel == "booking"
        else "https://www.airbnb.co.uk/rooms/123"
    )
    difference = round(candidate_total - subject_total, 2)
    percent = round(difference / subject_total * 100, 2)
    scenarios = []
    if channel == "booking":
        scenarios = [{
            "site": "booking",
            "discount_percent": 10.0,
            "status": "available",
            "adjusted_guest_total": 900.0,
            "adjusted_candidate_minus_subject": -100.0,
            "adjusted_difference_percent": -10.0,
            "additional_discount_amount": 100.0,
            "percentage_tax_reduction": 0.0,
            "note": "Hypothetical only",
        }]
    return {
        "subject": {
            "url": "https://www.travelstaytion.com/stay/1",
        },
        "candidate": {"url": candidate_url},
        "subject_price": {
            "outcome": "priced",
            "headline_total": subject_total,
            "comparable_total": subject_total,
            "breakdown_status": "exact",
            "line_items": [{
                "label": "3 nights accommodation",
                "amount": subject_total,
                "category": "accommodation",
                "included_in_displayed_total": True,
            }],
        },
        "price": {
            "outcome": "priced",
            "headline_total": candidate_total,
            "comparable_total": candidate_total - 50.0,
            "breakdown_status": "exact",
            "line_items": [
                {
                    "label": "3 nights accommodation",
                    "amount": candidate_total - 50.0,
                    "category": "accommodation",
                    "included_in_displayed_total": True,
                },
                {
                    "label": "Cleaning fee",
                    "amount": 50.0,
                    "category": "cleaning",
                    "included_in_displayed_total": True,
                },
            ],
        },
        "comparison": {
            "candidate_minus_subject": difference,
            "candidate_difference_percent": percent,
            "verdict": "candidate_more_expensive",
            "refundability_note": "Terms differ",
        },
        "discount_scenarios": scenarios,
    }


def _manifest(tmp_path, *, listing_count=1):
    quotes = tmp_path / "quotes"
    booking_quote = quotes / "one-booking.json"
    airbnb_quote = quotes / "one-airbnb.json"
    _write(booking_quote, _quote("booking", 1100.0, 1000.0))
    _write(airbnb_quote, _quote("airbnb", 1050.0, 1000.0))
    listings = []
    results = []
    for index in range(listing_count):
        listing_id = f"{index + 1:02d}-one"
        listings.append({
            "listing_id": listing_id,
            "name": f"One {index + 1}",
            "subject": "https://www.travelstaytion.com/stay/1",
            "currency": "AUD",
            "guests": 2,
            "checkin": "2027-02-15",
            "checkout": "2027-02-18",
            "channels": [],
        })
        for channel, path, total, difference in (
            ("booking", booking_quote, 1100.0, 100.0),
            ("airbnb", airbnb_quote, 1050.0, 50.0),
        ):
            results.append({
                "listing_id": listing_id,
                "channel": channel,
                "status": "PRICED",
                "candidate_url": _quote(channel, total, 1000.0)["candidate"]["url"],
                "checkin": "2027-02-15",
                "checkout": "2027-02-18",
                "nights": 3,
                "guests": 2,
                "currency": "AUD",
                "travelstaytion_guest_total": 1000.0,
                "ota_guest_total": total,
                "difference": difference,
                "difference_percent": difference / 10,
                "verdict": "candidate_more_expensive",
                "quote_output": str(path),
                "error": None,
            })
    path = tmp_path / "portfolio-results.json"
    _write(path, {"host": "Proof Host", "listings": listings, "results": results})
    return path


def test_dashboard_loads_guest_totals_breakdowns_links_and_scenarios(tmp_path):
    data = load_dashboard_data(_manifest(tmp_path))

    assert data["host"] == "Proof Host"
    assert len(data["listings"]) == 1
    listing = data["listings"][0]
    assert listing["subject"]["price"]["guest_total"] == 1000.0
    assert listing["subject"]["price"]["line_items"][0]["label"] == (
        "3 nights accommodation"
    )
    assert [item["channel"] for item in listing["channels"]] == [
        "booking",
        "airbnb",
    ]
    booking = listing["channels"][0]
    assert booking["price"]["guest_total"] == 1100.0
    assert booking["price"]["line_items"][1]["label"] == "Cleaning fee"
    assert booking["discount_scenarios"][0]["adjusted_guest_total"] == 900.0
    assert booking["url"].startswith("https://www.booking.com/")


def test_dashboard_does_not_invent_breakdown_without_quote_report(tmp_path):
    manifest_path = _manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["results"][0]["quote_output"] = str(tmp_path / "missing.json")
    _write(manifest_path, manifest)

    data = load_dashboard_data(manifest_path)
    booking = data["listings"][0]["channels"][0]

    assert booking["price"]["guest_total"] == 1100.0
    assert booking["price"]["line_items"] == []
    assert booking["price"]["breakdown_status"] is None
    assert booking["discount_scenarios"] == []


def test_dashboard_keeps_safe_candidate_links_for_manual_review(tmp_path):
    manifest_path = _manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["results"][0]["review_candidates"] = [
        {
            "url": "https://www.booking.com/hotel/au/likely.html",
            "score": 72,
            "identity_status": "probable_review_required",
        },
        {"url": "javascript:alert(1)", "score": 99},
    ]
    _write(manifest_path, manifest)

    data = load_dashboard_data(manifest_path)

    assert data["listings"][0]["channels"][0]["review_candidates"] == [{
        "url": "https://www.booking.com/hotel/au/likely.html",
        "title": None,
        "score": 72.0,
        "identity_status": "probable_review_required",
    }]


def test_dashboard_rejects_more_than_ten_listings(tmp_path):
    with pytest.raises(ValueError, match="maximum is 10"):
        load_dashboard_data(_manifest(tmp_path, listing_count=11))


def test_dashboard_html_is_self_contained_and_script_safe(tmp_path):
    data = load_dashboard_data(_manifest(tmp_path))
    data["host"] = "</script><script>alert(1)</script>"

    rendered = render_dashboard(data)

    assert "https://" not in rendered.split("<style>", 1)[0]
    assert "\\u003c/script\\u003e" in rendered
    assert "https://cdn" not in rendered
    assert "Parity Desk" in rendered
    assert "Genius what-if" in rendered
    assert "← New check" in rendered
    assert "Export CSV" in rendered
    assert "--ts:#173f35" in rendered
    assert ".mode,.picker{display:none!important}" in rendered


def test_dashboard_cli_writes_portable_html(tmp_path, capsys):
    manifest_path = _manifest(tmp_path)
    output_path = tmp_path / "parity-dashboard.html"

    exit_code = main([
        "dashboard",
        str(manifest_path),
        "--output",
        str(output_path),
    ])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert output_path.is_file()
    assert "DASHBOARD: 1 listing(s); self-contained and offline" in captured.err


def test_dashboard_cli_can_launch_compact_window(tmp_path, monkeypatch):
    manifest_path = _manifest(tmp_path)
    output_path = tmp_path / "parity-dashboard.html"
    opened = []
    monkeypatch.setattr(
        "parity.cli._open_compact_dashboard",
        lambda path: opened.append(path) or True,
    )

    exit_code = main([
        "dashboard",
        str(manifest_path),
        "--output",
        str(output_path),
        "--compact",
    ])

    assert exit_code == 0
    assert opened == [output_path]
