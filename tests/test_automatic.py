import pytest

from parity.automatic import (
    best_review_candidate,
    best_candidate_per_channel,
    first_verified_candidate,
    infer_listing_currency,
    load_historical_candidate_refs,
    ranked_candidates_per_channel,
)
from parity.core.models import (
    DiscoveryCandidate,
    DiscoveryReport,
    IdentityDecision,
    MatchResult,
    PropertyRef,
)
from parity.discovery.engine import score_candidate
from parity.storage import EvidenceStore


def _subject(country="Australia"):
    return PropertyRef(
        site="travelstaytion",
        raw_id="1",
        url="https://www.travelstaytion.com/stay/1",
        country=country,
    )


@pytest.mark.parametrize(
    ("country", "expected"),
    [("Australia", "AUD"), ("United Kingdom", "GBP"), ("Italy", "EUR")],
)
def test_currency_inference_uses_subject_country(country, expected):
    assert infer_listing_currency(_subject(country)) == expected


def test_currency_override_is_validated():
    assert infer_listing_currency(_subject(), "gbp") == "GBP"
    with pytest.raises(ValueError):
        infer_listing_currency(_subject(), "pounds")


def test_best_candidate_selects_first_ranked_item_per_channel():
    candidates = [
        DiscoveryCandidate(PropertyRef("airbnb", "1", "https://airbnb.com/rooms/1"), ["x"], 90),
        DiscoveryCandidate(PropertyRef("airbnb", "2", "https://airbnb.com/rooms/2"), ["x"], 80),
        DiscoveryCandidate(PropertyRef("booking", "a", "https://booking.com/hotel/it/a.html"), ["x"], 70),
    ]
    report = DiscoveryReport(subject=_subject(), candidates=candidates)

    selected = best_candidate_per_channel(report)

    assert selected["airbnb"].ref.raw_id == "1"
    assert selected["booking"].ref.raw_id == "a"


def test_historical_verified_mapping_is_reused_as_candidate_not_proof(tmp_path):
    root = tmp_path / ".parity"
    candidate = PropertyRef(
        "booking",
        "quiet-stay",
        "https://www.booking.com/hotel/au/quiet-stay.en-gb.html",
    )
    with EvidenceStore(root / "old-proof.db") as store:
        store.save_verified_mapping(
            _subject(),
            MatchResult(ref=candidate, decision=IdentityDecision.VERIFIED),
        )

    refs = load_historical_candidate_refs(root, _subject())

    assert [ref.url for ref in refs] == [candidate.url]
    assert refs[0].extra == {"historical_verified_candidate": True}
    assert refs[0].extra.get("verified_mapping") is not True


def test_historical_discovery_candidate_is_available_for_manual_review(tmp_path):
    root = tmp_path / ".parity"
    candidate = DiscoveryCandidate(
        PropertyRef("airbnb", "123", "https://www.airbnb.com/rooms/123"),
        ["airbnb_map_search"],
        62,
    )
    with EvidenceStore(root / "older-search.db") as store:
        store.save_candidates(_subject(), [candidate])

    refs = load_historical_candidate_refs(root, _subject())

    assert refs[0].url == candidate.ref.url
    assert refs[0].extra == {"historical_discovery_candidate": True}


def test_fresh_exact_title_outweighs_historical_cache_bonus():
    subject = _subject()
    subject.title = "The Example House"
    prior = PropertyRef(
        "booking",
        "prior",
        "https://www.booking.com/hotel/gb/prior.html",
        title="Different name",
        extra={"historical_verified_candidate": True},
    )
    fresh = PropertyRef(
        "booking",
        "fresh",
        "https://www.booking.com/hotel/gb/fresh.html",
        title="The Example House",
    )

    assert score_candidate(subject, fresh)[0] > score_candidate(subject, prior)[0]


def test_multi_candidate_selection_uses_first_proven_identity():
    likely = DiscoveryCandidate(
        PropertyRef("booking", "likely", "https://booking.com/hotel/gb/likely.html"),
        ["booking_public_search"],
        90,
    )
    proven = DiscoveryCandidate(
        PropertyRef("booking", "proven", "https://booking.com/hotel/gb/proven.html"),
        ["booking_public_search"],
        80,
    )
    report = DiscoveryReport(subject=_subject(), candidates=[likely, proven])
    ranked = ranked_candidates_per_channel(report)["booking"]
    matches = {
        likely.ref.url: MatchResult(
            ref=likely.ref,
            decision=IdentityDecision.INSUFFICIENT_EVIDENCE,
        ),
        proven.ref.url: MatchResult(
            ref=proven.ref,
            decision=IdentityDecision.VERIFIED,
        ),
    }

    selected = first_verified_candidate(ranked, matches)

    assert selected is not None
    assert selected[0].ref.raw_id == "proven"


def test_manual_review_prefers_partial_evidence_over_unchecked_top_result():
    top = DiscoveryCandidate(
        PropertyRef("airbnb", "top", "https://airbnb.com/rooms/top"), ["x"], 90
    )
    partial = DiscoveryCandidate(
        PropertyRef("airbnb", "partial", "https://airbnb.com/rooms/partial"), ["x"], 80
    )
    match = MatchResult(
        ref=partial.ref,
        decision=IdentityDecision.PROBABLE_REVIEW_REQUIRED,
    )

    selected = best_review_candidate(
        [top, partial], {partial.ref.url: match}
    )

    assert selected is not None
    assert selected[0].ref.raw_id == "partial"
