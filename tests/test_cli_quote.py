from __future__ import annotations

import json
from datetime import date

import httpx

from parity.cli import main
from parity.adapters.browser.booking import (
    BOOKING_PRICING_SNAPSHOT_VERSION,
    booking_quote_url,
)
from parity.adapters.http.airbnb import _stays_pdp_request
from parity.browser_control import BrowserSnapshotStore
from parity.core.models import (
    IdentityDecision,
    IdentityEvidence,
    IdentityEvidenceKind,
    MatchResult,
    Outcome,
    PriceResult,
    PropertyRef,
)
from parity.http_control import HttpFixtureStore
from parity.storage import EvidenceStore


SUBJECT_URL = "https://www.travelstaytion.com/stay/742313"
CANDIDATE_URL = "https://www.airbnb.com.au/rooms/1099791623724969319"
BOOKING_URL = "https://www.booking.com/hotel/es/villa-sa-planeta.en-gb.html"
VILLA_ANDREWS_SUBJECT_URL = "https://www.travelstaytion.com/stay/1065762"
VILLA_ANDREWS_BOOKING_URL = (
    "https://www.booking.com/hotel/it/villa-andrews-by-wonderful-italy.en-gb.html"
)


def _save_mapping(path) -> None:
    subject = PropertyRef(site="travelstaytion", raw_id="742313", url=SUBJECT_URL)
    candidate = PropertyRef(
        site="airbnb",
        raw_id="1099791623724969319",
        url=CANDIDATE_URL,
    )
    match = MatchResult(
        ref=candidate,
        decision=IdentityDecision.VERIFIED,
        evidence=[IdentityEvidence(
            kind=IdentityEvidenceKind.PHOTO_MATCH,
            detail="verified fixture mapping",
        )],
    )
    with EvidenceStore(path) as store:
        store.save_verified_mapping(subject, match)


def _save_booking_mapping(path) -> None:
    subject = PropertyRef(site="travelstaytion", raw_id="742313", url=SUBJECT_URL)
    candidate = PropertyRef(
        site="booking",
        raw_id="villa-sa-planeta",
        url=BOOKING_URL,
    )
    match = MatchResult(
        ref=candidate,
        decision=IdentityDecision.VERIFIED,
        evidence=[IdentityEvidence(
            kind=IdentityEvidenceKind.PHOTO_MATCH,
            detail="verified Booking fixture mapping",
        )],
    )
    with EvidenceStore(path) as store:
        store.save_verified_mapping(subject, match)


