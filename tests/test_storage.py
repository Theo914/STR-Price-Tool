from parity.core.models import (
    DiscoveryCandidate,
    IdentityDecision,
    IdentityEvidence,
    IdentityEvidenceKind,
    MatchResult,
    PropertyRef,
)
from parity.storage import EvidenceStore
from parity.core.matching import PHash


def _subject():
    return PropertyRef(
        site="travelstaytion",
        raw_id="742313",
        url="https://www.travelstaytion.com/stay/742313",
    )


def test_verified_mapping_round_trip(tmp_path):
    candidate = PropertyRef(
        site="airbnb",
        raw_id="1099791623724969319",
        url="https://www.airbnb.com/rooms/1099791623724969319",
    )
    match = MatchResult(
        ref=candidate,
        decision=IdentityDecision.VERIFIED,
        evidence=[IdentityEvidence(
            kind=IdentityEvidenceKind.MANUAL_VERIFICATION,
            detail="reviewed against gallery and location",
        )],
    )

    with EvidenceStore(tmp_path / "parity.db") as store:
        store.save_verified_mapping(_subject(), match)
        refs = store.load_verified_refs(_subject())

    assert [(ref.site, ref.raw_id, ref.url) for ref in refs] == [
        ("airbnb", "1099791623724969319", "https://www.airbnb.com/rooms/1099791623724969319")
    ]


def test_non_verified_mapping_cannot_be_persisted(tmp_path):
    candidate = PropertyRef(site="airbnb", raw_id="1", url="https://www.airbnb.com/rooms/1")
    match = MatchResult(ref=candidate, decision=IdentityDecision.INSUFFICIENT_EVIDENCE)

    with EvidenceStore(tmp_path / "parity.db") as store:
        try:
            store.save_verified_mapping(_subject(), match)
        except ValueError as error:
            assert "only VERIFIED" in str(error)
        else:
            raise AssertionError("unverified mapping was persisted")


def test_discovery_candidate_and_circuit_state_are_persisted(tmp_path):
    candidate = DiscoveryCandidate(
        ref=PropertyRef(site="airbnb", raw_id="1", url="https://www.airbnb.com/rooms/1"),
        sources=["airbnb_map_search"],
        score=75,
        reasons=["bedrooms exact"],
    )
    with EvidenceStore(tmp_path / "parity.db") as store:
        store.save_candidates(_subject(), [candidate])
        store.open_circuit("airbnb.com", "rate limited", seconds=60)
        assert store.circuit_reason("airbnb.com") == "rate limited"


def test_photo_hash_round_trip(tmp_path):
    expected = PHash(full=0xFEDCBA9876543210, crop=0x0123456789ABCDEF)

    with EvidenceStore(tmp_path / "photo-hashes.db") as store:
        store.save_photo_hash("https://images.example/villa.jpg", expected)
        actual = store.load_photo_hash("https://images.example/villa.jpg")

    assert actual == expected
