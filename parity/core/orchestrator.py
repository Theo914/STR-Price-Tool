"""Drives one listing check end to end: identify the subject, find matches
on other channels, price every channel, run the neighbour comp, and pick
the cheapest price -- flagging it if it's non-refundable (Theo's 22 Sep
product decision: always show the most competitive price, mention if it
has no refundable rate).

Matching (licence-first, photo fallback) and multi-site discovery are only
as good as what's fed in: this orchestrator does NOT crawl the internet to
*find* the same property on other OTAs on its own. It prices a list of
already-known URLs for the same property (the CLI collects these, e.g. via
a small per-property config, since discovering every mirror listing
automatically was explicitly still an open item in the research). Where a
licence number or gallery is available, it also reports how confidently
each supplied URL matches the subject, using parity.core.matching.
"""
from __future__ import annotations

import sys
import time
from datetime import date
from urllib.parse import urlparse

import httpx

# A minimal politeness delay between requests to the SAME site. This tool
# fires several requests per listing (identify + get_price + up to
# GALLERY_HASH_LIMIT photo fetches for matching, x2+ for subject and every
# candidate) -- hitting Airbnb's rate limiter after ~8 requests in under
# 10 minutes was confirmed live on 22 Sep. This won't eliminate 429s under
# heavy use, but it meaningfully reduces how fast a normal run trips them.
REQUEST_DELAY_SECONDS = {
    "airbnb": 1.5,
}
_last_request_at: dict[str, float] = {}


def _throttle(site: str) -> None:
    delay = REQUEST_DELAY_SECONDS.get(site)
    if not delay:
        return
    last = _last_request_at.get(site)
    now = time.monotonic()
    if last is not None and now - last < delay:
        time.sleep(delay - (now - last))
    _last_request_at[site] = time.monotonic()

from parity.adapters.factory import get_browser_adapter, get_http_adapter
from parity.adapters.http.airbnb import _ID_RE as AIRBNB_ID_RE
from parity.adapters.http.hostaway import _ID_RE as HOSTAWAY_ID_RE
from parity.adapters.http.lodgify import _ID_IN_PATH_RE as LODGIFY_ID_RE
from parity.adapters.http.travelstaytion import _ID_RE as TS_ID_RE
from parity.adapters.registry import resolve
from parity.core.matching import (
    common_registration_ids,
    conflicting_registration_ids,
    find_licence_numbers,
    KEYPOINT_MIN_INLIERS,
    keypoint_matching_available,
    match_photo_hashes,
    match_photo_keypoints,
    phash_image,
    split_structural_conflicts,
    structural_conflicts,
)
from parity.core.models import (
    IdentityDecision,
    IdentityEvidence,
    IdentityEvidenceKind,
    ListingReport,
    ManualVerification,
    MatchResult,
    Outcome,
    PriceResult,
    PropertyRef,
)
from parity.core.neighbours import find_neighbours
from parity.http_control import OfflineFixtureError, RequestDeferred

# Fallback raw-id extraction per site, used ONLY when identify() itself
# threw -- so a page hiccup on the identify() fetch (bot detection,
# transient error, a markup change) doesn't also break pricing, which
# needs nothing but the numeric/slug id out of the URL. Without this, a
# failed identify() silently stuffed the whole URL into raw_id, and
# get_price() then built a broken nested URL (confirmed live 22 Sep: GET
# https://www.airbnb.com/rooms/https://www.airbnb.co.uk/rooms/4554005 ->
# 404) instead of a clean one.
_ID_PATTERNS = {
    "travelstaytion": TS_ID_RE,
    "airbnb": AIRBNB_ID_RE,
    "hostaway": HOSTAWAY_ID_RE,
    "lodgify": LODGIFY_ID_RE,
}


def _best_effort_raw_id(site: str, url: str) -> str:
    pattern = _ID_PATTERNS.get(site)
    if pattern:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return url

