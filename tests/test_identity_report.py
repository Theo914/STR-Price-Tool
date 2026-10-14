import json

from parity.core.models import (
    IdentityDecision,
    IdentityEvidence,
    IdentityEvidenceKind,
    IdentityReport,
    MatchResult,
    PropertyRef,
)
from parity.report import identity_to_console, identity_to_json, identity_verdicts_to_console


def test_identity_report_states_that_no_prices_were_requested():
    subject = PropertyRef(
        site="travelstaytion",
        raw_id="742313",
        url="https://www.travelstaytion.com/stay/742313",
        title="Villa Sa Planeta",
    )
    candidate = PropertyRef(
        site="airbnb",
        raw_id="1099791623724969319",
        url="https://www.airbnb.co.uk/rooms/1099791623724969319",
    )
    report = IdentityReport(
        subject=subject,
        matches=[MatchResult(
            ref=candidate,
            decision=IdentityDecision.VERIFIED,
            evidence=[IdentityEvidence(
                kind=IdentityEvidenceKind.PHOTO_MATCH,
                detail="2 one-to-one photos matched",
            )],
        )],
    )

    console = identity_to_console(report)
    payload = json.loads(identity_to_json(report))

    assert "No prices were requested." in console
    assert payload["matches"][0]["decision"] == "verified"
    assert payload["matches"][0]["evidence"][0]["kind"] == "photo_match"


def test_compact_identity_verdict_is_visible_for_file_output():
    subject = PropertyRef(site="travelstaytion", raw_id="1", url="subject")
    candidate = PropertyRef(site="airbnb", raw_id="2", url="https://airbnb.example/rooms/2")
    report = IdentityReport(
        subject=subject,
        matches=[MatchResult(
            ref=candidate,
            decision=IdentityDecision.REJECTED,
            evidence=[IdentityEvidence(
                kind=IdentityEvidenceKind.STRUCTURAL_CONFLICT,
                detail="bedrooms conflict; capacity conflict",
            )],
        )],
    )

    verdict = identity_verdicts_to_console(report)

    assert "RESULT REJECTED" in verdict
    assert candidate.url in verdict
    assert "bedrooms conflict" in verdict