def _save_booking_quote_snapshot(path) -> None:
    ref = PropertyRef(
        site="booking",
        raw_id="villa-sa-planeta",
        url=BOOKING_URL,
    )
    quote_url = booking_quote_url(
        ref,
        date(2027, 2, 1),
        date(2027, 2, 9),
        2,
        "EUR",
    )
    BrowserSnapshotStore(path).save("booking", "pricing", quote_url, {
        "snapshot_version": BOOKING_PRICING_SNAPSHOT_VERSION,
        "canonical_url": BOOKING_URL,
        "quote_url": quote_url,
        "checkin": "2027-02-01",
        "checkout": "2027-02-09",
        "guests": 2,
        "currency": "EUR",
        "outcome": "available",
        "displayed_total": 2150.0,
        "line_items": [
            {
                "label": "8 nights accommodation",
                "amount": 2100.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "Cleaning fee",
                "amount": 50.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
        ],
        "refundable": None,
        "cancellation_type": None,
    })


def _save_villa_andrews_booking_proof(db_path, fixture_path) -> None:
    subject = PropertyRef(
        site="travelstaytion",
        raw_id="1065762",
        url=VILLA_ANDREWS_SUBJECT_URL,
    )
    candidate = PropertyRef(
        site="booking",
        raw_id="villa-andrews-by-wonderful-italy",
        url=VILLA_ANDREWS_BOOKING_URL,
        registration_ids=["BA07200691000079856", "IT072006C200130178"],
    )
    with EvidenceStore(db_path) as store:
        store.save_verified_mapping(subject, MatchResult(
            ref=candidate,
            decision=IdentityDecision.VERIFIED,
            evidence=[IdentityEvidence(
                kind=IdentityEvidenceKind.REGISTRATION_EXACT,
                detail="exact Italian registration identifiers",
            )],
        ))

    quote_url = booking_quote_url(
        candidate,
        date(2026, 10, 12),
        date(2026, 10, 19),
        2,
        "EUR",
    )
    BrowserSnapshotStore(fixture_path).save("booking", "pricing", quote_url, {
        "snapshot_version": BOOKING_PRICING_SNAPSHOT_VERSION,
        "canonical_url": VILLA_ANDREWS_BOOKING_URL,
        "quote_url": quote_url,
        "checkin": "2026-10-12",
        "checkout": "2026-10-19",
        "guests": 2,
        "currency": "EUR",
        "outcome": "available",
        "displayed_total": 923.29,
        "line_items": [
            {
                "label": "7 nights accommodation",
                "amount": 760.0909000000001,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "10 % VAT",
                "amount": 76.00910000000009,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "Cleaning fee",
                "amount": 120.00000000000009,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "City tax",
                "amount": 28.00000000000009,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "Booking.com pays",
                "amount": -60.81,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
        ],
        "refundable": True,
        "cancellation_type": "Free cancellation",
        "evidence_source": "embedded_public_booking_price_breakdown",
    })


def _save_quote_fixture(path) -> None:
    state = {
        "presentation": {
            "stayProductDetailPage": {
                "sections": {
                    "structuredDisplayPrice": {
                        "explanationData": {
                            "priceDetails": [{"items": [
                                {"description": "€100 x 6 nights", "total": {"amount": 600}},
                                {"description": "Weekly discount", "total": {"amount": 60}},
                                {"description": "Cleaning fee", "total": {"amount": 80}},
                                {"description": "Airbnb service fee", "total": {"amount": 90}},
                                {"description": "Occupancy taxes", "total": {"amount": 50}},
                            ]}],
                        },
                        "secondaryLine": {"price": "€760.00 total"},
                    }
                }
            }
        }
    }
    ref = PropertyRef(
        site="airbnb",
        raw_id="1099791623724969319",
        url=CANDIDATE_URL,
    )
    url, params, payload, headers = _stays_pdp_request(
        ref,
        date(2026, 10, 4),
        date(2026, 10, 11),
        2,
        "EUR",
    )
    request = httpx.Request("POST", url, params=params, json=payload, headers=headers)
    response = httpx.Response(200, json=state, request=request)
    HttpFixtureStore(path).save(request, response)


def _save_travelstaytion_quote_fixture(
    path,
    *,
    subject_url=SUBJECT_URL,
    checkin="2026-10-04",
    checkout="2026-10-11",
    guests=2,
    currency="EUR",
    displayed_total=800.0,
):
    raw_id = subject_url.rstrip("/").rsplit("/", 1)[-1]
    url = f"https://www.travelstaytion.com/platform/api/calendar/quote/{raw_id}"
    request = httpx.Request("GET", url, params={
        "currency": currency,
        "checkin": checkin,
        "checkout": checkout,
        "guests": guests,
    })
    response = httpx.Response(200, json={
        "error": False,
        "data": {
            "available": True,
            "currency": currency,
            "price": {
                "totalDecimal": displayed_total,
                "totalNoDiscountDecimal": displayed_total,
                "discountDecimal": 0.0,
            },
            "cancellationInfo": {
                "key": "strict",
                "title": "Strict",
                "policyDescription": "This reservation is non-refundable.",
            },
        },
    }, request=request)
    HttpFixtureStore(path).save(request, response)


def test_quote_offline_uses_verified_mapping_and_only_pricing_fixture(tmp_path, capsys):
    db = tmp_path / "parity.db"
    fixture_dir = tmp_path / "fixtures"
    output = tmp_path / "quote.json"
    _save_mapping(db)
    _save_quote_fixture(fixture_dir)
    _save_travelstaytion_quote_fixture(fixture_dir)

    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--db",
        str(db),
        "--checkin",
        "2026-10-04",
        "--checkout",
        "2026-10-11",
        "--guests",
        "2",
        "--offline",
        "--fixtures-dir",
        str(fixture_dir),
        "--governor-db",
        str(tmp_path / "governor.db"),
        "--format",
        "json",
        "--output",
        str(output),
    ])
    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["subject"]["raw_id"] == "742313"
    assert report["candidate"]["raw_id"] == "1099791623724969319"
    assert report["price"]["outcome"] == "priced"
    assert report["price"]["comparable_total"] == 710.0
    assert report["price"]["headline_total"] == 760.0
    assert report["subject_price"]["headline_total"] == 800.0
    assert report["comparison"]["candidate_minus_subject"] == -40.0
    assert report["discount_scenarios"][0]["discount_percent"] == 10.0
    assert report["discount_scenarios"][0]["status"] == "already_applied"
    assert report["discount_scenarios"][0]["adjusted_guest_total"] == 760.0
    assert "pricing network requests: 0/1, fixture hits: 1" in captured.err
    assert "WHAT-IF DISCOUNTS (HYPOTHETICAL, NOT BOOKABLE)" in captured.err
    assert "10%: already_applied" in captured.err
    assert "page network requests" not in captured.err
    assert "search network requests" not in captured.err
    assert "image network requests" not in captured.err


