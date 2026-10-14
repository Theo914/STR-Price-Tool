from __future__ import annotations

import json

from parity.cli import main


BOOKING_URL = (
    "https://www.booking.com/hotel/it/"
    "villa-andrews-by-wonderful-italy.en-gb.html"
)


def _source() -> str:
    breakdown = {
        "b_total_price": [{
            "b_raw_value_user_currency": 984.1,
            "b_value_user_currency": "€ 984.10",
        }],
        "b_net_room_price": [{
            "b_raw_value": "760.0909000000001",
            "b_value": "€ 760.09",
        }],
        "b_sum_included_charges": [{
            "b_raw_value_user_currency": 224.0091,
        }],
        "b_included_charges": [
            {
                "b_copy": "10 % VAT",
                "b_copy_alt": "VAT",
                "b_type": "percentage__included",
                "b_raw_value": "76.00910000000009",
                "b_value": "€ 76.01",
            },
            {
                "b_copy": "Cleaning fee",
                "b_copy_alt": "Cleaning fee",
                "b_type": "per_stay__included",
                "b_raw_value": "120.00000000000009",
                "b_value": "€ 120",
            },
            {
                "b_copy": "City tax",
                "b_copy_alt": "City tax",
                "b_type": "per_person_per_night__included",
                "b_raw_value": "28.00000000000009",
                "b_value": "€ 28",
            },
        ],
        "b_excluded_charges_amount": 0.0,
        "b_excluded_charges": [],
        "b_conditional_charges": [],
        "b_nonmandatory_charges": [],
        "b_extra_addons": [],
        "b_charges_info_copy": {"b_is_all_included": 1},
    }
    return json.dumps({
        "b_block_id": "1731604301_444769642_2_0_0",
        "b_cancellation_type": "free_cancellation",
        "b_price_breakdown_simplified": breakdown,
    }, separators=(",", ":"))


def test_import_booking_source_is_offline_and_prints_components(tmp_path, capsys):
    source = tmp_path / "villa-andrews-source.html"
    source.write_text(_source(), encoding="utf-8")
    output = tmp_path / "quote.json"

    exit_code = main([
        "import-booking-source",
        "--source", str(source),
        "--candidate-url", BOOKING_URL,
        "--checkin", "2026-10-12",
        "--checkout", "2026-10-19",
        "--guests", "2",
        "--currency", "EUR",
        "--format", "json",
        "--output", str(output),
    ])
    stderr = capsys.readouterr().err
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["price"]["outcome"] == "priced"
    assert report["price"]["headline_total"] == 984.1
    assert report["price"]["comparable_total"] == 880.09
    assert "Cleaning fee: EUR +120.00" in stderr
    assert "10 % VAT: EUR +76.01" in stderr
    assert "City tax: EUR +28.00" in stderr
    assert "DISPLAYED TOTAL: EUR 984.10" in stderr
    assert "COMPARABLE TOTAL: EUR 880.09" in stderr
