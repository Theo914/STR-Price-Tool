from datetime import date
from types import SimpleNamespace

from parity.core import orchestrator
from parity.core.matching import PHash
from parity.core.models import (
    IdentityDecision,
    IdentityEvidence,
    IdentityEvidenceKind,
    MatchResult,
    Outcome,
    PriceResult,
    PropertyRef,
)
from parity.storage import EvidenceStore
from parity.http_control import RequestDeferred


class _FakeAdapter:
    def __init__(self):
        self.priced_ids = []

    def get_price(self, ref, checkin, checkout, guests):
        self.priced_ids.append(ref.raw_id)
        return PriceResult(
            site=ref.site,
            ref=ref,
            outcome=Outcome.PRICED,
            checkin=checkin,
            checkout=checkout,
            guests=guests,
            comparable_total=100.0,
        )


class _IdentityAdapter:
    def identify(self, url):
        if "travelstaytion" in url:
            return PropertyRef(
                site="travelstaytion",
                raw_id="742313",
                url=url,
                bedrooms=3,
                bathrooms=2,
                capacity=6,
                gallery=["https://images.example/subject.jpg"],
            )
        return PropertyRef(
            site="airbnb",
            raw_id="638215869021568083",
            url=url,
            bedrooms=4,
            bathrooms=2,
            capacity=8,
            gallery=["https://images.example/candidate.jpg"],
        )


def test_unverified_candidate_is_excluded_from_pricing_and_cheapest(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(
        orchestrator,
        "resolve",
        lambda url: SimpleNamespace(site="airbnb", needs_browser=False),
    )
    monkeypatch.setattr(orchestrator, "get_http_adapter", lambda site, client=None: adapter)
    monkeypatch.setattr(orchestrator, "_throttle", lambda site: None)

    subject = PropertyRef(
        site="travelstaytion",
        raw_id="subject",
        url="https://www.travelstaytion.com/stay/1",
    )
    candidate_ref = PropertyRef(
        site="airbnb",
        raw_id="neighbour",
        url="https://www.airbnb.com/rooms/2",
    )
    matches = [
        MatchResult(
            ref=candidate_ref,
            decision=IdentityDecision.PROBABLE_REVIEW_REQUIRED,
            evidence=[IdentityEvidence(
                kind=IdentityEvidenceKind.INSUFFICIENT_PHOTO_MATCH,
                detail="only one photo matched",
            )],
        )
    ]

    prices = orchestrator.price_all(
        client=object(),
        subject=subject,
        matches=matches,
        checkin=date(2026, 10, 1),
        checkout=date(2026, 10, 3),
        guests=2,
    )

    assert adapter.priced_ids == ["subject"]
    assert prices[1].outcome == Outcome.IDENTITY_UNVERIFIED
    assert orchestrator.pick_cheapest(prices).ref.raw_id == "subject"


def test_verified_candidate_is_eligible_for_pricing(monkeypatch):
    adapter = _FakeAdapter()
    monkeypatch.setattr(
        orchestrator,
        "resolve",
        lambda url: SimpleNamespace(
            site="travelstaytion" if "travelstaytion" in url else "airbnb",
            needs_browser=False,
        ),
    )
    monkeypatch.setattr(orchestrator, "get_http_adapter", lambda site, client=None: adapter)
    monkeypatch.setattr(orchestrator, "_throttle", lambda site: None)

    subject = PropertyRef(
        site="travelstaytion",
        raw_id="subject",
        url="https://www.travelstaytion.com/stay/1",
    )
    candidate_ref = PropertyRef(
        site="airbnb",
        raw_id="confirmed",
        url="https://www.airbnb.com/rooms/2",
    )
    matches = [MatchResult(
        ref=candidate_ref,
        decision=IdentityDecision.VERIFIED,
        evidence=[IdentityEvidence(
            kind=IdentityEvidenceKind.MANUAL_VERIFICATION,
            detail="reviewed by Theo: matching gallery and location",
        )],
    )]

    prices = orchestrator.price_all(
        client=object(),
        subject=subject,
        matches=matches,
        checkin=date(2026, 10, 1),
        checkout=date(2026, 10, 3),
        guests=2,
    )

    assert adapter.priced_ids == ["subject", "confirmed"]
    assert all(price.outcome == Outcome.PRICED for price in prices)


def test_two_structural_conflicts_reject_before_photo_downloads(monkeypatch):
    adapter = _IdentityAdapter()
    monkeypatch.setattr(
        orchestrator,
        "resolve",
        lambda url: SimpleNamespace(
            site="travelstaytion" if "travelstaytion" in url else "airbnb",
            needs_browser=False,
        ),
    )
    monkeypatch.setattr(orchestrator, "get_http_adapter", lambda site, client=None: adapter)
    monkeypatch.setattr(orchestrator, "_throttle", lambda site: None)
    monkeypatch.setattr(
        orchestrator,
        "_fetch_gallery_hashes",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("photos were downloaded")),
    )

    _, matches = orchestrator.identify_and_match(
        client=object(),
        subject_url="https://www.travelstaytion.com/stay/742313",
        candidate_urls=["https://www.airbnb.com/rooms/638215869021568083"],
    )

    assert matches[0].decision == IdentityDecision.REJECTED
    assert matches[0].evidence[0].kind == IdentityEvidenceKind.STRUCTURAL_CONFLICT


