"""Data types shared by every adapter and the orchestrator.

The dataclasses are the shapes that flow through one check.  Durable discovery
evidence, verified mappings, request telemetry and photo hashes live in the
SQLite stores defined in :mod:`parity.storage`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional


class Outcome(str, Enum):
    """What happened when we tried to price a listing on a channel."""

    PRICED = "priced"
    UNAVAILABLE = "unavailable"      # sold out for these dates
    MIN_STAY = "min_stay"            # stay too short for this property's rule
    LOGIN_REQUIRED = "login"         # site demanded auth to see a price
    BLOCKED = "blocked"              # bot detection / captcha
    DEFERRED = "deferred"            # cooldown or configured request budget
    NOT_FOUND = "not_found"          # listing/property page didn't resolve
    IDENTITY_UNVERIFIED = "identity_unverified"  # candidate was not proven to be the subject property
    INCOMPLETE_EVIDENCE = "incomplete_evidence"  # exact price observed, but required comparable components are missing
    UNKNOWN_SITE = "unknown_site"    # registry couldn't match this domain to an adapter
    ERROR = "error"                  # unexpected failure; message kept, run continues


class IdentityDecision(str, Enum):
    """Whether a candidate may be treated as the subject property."""

    VERIFIED = "verified"
    PROBABLE_REVIEW_REQUIRED = "probable_review_required"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REJECTED = "rejected"


class IdentityEvidenceKind(str, Enum):
    REGISTRATION_EXACT = "registration_exact"
    PHOTO_MATCH = "photo_match"
    MANUAL_VERIFICATION = "manual_verification"
    IDENTIFICATION_FAILURE = "identification_failure"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    INSUFFICIENT_PHOTO_MATCH = "insufficient_photo_match"
    STRUCTURAL_CONFLICT = "structural_conflict"


@dataclass
class PropertyRef:
    """Whatever an adapter needs to re-query a specific listing.

    `raw_id` is the adapter-specific id (a TS numeric id, an Airbnb room id,
    a Guesty 24-hex propertyId, etc). `extra` carries adapter-specific bits
    (e.g. Lodgify's site slug, Hostaway's bookingEngine slug) without forcing
    every adapter to share a schema.
    """

    site: str
    raw_id: str
    url: str
    licence_number: Optional[str] = None
    registration_ids: list[str] = field(default_factory=list)
    lat: Optional[float] = None
    lng: Optional[float] = None
    country: Optional[str] = None
    city: Optional[str] = None
    postcode: Optional[str] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[float] = None
    capacity: Optional[int] = None
    property_type: Optional[str] = None
    amenities: list[str] = field(default_factory=list)
    title: Optional[str] = None
    gallery: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


@dataclass
class PriceBreakdown:
    nightly_total: Optional[float] = None
    discount: Optional[float] = None          # positive magnitude; subtracted from comparable_total
    cleaning: Optional[float] = None
    service_fee: Optional[float] = None
    other_mandatory_fees: Optional[float] = None
    tourist_tax: Optional[float] = None      # excluded from comparable_total
    deposit: Optional[float] = None          # excluded from comparable_total (refundable)


@dataclass
class PriceLineItem:
    """One auditable checkout line; discounts are stored as negative amounts."""

    label: str
    amount: float
    category: str
    currency: Optional[str] = None
    included_in_displayed_total: Optional[bool] = None


@dataclass
class PriceResult:
    site: str
    ref: PropertyRef
    outcome: Outcome
    checkin: date
    checkout: date
    guests: int
    currency: Optional[str] = None
    comparable_total: Optional[float] = None   # nightly + mandatory fees, excl. tax & deposit
    headline_total: Optional[float] = None     # whatever the site shows as "Total"
    breakdown: PriceBreakdown = field(default_factory=PriceBreakdown)
    line_items: list[PriceLineItem] = field(default_factory=list)
    reconciliation_difference: Optional[float] = None
    refundable: Optional[bool] = None
    cancellation_type: Optional[str] = None    # free text, e.g. "non-refundable", "24h free then 50%"
    minimum_stay_nights: Optional[int] = None  # exact restriction for the requested check-in, when exposed
    breakdown_status: str = "unavailable"     # exact | unavailable
    breakdown_note: Optional[str] = None
    source_url: Optional[str] = None
    notes: Optional[str] = None                # why blank / what went wrong
    # Every rate the channel offered for the same stay (total + terms); the
    # compared rate is marked "selected".  Informational only.
    rate_options: list[dict] = field(default_factory=list)
    # The room type compared when a channel lists several (Booking complexes).
    room_name: Optional[str] = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_priced(self) -> bool:
        return self.outcome == Outcome.PRICED and self.comparable_total is not None


@dataclass(frozen=True)
class PriceComparison:
    """Auditable comparison of two aligned guest-visible quotes.

    ``candidate_minus_subject`` is negative when the competitor is cheaper.
    The primary verdict uses each site's displayed total and the percentage
    uses the TravelStaytion displayed total as its base.  Component totals
    that exclude separately captured tax and deposit lines remain attached as
    secondary audit evidence.
    """

    currency: str
    subject_site: str
    candidate_site: str
    comparison_basis: str
    subject_displayed_total: float
    candidate_displayed_total: float
    subject_comparable_total: float
    candidate_comparable_total: float
    candidate_minus_subject: float
    candidate_difference_percent: float
    lower_price_site: Optional[str]
    verdict: str
    refundability_note: Optional[str] = None


@dataclass(frozen=True)
class DiscountScenario:
    """One non-bookable, post-capture discount calculation for the UI."""

    site: str
    discount_percent: float
    status: str  # available | unavailable | already_applied
    currency: Optional[str]
    original_guest_total: Optional[float]
    eligible_accommodation_total: Optional[float]
    additional_discount_amount: Optional[float]
    percentage_tax_reduction: Optional[float]
    adjusted_guest_total: Optional[float]
    adjusted_candidate_minus_subject: Optional[float]
    adjusted_difference_percent: Optional[float]
    note: str


@dataclass
class IdentityEvidence:
    kind: IdentityEvidenceKind
    detail: str
    source_url: Optional[str] = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ManualVerification:
    """An auditable human decision that a candidate is the same property."""

    url: str
    reviewed_by: str
    note: str
    reviewed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MatchResult:
    """Identity decision and the evidence supporting it."""

    ref: PropertyRef
    decision: IdentityDecision
    evidence: list[IdentityEvidence] = field(default_factory=list)

    def is_verified(self) -> bool:
        return self.decision == IdentityDecision.VERIFIED


@dataclass
class DiscoveryCandidate:
    """An unverified URL proposed by one or more discovery providers."""

    ref: PropertyRef
    sources: list[str]
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ProviderDiscoveryResult:
    """Candidates plus a human-readable account of what a provider observed."""

    refs: list[PropertyRef] = field(default_factory=list)
    diagnostic: str = ""


@dataclass
class DiscoveryReport:
    subject: PropertyRef
    candidates: list[DiscoveryCandidate] = field(default_factory=list)
    provider_diagnostics: dict[str, str] = field(default_factory=dict)
    provider_errors: dict[str, str] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class IdentityReport:
    """Identity-only result; no candidate is priced by this workflow."""

    subject: PropertyRef
    matches: list[MatchResult] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class QuoteReport:
    """A price-only result produced from one persisted verified mapping."""

    subject: PropertyRef
    candidate: PropertyRef
    price: PriceResult
    subject_price: Optional[PriceResult] = None
    comparison: Optional[PriceComparison] = None
    minimum_stay_nights: Optional[int] = None
    comparison_stay_nights: Optional[int] = None
    subject_minimum_stay_nights: Optional[int] = None
    candidate_minimum_stay_nights: Optional[int] = None
    candidate_minimum_stay_evidence: Optional[str] = None
    discount_scenarios: list[DiscountScenario] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class NeighbourComp:
    ref: PropertyRef
    comparable_total: Optional[float]
    bedrooms: Optional[int]
    bathrooms: Optional[float]
    distance_km: Optional[float]


@dataclass
class NeighbourReport:
    comps: list[NeighbourComp]
    median: Optional[float]
    our_price: Optional[float]
    pct_vs_median: Optional[float]     # negative = we're cheaper
    percentile: Optional[float]        # % of comps that are more expensive than us
    low_confidence: bool               # True if fewer than MIN_COMPS survived filtering
    filters_used: dict = field(default_factory=dict)


@dataclass
class ListingReport:
    """The full result of one `parity check` run."""

    subject: PropertyRef
    matches: list[MatchResult] = field(default_factory=list)
    prices: list[PriceResult] = field(default_factory=list)
    neighbours: Optional[NeighbourReport] = None
    cheapest: Optional[PriceResult] = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