def test_quote_refuses_to_price_without_verified_mapping(tmp_path, capsys):
    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--db",
        str(tmp_path / "empty.db"),
        "--checkin",
        "2026-10-04",
        "--checkout",
        "2026-10-11",
        "--offline",
        "--fixtures-dir",
        str(tmp_path / "fixtures"),
    ])
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "no unexpired verified airbnb mapping" in captured.err
    assert "network requests" not in captured.err


def test_booking_quote_offline_replays_exact_structured_snapshot(tmp_path, capsys):
    db = tmp_path / "parity.db"
    fixture_dir = tmp_path / "fixtures"
    output = tmp_path / "booking-quote.json"
    _save_booking_mapping(db)
    _save_booking_quote_snapshot(fixture_dir)
    _save_travelstaytion_quote_fixture(
        fixture_dir,
        checkin="2027-02-01",
        checkout="2027-02-09",
        displayed_total=2200.0,
    )

    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--channel",
        "booking",
        "--candidate-url",
        BOOKING_URL,
        "--db",
        str(db),
        "--checkin",
        "2027-02-01",
        "--checkout",
        "2027-02-09",
        "--guests",
        "2",
        "--currency",
        "EUR",
        "--offline",
        "--fixtures-dir",
        str(fixture_dir),
        "--governor-db",
        str(tmp_path / "governor.db"),
        "--format",
        "json",
        "--output",
        str(output),
    ])
    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["candidate"]["site"] == "booking"
    assert report["price"]["outcome"] == "priced"
    assert report["price"]["comparable_total"] == 2150.0
    assert report["price"]["headline_total"] == 2150.0
    assert report["subject_price"]["headline_total"] == 2200.0
    assert report["comparison"]["candidate_minus_subject"] == -50.0
    assert report["comparison"]["comparison_basis"] == (
        "guest_visible_displayed_total"
    )
    assert [
        scenario["discount_percent"]
        for scenario in report["discount_scenarios"]
    ] == [10.0, 15.0, 20.0]
    assert [
        scenario["adjusted_guest_total"]
        for scenario in report["discount_scenarios"]
    ] == [1940.0, 1835.0, 1730.0]
    assert [
        scenario["adjusted_candidate_minus_subject"]
        for scenario in report["discount_scenarios"]
    ] == [-260.0, -365.0, -470.0]
    assert "replaying structured browser snapshots" in captured.err
    assert "[booking] pricing network requests: 0/1, fixture hits: 1" in captured.err
    assert "WHAT-IF DISCOUNTS (HYPOTHETICAL, NOT BOOKABLE)" in captured.err
    assert "10%: EUR 1940.00 guest total; additional discount EUR 210.00" in captured.err
    assert "launching signed browser" not in captured.err