# Photo-hash matching downloads real images one at a time -- on a slow or
# bot-throttled connection this can take minutes with NOTHING printed,
# which looks identical to a hang. GALLERY_HASH_LIMIT caps how many photos
# we bother with per listing (2+ matches is enough to confirm, per the
# validated threshold in matching.py) and _log() gives visible progress
# instead of a silent wait.
GALLERY_HASH_LIMIT = 20


def _log(msg: str) -> None:
    print(f"[parity] {msg}", file=sys.stderr, flush=True)


def _fetch_gallery_hashes(
    client: httpx.Client,
    gallery: list[str],
    limit: int = GALLERY_HASH_LIMIT,
    hash_cache=None,
):
    hashes = []
    total = min(len(gallery), limit)
    for i, url in enumerate(gallery[:limit], start=1):
        if hash_cache is not None:
            try:
                cached = hash_cache.load_photo_hash(url)
            except Exception as error:
                _log(f"  photo hash cache read failed ({error}); downloading normally")
                cached = None
            if cached is not None:
                _log(f"  hashing photo {i}/{total}... cached")
                hashes.append(cached)
                continue
        _log(f"  hashing photo {i}/{total}...")
        try:
            resp = client.get(url, timeout=15.0)
            resp.raise_for_status()
            image_hash = phash_image(resp.content)
            hashes.append(image_hash)
            if hash_cache is not None:
                try:
                    hash_cache.save_photo_hash(url, image_hash)
                except Exception as error:
                    _log(f"  photo hash cache write failed ({error}); continuing without cache")
        except (OfflineFixtureError, RequestDeferred):
            raise
        except Exception as e:
            _log(f"  photo {i}/{total} failed ({e}); skipping")
            continue
    return hashes


KEYPOINT_IMAGE_LIMIT = 12


def _fetch_gallery_images(client: httpx.Client, gallery: list[str], limit: int = KEYPOINT_IMAGE_LIMIT) -> list[bytes]:
    """Raw photo bytes for the keypoint fallback (not persisted)."""
    images: list[bytes] = []
    for url in gallery[:limit]:
        try:
            resp = client.get(url, timeout=15.0)
            resp.raise_for_status()
            images.append(resp.content)
        except (OfflineFixtureError, RequestDeferred):
            raise
        except Exception as error:
            _log(f"  keypoint photo download failed ({error}); skipping")
    return images


