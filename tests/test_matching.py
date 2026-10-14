import io

from PIL import Image

from parity.core.matching import (
    PHash,
    deduplicate_hashes,
    is_same_property,
    match_photo_hashes,
    phash_image,
    structural_conflicts,
)
from parity.core.models import PropertyRef


def _hash(value: int) -> PHash:
    return PHash(full=value, crop=value)


def test_one_candidate_photo_cannot_count_as_two_independent_matches():
    subject = [_hash(0), _hash(0)]
    candidate = [_hash(0)]

    matched, count = is_same_property(subject, candidate)

    assert matched is False
    assert count == 1


def test_two_distinct_candidate_photos_can_confirm_a_match():
    subject = [_hash(0), _hash((1 << 20) - 1)]
    candidate = [_hash(0), _hash((1 << 20) - 1)]

    matched, count = is_same_property(subject, candidate)

    assert matched is True
    assert count == 2


def test_duplicates_on_both_sides_cannot_fake_two_independent_matches():
    subject = [_hash(0), _hash(0)]
    candidate = [_hash(0), _hash(0)]

    summary = match_photo_hashes(subject, candidate)

    assert summary.same_property is False
    assert summary.matching_pairs == 1
    assert summary.subject_unique == 1
    assert summary.candidate_unique == 1


def test_near_duplicates_are_collapsed_before_matching():
    subject = [_hash(0), _hash(0b1111)]
    candidate = [_hash(0), _hash(0b1111)]

    matched, count = is_same_property(subject, candidate)

    assert matched is False
    assert count == 1


def test_transitive_duplicate_chain_is_one_piece_of_evidence():
    hashes = [_hash(0), _hash(0x00FF), _hash(0xFFFF)]

    unique = deduplicate_hashes(hashes)

    assert len(unique) == 1


def test_structural_conflicts_find_known_wrong_villa_shape():
    subject = PropertyRef(site="travelstaytion", raw_id="1", url="subject", bedrooms=3, capacity=6)
    candidate = PropertyRef(site="airbnb", raw_id="2", url="candidate", bedrooms=4, capacity=8)

    conflicts = structural_conflicts(subject, candidate)

    assert conflicts == [
        "bedrooms conflict: subject 3, candidate 4",
        "capacity conflict: subject 6, candidate 8",
    ]


def test_half_bath_difference_is_not_a_hard_structural_conflict():
    subject = PropertyRef(site="travelstaytion", raw_id="1", url="subject", bathrooms=2)
    candidate = PropertyRef(site="airbnb", raw_id="2", url="candidate", bathrooms=2.5)

    assert structural_conflicts(subject, candidate) == []


def test_phash_image_hashes_real_png_bytes():
    image = Image.new("RGB", (40, 30))
    for x in range(40):
        for y in range(30):
            image.putpixel((x, y), (x * 6, y * 8, (x + y) * 3))
    payload = io.BytesIO()
    image.save(payload, format="PNG")

    result = phash_image(payload.getvalue())

    assert isinstance(result, PHash)
    assert 0 <= result.full < 2**64
    assert 0 <= result.crop < 2**64