def test_villa_andrews_booking_html_breakdown_replays_as_comparable_quote(
    tmp_path,
    capsys,
):
    db = tmp_path / "parity.db"
    fixture_dir = tmp_path / "fixtures"
    output = tmp_path / "villa-andrews-booking-quote.json"
    _save_villa_andrews_booking_proof(db, fixture_dir)
    _save_travelstaytion_quote_fixture(
        fixture_dir,
        subject_url=VILLA_ANDREWS_SUBJECT_URL,
        checkin="2026-10-12",
        checkout="2026-10-19",
        displayed_total=950.0,
    )

    exit_code = main([
        "quote",
        VILLA_ANDREWS_SUBJECT_URL,
        "--channel",
        "booking",
        "--candidate-url",
        VILLA_ANDREWS_BOOKING_URL,
        "--db",
        str(db),
        "--checkin",
        "2026-10-12",
        "--checkout",
        "2026-10-19",
        "--guests",
        "2",
        "--currency",
        "EUR",
        "--offline",
        "--fixtures-dir",
        str(fixture_dir),
        "--governor-db",
        str(tmp_path / "governor.db"),
        "--format",
        "json",
        "--output",
        str(output),
    ])
    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["price"]["outcome"] == "priced"
    assert report["price"]["headline_total"] == 923.29
    assert report["price"]["comparable_total"] == 819.28
    assert report["price"]["breakdown"]["discount"] == 60.81
    assert report["price"]["breakdown"]["cleaning"] == 120.0
    assert report["price"]["breakdown"]["tourist_tax"] == 104.01
    assert report["subject_price"]["headline_total"] == 950.0
    assert report["comparison"]["candidate_minus_subject"] == -26.71
    assert "[booking] pricing network requests: 0/1, fixture hits: 1" in captured.err
    assert "launching signed browser" not in captured.err
    assert "[parity]   7 nights accommodation: EUR +760.09" in captured.err
    assert "[parity]   Cleaning fee: EUR +120.00" in captured.err
    assert "[parity]   10 % VAT: EUR +76.01" in captured.err
    assert "[parity]   City tax: EUR +28.00" in captured.err
    assert "[parity]   Booking.com pays: EUR -60.81" in captured.err
    assert "[parity]   DISPLAYED TOTAL: EUR 923.29" in captured.err
    assert "[parity]   COMPARABLE TOTAL: EUR 819.28" in captured.err


def test_booking_quote_live_is_locked_before_browser_or_network(tmp_path, capsys):
    db = tmp_path / "parity.db"
    _save_booking_mapping(db)

    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--channel",
        "booking",
        "--candidate-url",
        BOOKING_URL,
        "--db",
        str(db),
        "--checkin",
        "2027-02-01",
        "--checkout",
        "2027-02-09",
    ])
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "live Booking pricing capture is locked" in captured.err
    assert "launching signed browser" not in captured.err
    assert "network requests" not in captured.err


def test_booking_diagnostic_capture_requires_fixture_recording(tmp_path, capsys):
    db = tmp_path / "parity.db"
    _save_booking_mapping(db)

    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--channel",
        "booking",
        "--candidate-url",
        BOOKING_URL,
        "--db",
        str(db),
        "--checkin",
        "2027-02-01",
        "--checkout",
        "2027-02-09",
        "--enable-booking-pricing-capture",
    ])
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "requires --record-fixtures" in captured.err
    assert "launching signed browser" not in captured.err
    assert "network requests" not in captured.err