def test_vrbo_structured_identity_can_verify_by_exact_registration(monkeypatch):
    licence = "IT082053C2UUYPDYCD"

    class _SubjectAdapter:
        def identify(self, url):
            return PropertyRef(
                site="travelstaytion",
                raw_id="319567",
                url=url,
                title="Casetta Borsa by Wonderful Italy",
                registration_ids=[licence],
            )

    class _VrboAdapter:
        def identify(self, url):
            return PropertyRef(
                site="vrbo_expedia",
                raw_id="11180096",
                url=url,
                title="Casetta Borsa by Wonderful Italy",
                registration_ids=[licence],
            )

    monkeypatch.setattr(
        orchestrator,
        "resolve",
        lambda url: SimpleNamespace(
            site="travelstaytion" if "travelstaytion" in url else "vrbo_expedia",
            needs_browser="vrbo" in url,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "get_http_adapter",
        lambda site, client=None: _SubjectAdapter(),
    )
    monkeypatch.setattr(
        orchestrator,
        "get_browser_adapter",
        lambda site, session=None: _VrboAdapter(),
    )

    _, matches = orchestrator.identify_and_match(
        client=object(),
        subject_url="https://www.travelstaytion.com/stay/319567",
        candidate_urls=["https://www.vrbo.com/11180096ha"],
        browser_session=object(),
    )

    assert matches[0].decision == IdentityDecision.VERIFIED
    assert matches[0].evidence[0].kind == IdentityEvidenceKind.REGISTRATION_EXACT


def test_marriott_structured_identity_can_verify_by_exact_registration(monkeypatch):
    licence = "15063044EXT0379"

    class _SubjectAdapter:
        def identify(self, url):
            return PropertyRef(
                site="travelstaytion",
                raw_id="210890",
                url=url,
                title="Casa Tatano",
                registration_ids=[licence],
            )

    class _MarriottAdapter:
        def identify(self, url):
            return PropertyRef(
                site="marriott",
                raw_id="78114516",
                url=url,
                title="Captivating Idyllic House with a Stunning Sea View",
                registration_ids=[licence],
            )

    monkeypatch.setattr(
        orchestrator,
        "resolve",
        lambda url: SimpleNamespace(
            site="travelstaytion" if "travelstaytion" in url else "marriott",
            needs_browser="marriott" in url,
        ),
    )
    monkeypatch.setattr(
        orchestrator, "get_http_adapter", lambda site, client=None: _SubjectAdapter()
    )
    monkeypatch.setattr(
        orchestrator,
        "get_browser_adapter",
        lambda site, session=None: _MarriottAdapter(),
    )

    _, matches = orchestrator.identify_and_match(
        client=object(),
        subject_url="https://www.travelstaytion.com/stay/210890",
        candidate_urls=[
            "https://homes-and-villas.marriott.com/en/properties/78114516"
        ],
        browser_session=object(),
    )

    assert matches[0].decision == IdentityDecision.VERIFIED
    assert matches[0].evidence[0].kind == IdentityEvidenceKind.REGISTRATION_EXACT


def test_gallery_hash_fetch_uses_persistent_cache_without_http(tmp_path):
    expected = PHash(full=123, crop=456)
    image_url = "https://images.example/cached.jpg"

    class _NoHttpClient:
        def get(self, *args, **kwargs):
            raise AssertionError("cached image was downloaded")

    with EvidenceStore(tmp_path / "photo-hashes.db") as store:
        store.save_photo_hash(image_url, expected)
        actual = orchestrator._fetch_gallery_hashes(
            _NoHttpClient(),
            [image_url],
            hash_cache=store,
        )

    assert actual == [expected]


def test_deferred_http_price_does_not_stop_other_channels(monkeypatch):
    class _DeferredAdapter:
        def get_price(self, ref, checkin, checkout, guests):
            raise RequestDeferred("DEFERRED — airbnb cooldown remaining 1h 00m")

    class _WorkingAdapter(_FakeAdapter):
        pass

    deferred = _DeferredAdapter()
    working = _WorkingAdapter()
    monkeypatch.setattr(
        orchestrator,
        "resolve",
        lambda url: SimpleNamespace(
            site="airbnb" if "airbnb" in url else "travelstaytion",
            needs_browser=False,
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "get_http_adapter",
        lambda site, client=None: deferred if site == "airbnb" else working,
    )
    monkeypatch.setattr(orchestrator, "_throttle", lambda site: None)

    subject = PropertyRef(
        site="travelstaytion",
        raw_id="subject",
        url="https://www.travelstaytion.com/stay/1",
    )
    candidate = PropertyRef(
        site="airbnb",
        raw_id="candidate",
        url="https://www.airbnb.com/rooms/2",
    )
    matches = [MatchResult(
        ref=candidate,
        decision=IdentityDecision.VERIFIED,
        evidence=[IdentityEvidence(
            kind=IdentityEvidenceKind.MANUAL_VERIFICATION,
            detail="test",
        )],
    )]

    prices = orchestrator.price_all(
        client=object(),
        subject=subject,
        matches=matches,
        checkin=date(2026, 10, 1),
        checkout=date(2026, 10, 3),
        guests=2,
    )

    assert prices[0].outcome == Outcome.PRICED
    assert prices[1].outcome == Outcome.DEFERRED
    assert "cooldown remaining" in prices[1].notes


def test_identity_matching_stops_after_first_verified_candidate_per_site(monkeypatch):
    calls = []

    class Adapter:
        def identify(self, url):
            calls.append(url)
            if "travelstaytion" in url:
                return PropertyRef(
                    site="travelstaytion",
                    raw_id="1",
                    url=url,
                    registration_ids=["ABC-123"],
                )
            return PropertyRef(
                site="airbnb",
                raw_id=url.rsplit("/", 1)[-1],
                url=url,
                registration_ids=["ABC-123"],
            )

    adapter = Adapter()
    monkeypatch.setattr(
        orchestrator,
        "resolve",
        lambda url: SimpleNamespace(
            site="travelstaytion" if "travelstaytion" in url else "airbnb",
            needs_browser=False,
        ),
    )
    monkeypatch.setattr(
        orchestrator, "get_http_adapter", lambda site, client=None: adapter
    )
    monkeypatch.setattr(orchestrator, "_throttle", lambda site: None)

    _, matches = orchestrator.identify_and_match(
        client=object(),
        subject_url="https://www.travelstaytion.com/stay/1",
        candidate_urls=[
            "https://www.airbnb.com/rooms/first",
            "https://www.airbnb.com/rooms/second",
        ],
        stop_after_verified_sites=True,
    )

    assert [match.ref.raw_id for match in matches] == ["first"]
    assert calls == [
        "https://www.travelstaytion.com/stay/1",
        "https://www.airbnb.com/rooms/first",
    ]
