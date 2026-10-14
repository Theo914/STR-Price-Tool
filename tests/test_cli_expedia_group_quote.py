from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from parity.adapters.browser.expedia_group_pricing import (
    EXPEDIA_GROUP_PRICING_SNAPSHOT_VERSION,
    expedia_group_quote_url,
)
from parity.browser_control import BrowserSnapshotStore
from parity.cli import main
from parity.core.models import (
    IdentityDecision,
    IdentityEvidence,
    IdentityEvidenceKind,
    MatchResult,
    PropertyRef,
)
from parity.http_control import HttpFixtureStore
from parity.storage import EvidenceStore


SUBJECT_URL = "https://www.travelstaytion.com/stay/319567"
CHECKIN = date(2027, 2, 1)
CHECKOUT = date(2027, 2, 9)


CASES = [
    (
        "vrbo",
        "vrbo_expedia",
        "11180096",
        "https://www.vrbo.com/11180096ha",
        "https://www.vrbo.com/it-it/affitto-vacanze/p11180096",
    ),
    (
        "expedia",
        "expedia",
        "89624086",
        "https://www.expedia.com/x.h89624086.Hotel-Information",
        "https://www.expedia.com/Palermo-Hotels-Casetta-Borsa.h89624086.Hotel-Information",
    ),
]


def _save_mapping(path, platform, raw_id, canonical_url):
    subject = PropertyRef(site="travelstaytion", raw_id="319567", url=SUBJECT_URL)
    candidate = PropertyRef(site=platform, raw_id=raw_id, url=canonical_url)
    match = MatchResult(
        ref=candidate,
        decision=IdentityDecision.VERIFIED,
        evidence=[IdentityEvidence(
            kind=IdentityEvidenceKind.PHOTO_MATCH,
            detail="verified Expedia Group fixture mapping",
        )],
    )
    with EvidenceStore(path) as store:
        store.save_verified_mapping(subject, match)
    return candidate


def _save_snapshot(path, platform, ref):
    quote_url = expedia_group_quote_url(
        platform, ref, CHECKIN, CHECKOUT, 2, "EUR"
    )
    BrowserSnapshotStore(path).save(platform, "pricing", quote_url, {
        "snapshot_version": EXPEDIA_GROUP_PRICING_SNAPSHOT_VERSION,
        "platform": platform,
        "canonical_url": ref.url,
        "quote_url": quote_url,
        "checkin": CHECKIN.isoformat(),
        "checkout": CHECKOUT.isoformat(),
        "guests": 2,
        "currency": "EUR",
        "outcome": "available",
        "displayed_total": 760.0,
        "line_items": [
            {
                "label": "8 nights accommodation",
                "amount": 700.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "City tax",
                "amount": 60.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
        ],
        "refundable": None,
        "cancellation_type": None,
    })


def _save_travelstaytion_quote(path):
    url = "https://www.travelstaytion.com/platform/api/calendar/quote/319567"
    request = httpx.Request("GET", url, params={
        "currency": "EUR",
        "checkin": CHECKIN.isoformat(),
        "checkout": CHECKOUT.isoformat(),
        "guests": 2,
    })
    response = httpx.Response(200, json={
        "error": False,
        "data": {
            "available": True,
            "currency": "EUR",
            "price": {
                "totalDecimal": 800.0,
                "totalNoDiscountDecimal": 800.0,
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


@pytest.mark.parametrize(
    ("channel", "platform", "raw_id", "canonical_url", "candidate_url"),
    CASES,
)
def test_expedia_group_quote_offline_uses_canonical_verified_mapping(
    tmp_path,
    capsys,
    channel,
    platform,
    raw_id,
    canonical_url,
    candidate_url,
):
    db = tmp_path / "parity.db"
    fixtures = tmp_path / "fixtures"
    output = tmp_path / f"{channel}-quote.json"
    ref = _save_mapping(db, platform, raw_id, canonical_url)
    _save_snapshot(fixtures, platform, ref)
    _save_travelstaytion_quote(fixtures)

    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--channel", channel,
        "--candidate-url", candidate_url,
        "--db", str(db),
        "--checkin", CHECKIN.isoformat(),
        "--checkout", CHECKOUT.isoformat(),
        "--guests", "2",
        "--currency", "EUR",
        "--offline",
        "--fixtures-dir", str(fixtures),
        "--governor-db", str(tmp_path / "governor.db"),
        "--format", "json",
        "--output", str(output),
    ])
    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["candidate"]["site"] == platform
    assert report["price"]["outcome"] == "priced"
    assert report["price"]["comparable_total"] == 700.0
    assert report["price"]["headline_total"] == 760.0
    assert report["subject_price"]["headline_total"] == 800.0
    assert report["comparison"]["candidate_minus_subject"] == -40.0
    assert "replaying structured browser snapshots" in captured.err
    assert f"[{platform}] pricing network requests: 0/1, fixture hits: 1" in captured.err
    assert "launching signed browser" not in captured.err


@pytest.mark.parametrize(
    ("channel", "platform", "raw_id", "canonical_url", "candidate_url"),
    CASES,
)
def test_expedia_group_live_quote_is_locked_before_browser_or_network(
    tmp_path,
    capsys,
    channel,
    platform,
    raw_id,
    canonical_url,
    candidate_url,
):
    db = tmp_path / "parity.db"
    _save_mapping(db, platform, raw_id, canonical_url)

    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--channel", channel,
        "--candidate-url", candidate_url,
        "--db", str(db),
        "--checkin", CHECKIN.isoformat(),
        "--checkout", CHECKOUT.isoformat(),
    ])
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "pricing capture is locked" in captured.err
    assert "launching signed browser" not in captured.err
    assert "network requests" not in captured.err


@pytest.mark.parametrize(
    ("channel", "platform", "raw_id", "canonical_url", "candidate_url"),
    CASES,
)
def test_expedia_group_diagnostic_capture_requires_fixture_recording(
    tmp_path,
    capsys,
    channel,
    platform,
    raw_id,
    canonical_url,
    candidate_url,
):
    db = tmp_path / "parity.db"
    _save_mapping(db, platform, raw_id, canonical_url)

    exit_code = main([
        "quote",
        SUBJECT_URL,
        "--channel", channel,
        "--candidate-url", candidate_url,
        "--db", str(db),
        "--checkin", CHECKIN.isoformat(),
        "--checkout", CHECKOUT.isoformat(),
        "--enable-expedia-group-pricing-capture",
    ])
    captured = capsys.readouterr()

    assert exit_code == 3
    assert "requires --record-fixtures" in captured.err
    assert "launching signed browser" not in captured.err
    assert "network requests" not in captured.err