def test_live_quote_rejects_near_term_date_before_mapping_or_network(
    tmp_path,
    capsys,
    monkeypatch,
):
    monkeypatch.setattr(
        "parity.cli.operating_today",
        lambda name: date(2026, 10, 12),
    )

    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--db",
        str(tmp_path / "empty.db"),
        "--checkin",
        "2026-10-19",
        "--checkout",
        "2026-10-26",
    ])
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "live pricing check-in is too close or has passed" in captured.err
    assert "earliest permitted check-in is 2026-11-11" in captured.err
    assert "network requests" not in captured.err


def test_auto_dates_select_first_priced_subject_window_and_reuse_it(
    tmp_path,
    capsys,
    monkeypatch,
):
    db = tmp_path / "parity.db"
    output = tmp_path / "auto-date-quote.json"
    _save_mapping(db)
    calls = []
    subject_currencies = []
    subject_outcomes = iter([
        Outcome.UNAVAILABLE,
        Outcome.MIN_STAY,
        Outcome.PRICED,
    ])

    class FakeAdapter:
        def get_price(self, ref, checkin, checkout, guests):
            calls.append((ref.site, checkin, checkout, guests))
            if ref.site == "travelstaytion":
                subject_currencies.append(ref.extra.get("quote_currency"))
            outcome = (
                next(subject_outcomes)
                if ref.site == "travelstaytion"
                else Outcome.PRICED
            )
            total = (
                1480.57
                if ref.site == "travelstaytion" and outcome == Outcome.PRICED
                else 1387.50
                if outcome == Outcome.PRICED
                else None
            )
            return PriceResult(
                site=ref.site,
                ref=ref,
                outcome=outcome,
                checkin=checkin,
                checkout=checkout,
                guests=guests,
                currency="EUR",
                comparable_total=total,
                headline_total=total,
                refundable=True if outcome == Outcome.PRICED else None,
                notes="minimum stay rule" if outcome == Outcome.MIN_STAY else None,
            )

    monkeypatch.setattr(
        "parity.cli.operating_today",
        lambda name: date(2026, 10, 12),
    )
    monkeypatch.setattr(
        "parity.cli.get_http_adapter",
        lambda site, client=None: FakeAdapter(),
    )

    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--candidate-url",
        CANDIDATE_URL,
        "--db",
        str(db),
        "--auto-dates",
        "--target-lead-days",
        "60",
        "--auto-date-search-weeks",
        "4",
        "--governor-db",
        str(tmp_path / "governor.db"),
        "--format",
        "json",
        "--output",
        str(output),
    ])
    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["price"]["checkin"] == "2026-12-28"
    assert report["price"]["checkout"] == "2027-01-04"
    assert report["subject_price"]["site"] == "travelstaytion"
    assert report["subject_price"]["comparable_total"] == 1480.57
    assert report["comparison"] == {
        "currency": "EUR",
        "subject_site": "travelstaytion",
        "candidate_site": "airbnb",
        "comparison_basis": "guest_visible_displayed_total",
        "subject_displayed_total": 1480.57,
        "candidate_displayed_total": 1387.5,
        "subject_comparable_total": 1480.57,
        "candidate_comparable_total": 1387.5,
        "candidate_minus_subject": -93.07,
        "candidate_difference_percent": -6.29,
        "lower_price_site": "airbnb",
        "verdict": "candidate_cheaper",
        "refundability_note": None,
    }
    policy = report["candidate"]["extra"]["date_policy"]
    assert policy["mode"] == "automatic"
    assert policy["selected_window_index"] == 3
    assert [probe["outcome"] for probe in policy["probes"]] == [
        "unavailable",
        "min_stay",
        "priced",
    ]
    assert calls[-1] == (
        "airbnb",
        date(2026, 12, 28),
        date(2027, 1, 4),
        2,
    )
    assert "automatic dates selected: 2026-12-28 to 2027-01-04" in captured.err
    assert "min_stay -- minimum stay rule" in captured.err
    assert subject_currencies == ["EUR", "EUR", "EUR"]
    assert "PARITY: airbnb cheaper by EUR 93.07 (6.29%)" in captured.err
    assert "TRAVELSTAYTION GUEST TOTAL: EUR 1480.57" in captured.err
    assert "AIRBNB GUEST TOTAL: EUR 1387.50" in captured.err