def identify_and_match(
    client: httpx.Client,
    subject_url: str,
    candidate_urls: list[str],
    browser_session=None,
    manual_verifications: dict[str, ManualVerification] | None = None,
    photo_hash_cache=None,
    stop_after_verified_sites: bool = False,
) -> tuple[PropertyRef, list[MatchResult]]:
    """Identify the subject listing and score how confidently each
    candidate URL is the same property (licence number first, photo pHash
    fallback per parity-price-verification-2026-09-22.md)."""

    subject_match = resolve(subject_url)
    if subject_match is None:
        raise ValueError(f"Could not resolve an adapter for subject URL {subject_url}")
    _log(f"identifying subject: {subject_url}")
    subject_adapter = get_http_adapter(subject_match.site, client=client)
    subject = subject_adapter.identify(subject_url)
    _log(f"  -> {subject.title or subject.raw_id}"
         f"{' (licence ' + subject.licence_number + ')' if subject.licence_number else ''}, "
         f"{len(subject.gallery)} photos")

    subject_hashes = None
    subject_images: list[bytes] | None = None
    matches: list[MatchResult] = []
    verified_sites: set[str] = set()
    manual_verifications = manual_verifications or {}

    soft_conflict_notes: dict[str, str] = {}

    def record(match: MatchResult) -> None:
        note = soft_conflict_notes.get(match.ref.url)
        if note:
            match.evidence.append(IdentityEvidence(
                kind=IdentityEvidenceKind.STRUCTURAL_CONFLICT,
                detail=f"{note}; channels count bathrooms/sofa beds differently, so this was not blocking",
                source_url=match.ref.url,
            ))
        matches.append(match)
        if match.decision == IdentityDecision.VERIFIED:
            verified_sites.add(match.ref.site)

    def manual_evidence(url: str) -> IdentityEvidence | None:
        review = manual_verifications.get(url)
        if review is None:
            return None
        return IdentityEvidence(
            kind=IdentityEvidenceKind.MANUAL_VERIFICATION,
            detail=(
                f"reviewed by {review.reviewed_by} at {review.reviewed_at.isoformat()}: "
                f"{review.note}"
            ),
            source_url=url,
            observed_at=review.reviewed_at,
        )

    for url in candidate_urls:
        cand_match = resolve(url)
        if (
            stop_after_verified_sites
            and cand_match is not None
            and cand_match.site in verified_sites
        ):
            _log(f"skipping candidate after {cand_match.site} identity was verified: {url}")
            continue
        _log(f"matching candidate: {url}")
        reviewed = manual_evidence(url)
        if cand_match is None:
            record(MatchResult(
                ref=PropertyRef(site="unknown", raw_id=url, url=url),
                decision=IdentityDecision.VERIFIED if reviewed else IdentityDecision.INSUFFICIENT_EVIDENCE,
                evidence=([reviewed] if reviewed else []) + [IdentityEvidence(
                    kind=IdentityEvidenceKind.ADAPTER_UNAVAILABLE,
                    detail="could not resolve an adapter for this URL",
                    source_url=url,
                )],
            ))
            continue

        if cand_match.needs_browser:
            if browser_session is None:
                record(MatchResult(
                    ref=PropertyRef(site=cand_match.site, raw_id=url, url=url),
                    decision=IdentityDecision.VERIFIED if reviewed else IdentityDecision.INSUFFICIENT_EVIDENCE,
                    evidence=([reviewed] if reviewed else []) + [IdentityEvidence(
                        kind=IdentityEvidenceKind.IDENTIFICATION_FAILURE,
                        detail="no browser session provided; cannot identify this URL",
                        source_url=url,
                    )],
                ))
                continue
            cand_adapter = get_browser_adapter(cand_match.site, browser_session)
            try:
                cand_ref = cand_adapter.identify(url)
                _log(f"  -> {cand_ref.title or cand_ref.raw_id}")
            except (OfflineFixtureError, RequestDeferred):
                raise
            except Exception as e:
                fallback_id = url  # these sites key by URL when the id isn't in the path anyway
                record(MatchResult(
                    ref=PropertyRef(site=cand_match.site, raw_id=fallback_id, url=url),
                    decision=IdentityDecision.VERIFIED if reviewed else IdentityDecision.INSUFFICIENT_EVIDENCE,
                    evidence=([reviewed] if reviewed else []) + [IdentityEvidence(
                        kind=IdentityEvidenceKind.IDENTIFICATION_FAILURE,
                        detail=f"identify() failed: {e}",
                        source_url=url,
                    )],
                ))
                continue
            if reviewed:
                record(MatchResult(
                    ref=cand_ref,
                    decision=IdentityDecision.VERIFIED,
                    evidence=[reviewed],
                ))
                continue
            if cand_match.site not in {"booking", "vrbo_expedia", "expedia", "marriott"}:
                record(MatchResult(
                    ref=cand_ref,
                    decision=IdentityDecision.INSUFFICIENT_EVIDENCE,
                    evidence=[IdentityEvidence(
                        kind=IdentityEvidenceKind.ADAPTER_UNAVAILABLE,
                        detail="browser adapter does not yet emit enough identity evidence",
                        source_url=url,
                    )],
                ))
                continue
        else:
            cand_adapter = get_http_adapter(cand_match.site, client=client)
            _throttle(cand_match.site)
            try:
                cand_ref = cand_adapter.identify(url)
                _log(f"  -> {cand_ref.title or cand_ref.raw_id}, {len(cand_ref.gallery)} photos")
            except Exception as e:
                fallback_id = _best_effort_raw_id(cand_match.site, url)
                _log(f"  identify() failed ({e}); still able to price it directly"
                     if fallback_id != url else f"  identify() failed ({e}); no id extractable, can't price it")
                record(MatchResult(
                    ref=PropertyRef(site=cand_match.site, raw_id=fallback_id, url=url),
                    decision=IdentityDecision.VERIFIED if reviewed else IdentityDecision.INSUFFICIENT_EVIDENCE,
                    evidence=([reviewed] if reviewed else []) + [IdentityEvidence(
                        kind=IdentityEvidenceKind.IDENTIFICATION_FAILURE,
                        detail=f"identify() failed: {e}",
                        source_url=url,
                    )],
                ))
                continue

            if reviewed:
                record(MatchResult(
                    ref=cand_ref,
                    decision=IdentityDecision.VERIFIED,
                    evidence=[reviewed],
                ))
                continue

        subject_ids = set(subject.registration_ids or ([subject.licence_number] if subject.licence_number else []))
        candidate_ids = set(cand_ref.registration_ids or ([cand_ref.licence_number] if cand_ref.licence_number else []))
        common_ids = common_registration_ids(
            subject_ids,
            candidate_ids,
            cand_ref.extra.get("registration_tokens") or (),
        )
        conflicts, soft = split_structural_conflicts(subject, cand_ref)
        if soft and not conflicts:
            soft_conflict_notes[cand_ref.url] = "; ".join(soft)
            _log(f"  {'; '.join(soft)} -- not blocking; continuing identity checks")
        if conflicts:
            conflicts = conflicts + soft
            conflict_detail = "; ".join(conflicts)
            decision = (
                IdentityDecision.PROBABLE_REVIEW_REQUIRED
                if common_ids or len(conflicts) == 1
                else IdentityDecision.REJECTED
            )
            _log(
                f"  structural conflict -- {decision.value}; skipping photo downloads: "
                f"{conflict_detail}"
            )
            evidence = [IdentityEvidence(
                kind=IdentityEvidenceKind.STRUCTURAL_CONFLICT,
                detail=(
                    f"{conflict_detail}; automatic verification blocked and photo hashing skipped"
                ),
                source_url=url,
            )]
            if common_ids:
                evidence.append(IdentityEvidence(
                    kind=IdentityEvidenceKind.REGISTRATION_EXACT,
                    detail=(
                        f"registration identifier {sorted(common_ids)[0]} matches, "
                        "but the structural conflict requires manual review"
                    ),
                    source_url=url,
                ))
            record(MatchResult(
                ref=cand_ref,
                decision=decision,
                evidence=evidence,
            ))
            continue
        if common_ids:
            matched_id = sorted(common_ids)[0]
            if matched_id:
                _log(f"  licence number matches -- confirmed, skipping photo hashing")
                record(MatchResult(
                    ref=cand_ref,
                    decision=IdentityDecision.VERIFIED,
                    evidence=[IdentityEvidence(
                        kind=IdentityEvidenceKind.REGISTRATION_EXACT,
                        detail=f"exact registration identifier {matched_id}",
                        source_url=url,
                    )],
                ))
                continue

        registration_conflicts = conflicting_registration_ids(subject_ids, candidate_ids)
        if registration_conflicts:
            _log(
                "  candidate publishes a different registration of the same scheme "
                f"({sorted(registration_conflicts)[0]}); photos can only mark it for review"
            )
        _log("  no licence match -- falling back to photo hashing")
        if subject_hashes is None:
            _log(f"  hashing subject's {min(len(subject.gallery), GALLERY_HASH_LIMIT)} photos (one-time)")
            subject_hashes = _fetch_gallery_hashes(
                client,
                subject.gallery,
                hash_cache=photo_hash_cache,
            )
        cand_hashes = _fetch_gallery_hashes(
            client,
            cand_ref.gallery,
            hash_cache=photo_hash_cache,
        )
        photo_summary = match_photo_hashes(subject_hashes, cand_hashes)
        _log(
            "  gallery deduplication: "
            f"subject {len(subject_hashes)} -> {photo_summary.subject_unique} unique; "
            f"candidate {len(cand_hashes)} -> {photo_summary.candidate_unique} unique"
        )
        keypoint_summary = None
        if not photo_summary.same_property and keypoint_matching_available():
            _log("  pHash inconclusive -- trying keypoint (ORB) photo matching")
            if subject_images is None:
                subject_images = _fetch_gallery_images(client, subject.gallery)
            keypoint_summary = match_photo_keypoints(
                subject_images, _fetch_gallery_images(client, cand_ref.gallery)
            )
            _log(f"  keypoint matching: {keypoint_summary.matching_pairs} one-to-one photo pair(s)")
        if registration_conflicts and (
            photo_summary.same_property
            or (keypoint_summary is not None and keypoint_summary.same_property)
        ):
            record(MatchResult(
                ref=cand_ref,
                decision=IdentityDecision.PROBABLE_REVIEW_REQUIRED,
                evidence=[IdentityEvidence(
                    kind=IdentityEvidenceKind.INSUFFICIENT_PHOTO_MATCH,
                    detail=(
                        "photos match, but the candidate publishes a different registration "
                        f"({sorted(registration_conflicts)[0]}) from the same scheme -- possibly "
                        "a sibling unit sharing building photos; manual review required"
                    ),
                    source_url=url,
                )],
            ))
        elif keypoint_summary is not None and keypoint_summary.same_property:
            record(MatchResult(
                ref=cand_ref,
                decision=IdentityDecision.VERIFIED,
                evidence=[IdentityEvidence(
                    kind=IdentityEvidenceKind.PHOTO_MATCH,
                    detail=(
                        f"{keypoint_summary.matching_pairs} one-to-one photos matched by "
                        f"keypoint geometry (>= {KEYPOINT_MIN_INLIERS} RANSAC inliers each); "
                        f"pHash alone found {photo_summary.matching_pairs}"
                    ),
                    source_url=url,
                )],
            ))
        elif photo_summary.same_property:
            record(MatchResult(
                ref=cand_ref,
                decision=IdentityDecision.VERIFIED,
                evidence=[IdentityEvidence(
                    kind=IdentityEvidenceKind.PHOTO_MATCH,
                    detail=(
                        f"{photo_summary.matching_pairs} one-to-one unique photos matched "
                        "within the provisional pHash threshold after gallery deduplication "
                        f"(subject {len(subject_hashes)} -> {photo_summary.subject_unique} unique; "
                        f"candidate {len(cand_hashes)} -> {photo_summary.candidate_unique} unique)"
                    ),
                    source_url=url,
                )],
            ))
        else:
            best_pairs = max(
                photo_summary.matching_pairs,
                keypoint_summary.matching_pairs if keypoint_summary else 0,
            )
            decision = (
                IdentityDecision.PROBABLE_REVIEW_REQUIRED
                if best_pairs > 0 else IdentityDecision.INSUFFICIENT_EVIDENCE
            )
            record(MatchResult(
                ref=cand_ref,
                decision=decision,
                evidence=[IdentityEvidence(
                    kind=IdentityEvidenceKind.INSUFFICIENT_PHOTO_MATCH,
                    detail=(
                        f"only {photo_summary.matching_pairs} one-to-one unique photos matched "
                        "after gallery deduplication "
                        f"(subject {len(subject_hashes)} -> {photo_summary.subject_unique} unique; "
                        f"candidate {len(cand_hashes)} -> {photo_summary.candidate_unique} unique); "
                        "manual review required"
                    ),
                    source_url=url,
                )],
            ))

    return subject, matches


