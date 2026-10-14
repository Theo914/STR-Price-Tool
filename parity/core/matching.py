"""Same-property matching: licence number first (EU Reg 2024/1028), photo
pHash as the fallback for non-EU listings with no registration scheme.

Both pieces are ported directly from the console snippets verified live
against Casa Tatano (see parity-price-verification-2026-09-22.md).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

# Licence-number regex: short-term-rental registration numbers as they
# actually appear in page text/scripts across EU sites, plus the common
# "licence/registration/CIN/AMA number: XXXX" phrasing fallback.
LICENCE_RE = re.compile(
    r"(?<![\w.])(IT\d{6}[A-Z]\d[A-Z0-9]{8}"
    r"|(?:\d{5}|2[AB]\d{3})\d{6}[A-Z0-9]{2}"
    r"|(?:VFT|VUT|HUT[A-Z]?)[/-]?[A-Z0-9/-]{4,30}"
    r"|\d{3,7}/AL"
    # Australia: WA STRA register and NSW PID-STRA numbers.
    r"|STRA\d{4}[A-Z0-9]{6,10}|PID-STRA-\d{3,7}(?:-[A-Z0-9]{3,6})?)(?![\w.])"
    r"|(?:licen[cs]e|registration|registro|\bCIN|\bAMA|ΑΜΑ)"
    r"\W{0,15}(?:number|no|n[°º])?\W{0,5}([A-Z0-9][A-Z0-9/-]{4,30})",
    re.IGNORECASE,
)


def find_licence_numbers(text: str) -> dict[str, str]:
    """Return {licence_value: 90-char context snippet} for every hit,
    filtering obvious noise (needs a licence-like value, not a bare word)."""
    out: dict[str, str] = {}
    for m in LICENCE_RE.finditer(text):
        val = m.group(1) or m.group(2)
        val = re.sub(r"[A-Z]?[a-z].*", "", val)  # trim glued trailing words
        if sum(c.isdigit() for c in val) > 3:
            start = max(0, m.start() - 30)
            out[val] = text[start:start + 90]
    return out


def normalise_registration(value: object) -> str:
    """Compare registration ids without case, spaces or punctuation."""
    return re.sub(r"[^0-9A-Z]", "", str(value or "").upper())


def is_distinctive_registration(value: object) -> bool:
    """A normalised id long and specific enough to prove identity."""
    compact = normalise_registration(value)
    digits = sum(c.isdigit() for c in compact)
    return len(compact) >= 6 and digits >= 3


_TOKEN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9/_.-]{0,48}[A-Za-z0-9])?")


def registration_tokens(text: str, limit: int = 500000) -> set[str]:
    """Every licence-shaped token in page text, normalised.

    Registration formats differ by country and new schemes appear often, so
    identity also checks whether the subject's known id occurs anywhere in
    the candidate page.  Adjacent tokens are joined to catch ids printed with
    spaces (``HUTB 012345``).
    """
    raw = _TOKEN_RE.findall(text or "")[:limit]
    tokens: set[str] = set()
    previous = ""
    for item in raw:
        for candidate in (item, previous + item):
            compact = normalise_registration(candidate)
            if 6 <= len(compact) <= 48 and is_distinctive_registration(compact):
                tokens.add(compact)
        previous = item if len(item) <= 12 else ""
    return tokens


def common_registration_ids(subject_ids, candidate_ids, candidate_tokens=()) -> set[str]:
    """Subject ids also published by the candidate, compared normalised."""
    candidate_keys = {normalise_registration(item) for item in candidate_ids if item}
    candidate_keys |= set(candidate_tokens or ())
    return {
        item
        for item in subject_ids
        if item
        and is_distinctive_registration(item)
        and normalise_registration(item) in candidate_keys
    }


# ---- Photo matching (DCT pHash) ------------------------------------------
#
# Validated thresholds (parity-price-verification-2026-09-22.md):
#   <=4 bits (of 64)  -> confirmed match
#   <=8 bits          -> likely match
#   true different-photo-of-same-house scores >=14
#   true non-match (different property) scored >=16 in testing
# A listing counts as the same property when >=2 of its photos hit <=8.

CONFIRMED_BITS = 4
LIKELY_BITS = 8
MIN_MATCHING_PHOTOS = 2


@dataclass
class PHash:
    full: int   # 64-bit DCT hash of the whole frame
    crop: int   # 64-bit DCT hash of the centre 80% crop

    def best_distance(self, other: "PHash") -> int:
        return min(
            bin(self.full ^ other.full).count("1"),
            bin(self.crop ^ other.crop).count("1"),
        )


@dataclass(frozen=True)
class PhotoMatchSummary:
    same_property: bool
    matching_pairs: int
    subject_unique: int
    candidate_unique: int


def structural_conflicts(subject, candidate) -> list[str]:
    """Return material configuration conflicts between two listing records.

    Titles and property-type wording are deliberately excluded because channel
    marketing labels vary.  A bedroom mismatch, a capacity difference of two
    or more, or at least one whole bathroom of difference means the listings
    are not safely comparable without human review.
    """
    conflicts: list[str] = []
    if (
        subject.bedrooms is not None
        and candidate.bedrooms is not None
        and subject.bedrooms != candidate.bedrooms
    ):
        conflicts.append(
            f"bedrooms conflict: subject {subject.bedrooms}, candidate {candidate.bedrooms}"
        )
    if (
        subject.capacity is not None
        and candidate.capacity is not None
        and abs(subject.capacity - candidate.capacity) >= 2
    ):
        conflicts.append(
            f"capacity conflict: subject {subject.capacity}, candidate {candidate.capacity}"
        )
    if (
        subject.bathrooms is not None
        and candidate.bathrooms is not None
        and abs(subject.bathrooms - candidate.bathrooms) >= 1.0
    ):
        conflicts.append(
            f"bathrooms conflict: subject {subject.bathrooms:g}, candidate {candidate.bathrooms:g}"
        )
    return conflicts


def _hash_region(im, size: int = 32) -> int:
    """One 64-bit DCT hash: 32x32 DCT-II, top-left 8x8 coefficients,
    thresholded against the median of the 63 AC coefficients (DC term
    excluded from the threshold, included in the output bits) — this
    mirrors the browser implementation validated in the project doc bit
    for bit, so the two are directly comparable.
    """
    import numpy as np
    from PIL import Image
    from scipy.fftpack import dct

    im = im.convert("L").resize((size, size), Image.LANCZOS)
    arr = np.asarray(im, dtype=float)
    d = dct(dct(arr, axis=0, norm="ortho"), axis=1, norm="ortho")
    coeffs64 = d[:8, :8].flatten()
    ac = sorted(coeffs64[1:])            # drop the DC term for the threshold
    median = ac[len(ac) // 2]
    bits = 0
    for c in coeffs64:
        bits = (bits << 1) | (1 if c > median else 0)
    return bits


def phash_image(data: bytes, size: int = 32) -> PHash:
    """32x32 greyscale DCT hash, full frame + centre-80% crop. Requires
    Pillow, numpy and scipy (see requirements.txt)."""
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    full = _hash_region(im, size)
    w, h = im.size
    s = int(min(w, h) * 0.8)
    left, top = (w - s) // 2, (h - s) // 2
    crop = _hash_region(im.crop((left, top, left + s, top + s)), size)
    return PHash(full=full, crop=crop)


def deduplicate_hashes(hashes: list[PHash]) -> list[PHash]:
    """Collapse internally similar photos into connected components.

    A gallery can contain the same source photo at several resolutions, crops
    or positions.  Any hashes joined by the normal photo-match threshold are
    treated as one piece of identity evidence.  Connected components prevent a
    chain of near-duplicates (A~B and B~C) from leaving A and C countable as two
    independent photos merely because they are not directly similar enough.
    """
    if len(hashes) < 2:
        return list(hashes)

    parents = list(range(len(hashes)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left in range(len(hashes)):
        for right in range(left + 1, len(hashes)):
            if hashes[left].best_distance(hashes[right]) <= LIKELY_BITS:
                union(left, right)

    representatives: dict[int, PHash] = {}
    for index, image_hash in enumerate(hashes):
        representatives.setdefault(find(index), image_hash)
    return list(representatives.values())


def match_photo_hashes(
    subject_hashes: list[PHash],
    candidate_hashes: list[PHash],
) -> PhotoMatchSummary:
    """Deduplicate both galleries, then compute a one-to-one photo match."""
    unique_subject = deduplicate_hashes(subject_hashes)
    unique_candidate = deduplicate_hashes(candidate_hashes)

    edges = [
        [
            j
            for j, candidate in enumerate(unique_candidate)
            if subject.best_distance(candidate) <= LIKELY_BITS
        ]
        for subject in unique_subject
    ]
    matched_subject_for_candidate: dict[int, int] = {}

    def _augment(subject_index: int, seen_candidates: set[int]) -> bool:
        for candidate_index in edges[subject_index]:
            if candidate_index in seen_candidates:
                continue
            seen_candidates.add(candidate_index)
            previous_subject = matched_subject_for_candidate.get(candidate_index)
            if previous_subject is None or _augment(previous_subject, seen_candidates):
                matched_subject_for_candidate[candidate_index] = subject_index
                return True
        return False

    matches = sum(_augment(i, set()) for i in range(len(unique_subject)))
    return PhotoMatchSummary(
        same_property=matches >= MIN_MATCHING_PHOTOS,
        matching_pairs=matches,
        subject_unique=len(unique_subject),
        candidate_unique=len(unique_candidate),
    )


def is_same_property(our_hashes: list[PHash], their_hashes: list[PHash]) -> tuple[bool, int]:
    """Compatibility wrapper returning the decision and unique pair count.

    Both galleries are internally deduplicated before one-to-one matching, so
    repeated or near-repeated photos cannot inflate the evidence count.
    """
    summary = match_photo_hashes(our_hashes, their_hashes)
    return summary.same_property, summary.matching_pairs