def test_auto_minimum_stay_prices_only_shortest_common_stay(
    tmp_path,
    capsys,
    monkeypatch,
):
    db = tmp_path / "parity.db"
    output = tmp_path / "minimum-stay-quote.json"
    _save_mapping(db)
    calls = []

    class FakeAdapter:
        def get_price(self, ref, checkin, checkout, guests):
            nights = (checkout - checkin).days
            calls.append((ref.site, nights))
            minimum = 4 if ref.site == "travelstaytion" else 10
            outcome = Outcome.PRICED if nights >= minimum else Outcome.MIN_STAY
            total = (
                800.0 if ref.site == "travelstaytion" else 900.0
            ) if outcome == Outcome.PRICED else None
            return PriceResult(
                site=ref.site,
                ref=ref,
                outcome=outcome,
                checkin=checkin,
                checkout=checkout,
                guests=guests,
                currency="EUR" if outcome == Outcome.PRICED else None,
                comparable_total=total,
                headline_total=total,
                refundable=True if outcome == Outcome.PRICED else None,
                minimum_stay_nights=minimum,
                notes=(
                    f"minimum stay is {minimum} nights"
                    if outcome == Outcome.MIN_STAY
                    else None
                ),
            )

    monkeypatch.setattr(
        "parity.cli.operating_today",
        lambda name: date(2026, 10, 12),
    )
    monkeypatch.setattr(
        "parity.cli.get_http_adapter",
        lambda site, client=None: FakeAdapter(),
    )

    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--candidate-url",
        CANDIDATE_URL,
        "--db",
        str(db),
        "--auto-minimum-stay",
        "--target-lead-days",
        "60",
        "--auto-date-search-weeks",
        "1",
        "--governor-db",
        str(tmp_path / "governor.db"),
        "--format",
        "json",
        "--output",
        str(output),
    ])
    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["price"]["checkin"] == "2026-12-14"
    assert report["price"]["checkout"] == "2026-12-24"
    assert report["minimum_stay_nights"] == 10
    assert report["subject_price"]["checkout"] == "2026-12-24"
    assert report["comparison"]["subject_displayed_total"] == 800.0
    assert report["comparison"]["candidate_displayed_total"] == 900.0
    policy = report["candidate"]["extra"]["date_policy"]
    assert policy["mode"] == "automatic_minimum_stay"
    assert policy["minimum_stay_nights"] == 10
    assert policy["comparison_stay_nights"] == 10
    assert policy["subject_minimum_stay_nights"] == 4
    assert policy["candidate_minimum_stay_nights"] == 10
    assert policy["candidate_minimum_stay_evidence"] == "exact"
    assert report["comparison_stay_nights"] == 10
    assert report["subject_minimum_stay_nights"] == 4
    assert report["candidate_minimum_stay_nights"] == 10
    assert [probe["site"] for probe in policy["probes"]] == [
        "travelstaytion",
        "travelstaytion",
        "airbnb",
        "travelstaytion",
        "airbnb",
    ]
    assert calls == [
        ("travelstaytion", 1),
        ("travelstaytion", 4),
        ("airbnb", 4),
        ("travelstaytion", 10),
        ("airbnb", 10),
    ]
    assert "common minimum stay selected: 2026-12-14 to 2026-12-24" in captured.err
    assert "10 night(s)" in captured.err
    assert "PRICE BREAKDOWN: unavailable" in captured.err


def test_quote_rejects_a_non_iso_currency_code_before_network_or_database_use(capsys):
    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--checkin",
        "2027-02-01",
        "--checkout",
        "2027-02-02",
        "--currency",
        "£",
    ])

    assert exit_code == 3
    assert "currency must be a three-letter code" in capsys.readouterr().err
