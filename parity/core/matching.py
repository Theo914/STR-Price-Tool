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


# A bare page token only proves identity when it is specific enough not to
# collide with prices, ids or dates; shorter all-digit ids must appear in
# licence/registration context (candidate_ids, from find_licence_numbers).
MIN_DIGITS_FOR_CONTEXT_FREE_NUMERIC_ID = 10


def common_registration_ids(subject_ids, candidate_ids, candidate_tokens=()) -> set[str]:
    """Subject ids also published by the candidate, compared normalised."""
    labelled = {normalise_registration(item) for item in candidate_ids if item}
    tokens = set(candidate_tokens or ())
    common: set[str] = set()
    for item in subject_ids:
        if not item or not is_distinctive_registration(item):
            continue
        key = normalise_registration(item)
        if key in labelled:
            common.add(item)
        elif key in tokens and (
            not key.isdigit() or len(key) >= MIN_DIGITS_FOR_CONTEXT_FREE_NUMERIC_ID
        ):
            common.add(item)
    return common


def conflicting_registration_ids(subject_ids, candidate_ids) -> set[str]:
    """Candidate ids from the same scheme as the subject's but different.

    Sibling units in one building share common-area photos but carry their
    own registrations (e.g. STRA6056KHBVDM4I vs STRA60568EX3H1MX).  "Same
    scheme" means the same normalised length and first three characters,
    which keeps unrelated numbers (timestamps, prices) from counting.
    """
    subject_keys = {
        normalise_registration(item) for item in subject_ids if is_distinctive_registration(item)
    }
    conflicts: set[str] = set()
    for item in candidate_ids:
        key = normalise_registration(item)
        if not is_distinctive_registration(key) or key in subject_keys:
            continue
        if any(len(key) == len(own) and key[:3] == own[:3] for own in subject_keys):
            conflicts.add(item)
    return conflicts


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


def split_structural_conflicts(subject, candidate) -> tuple[list[str], list[str]]:
    """(blocking, non-blocking) conflicts.

    Channels publish bathrooms inconsistently (half baths, separate WCs) and
    capacity often differs by a sofa bed, so any bathroom difference and a
    capacity difference of exactly two are recorded but do not stop the
    licence/photo checks.  Bedrooms and larger capacity gaps stay blocking.
    """
    blocking: list[str] = []
    soft: list[str] = []
    for item in structural_conflicts(subject, candidate):
        if item.startswith("bathrooms"):
            soft.append(item)
        elif (
            item.startswith("capacity")
            and subject.capacity is not None
            and candidate.capacity is not None
            and abs(subject.capacity - candidate.capacity) == 2
        ):
            soft.append(item)
        else:
            blocking.append(item)
    return blocking, soft


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


# ---- Keypoint fallback (ORB + RANSAC) --------------------------------------
#
# pHash compares whole-frame structure, so a photo that one channel crops,
# letterboxes or watermarks can fall outside the bit threshold.  Keypoint
# matching (the approach in Airbnb's cross-listing patent US11869106) counts
# geometrically consistent local features instead.  Measured live on
# TravelStaytion 337856: the same property scored 554-1259 inliers per photo,
# a different nearby flat scored at most 11.

KEYPOINT_MIN_INLIERS = 60
KEYPOINT_MAX_SIDE = 800
# Keypoints also match shared common areas (pool, facade) across sibling
# units, so the fallback asks for one more independent photo than pHash.
KEYPOINT_MIN_MATCHING_PHOTOS = 3


def keypoint_matching_available() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


def keypoint_features(data: bytes):
    """ORB keypoints/descriptors for one image, or None if undecodable."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    scale = KEYPOINT_MAX_SIDE / max(image.shape)
    if scale < 1:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    keypoints, descriptors = cv2.ORB_create(nfeatures=1500).detectAndCompute(image, None)
    if descriptors is None or len(keypoints) < 10:
        return None
    return keypoints, descriptors


def keypoint_inliers(left, right) -> int:
    """Ratio-tested ORB matches that agree on one homography."""
    import cv2
    import numpy as np

    if left is None or right is None:
        return 0
    left_points, left_descriptors = left
    right_points, right_descriptors = right
    good = []
    for pair in cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(left_descriptors, right_descriptors, k=2):
        if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
            good.append(pair[0])
    if len(good) < 8:
        return 0
    source = np.float32([left_points[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    target = np.float32([right_points[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    _, mask = cv2.findHomography(source, target, cv2.RANSAC, 5.0)
    return int(mask.sum()) if mask is not None else 0


def match_photo_keypoints(subject_images: list[bytes], candidate_images: list[bytes]) -> PhotoMatchSummary:
    """One-to-one photo pairs with at least KEYPOINT_MIN_INLIERS inliers."""
    subject = [f for f in (keypoint_features(item) for item in subject_images) if f is not None]
    candidate = [f for f in (keypoint_features(item) for item in candidate_images) if f is not None]
    scored = sorted(
        (
            (keypoint_inliers(left, right), i, j)
            for i, left in enumerate(subject)
            for j, right in enumerate(candidate)
        ),
        reverse=True,
    )
    used_subject: set[int] = set()
    used_candidate: set[int] = set()
    pairs = 0
    for inliers, i, j in scored:
        if inliers < KEYPOINT_MIN_INLIERS:
            break
        if i in used_subject or j in used_candidate:
            continue
        used_subject.add(i)
        used_candidate.add(j)
        pairs += 1
    return PhotoMatchSummary(
        same_property=pairs >= KEYPOINT_MIN_MATCHING_PHOTOS,
        matching_pairs=pairs,
        subject_unique=len(subject),
        candidate_unique=len(candidate),
    )