def price_all(
    client: httpx.Client,
    subject: PropertyRef,
    matches: list[MatchResult],
    checkin: date,
    checkout: date,
    guests: int = 2,
    browser_session=None,
) -> list[PriceResult]:
    """Prices the subject plus every matched candidate. HTTP sites price
    via httpx; Vrbo/Expedia, Booking.com and Guesty price via a real
    Patchright browser (browser_session) -- skipped only if no session
    was passed in, not because they're unimplemented."""
    results: list[PriceResult] = []

    def price_http(adapter, ref: PropertyRef) -> PriceResult:
        try:
            return adapter.get_price(ref, checkin, checkout, guests)
        except RequestDeferred as error:
            return PriceResult(
                site=ref.site,
                ref=ref,
                outcome=Outcome.DEFERRED,
                checkin=checkin,
                checkout=checkout,
                guests=guests,
                notes=str(error),
            )

    subject_match = resolve(subject.url)
    if subject_match and not subject_match.needs_browser:
        _log(f"pricing subject on {subject_match.site}...")
        _throttle(subject_match.site)
        adapter = get_http_adapter(subject_match.site, client=client)
        results.append(price_http(adapter, subject))

    for m in matches:
        site_match = resolve(m.ref.url)
        if site_match is None:
            results.append(PriceResult(site="unknown", ref=m.ref, outcome=Outcome.UNKNOWN_SITE,
                                        checkin=checkin, checkout=checkout, guests=guests))
            continue
        if not m.is_verified():
            _log(f"not pricing {m.ref.url} -- property identity is {m.decision.value}")
            evidence_detail = "; ".join(e.detail for e in m.evidence)
            results.append(PriceResult(
                site=site_match.site,
                ref=m.ref,
                outcome=Outcome.IDENTITY_UNVERIFIED,
                checkin=checkin,
                checkout=checkout,
                guests=guests,
                notes=f"candidate excluded from pricing: {m.decision.value}; {evidence_detail}",
            ))
            continue
        if site_match.needs_browser:
            if browser_session is None:
                _log(f"skipping {m.ref.url} -- needs a browser session (none provided this run)")
                continue
            _log(f"pricing {site_match.site} (browser): {m.ref.url}")
            adapter = get_browser_adapter(site_match.site, browser_session)
            results.append(adapter.get_price(m.ref, checkin, checkout, guests))
            continue
        _log(f"pricing {site_match.site}: {m.ref.url}")
        _throttle(site_match.site)
        adapter = get_http_adapter(site_match.site, client=client)
        results.append(price_http(adapter, m.ref))

    return results


def pick_cheapest(prices: list[PriceResult]) -> PriceResult | None:
    priced = [p for p in prices if p.is_priced()]
    if not priced:
        return None
    return min(priced, key=lambda p: p.comparable_total)


def run_check(
    subject_url: str,
    candidate_urls: list[str],
    checkin: date,
    checkout: date,
    guests: int = 2,
    client: httpx.Client | None = None,
    browser_session=None,
    manual_verifications: dict[str, ManualVerification] | None = None,
    photo_hash_cache=None,
) -> ListingReport:
    client = client or httpx.Client(timeout=20.0, follow_redirects=True)

    subject, matches = identify_and_match(
        client,
        subject_url,
        candidate_urls,
        browser_session=browser_session,
        manual_verifications=manual_verifications,
        photo_hash_cache=photo_hash_cache,
    )
    prices = price_all(client, subject, matches, checkin, checkout, guests, browser_session=browser_session)
    cheapest = pick_cheapest(prices)

    neighbours = None
    if subject.lat is not None and subject.lng is not None and subject.bedrooms is not None:
        _log("searching Airbnb for like-for-like neighbour comps...")
        our_price = None
        for p in prices:
            if p.ref.raw_id == subject.raw_id and p.site == subject.site and p.is_priced():
                our_price = p.comparable_total
                break
        exclude_ids = {subject.raw_id} | {m.ref.raw_id for m in matches if m.ref.site == "airbnb"}
        neighbours = find_neighbours(
            client, our_price, subject.lat, subject.lng, subject.bedrooms, exclude_raw_ids=exclude_ids,
        )

    return ListingReport(
        subject=subject, matches=matches, prices=prices, neighbours=neighbours, cheapest=cheapest,
    )
