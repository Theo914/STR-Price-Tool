from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest

from parity.adapters.browser.expedia import ExpediaAdapter
from parity.adapters.browser.expedia_group_pricing import (
    EXPEDIA_GROUP_PRICING_SNAPSHOT_VERSION,
    _capture_diagnostic_snapshot,
    expedia_group_quote_url,
    parse_expedia_group_pricing_snapshot,
)
from parity.adapters.browser.vrbo_expedia import VrboExpediaAdapter
from parity.browser_control import BrowserSnapshotStore
from parity.core.models import Outcome, PropertyRef
from parity.http_control import RequestClass, RequestDeferred, RequestGovernor
from parity.storage import EvidenceStore


CHECKIN = date(2027, 2, 1)
CHECKOUT = date(2027, 2, 9)


@pytest.fixture(params=["vrbo_expedia", "expedia"])
def case(request):
    if request.param == "vrbo_expedia":
        ref = PropertyRef(
            site="vrbo_expedia",
            raw_id="11180096",
            url="https://www.vrbo.com/11180096ha",
        )
        adapter_cls = VrboExpediaAdapter
    else:
        ref = PropertyRef(
            site="expedia",
            raw_id="89624086",
            url="https://www.expedia.com/x.h89624086.Hotel-Information",
        )
        adapter_cls = ExpediaAdapter
    return request.param, ref, adapter_cls


def _snapshot(platform: str, ref: PropertyRef) -> dict:
    quote_url = expedia_group_quote_url(
        platform, ref, CHECKIN, CHECKOUT, 2, "EUR"
    )
    return {
        "snapshot_version": EXPEDIA_GROUP_PRICING_SNAPSHOT_VERSION,
        "platform": platform,
        "canonical_url": ref.url,
        "quote_url": quote_url,
        "checkin": CHECKIN.isoformat(),
        "checkout": CHECKOUT.isoformat(),
        "guests": 2,
        "currency": "EUR",
        "outcome": "available",
        "displayed_total": 1400.0,
        "line_items": [
            {
                "label": "8 nights accommodation",
                "amount": 1200.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "Promotion discount",
                "amount": -100.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "Cleaning fee",
                "amount": 80.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "Service fee",
                "amount": 120.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "Property fee",
                "amount": 40.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "City tax",
                "amount": 60.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            },
            {
                "label": "Refundable damage deposit",
                "amount": 300.0,
                "currency": "EUR",
                "included_in_displayed_total": False,
                "refundable": True,
            },
        ],
        "refundable": True,
        "cancellation_type": "Free cancellation before 1 January",
    }


def _parse(case, snapshot):
    platform, ref, _ = case
    return parse_expedia_group_pricing_snapshot(
        platform, ref, snapshot, CHECKIN, CHECKOUT, 2, "EUR"
    )


def test_explicit_itemisation_prices_only_after_exact_reconciliation(case):
    platform, ref, _ = case
    result = _parse(case, _snapshot(platform, ref))

    assert result.outcome == Outcome.PRICED
    assert result.comparable_total == 1340.0
    assert result.headline_total == 1400.0
    assert result.reconciliation_difference == 0.0
    assert result.breakdown.nightly_total == 1200.0
    assert result.breakdown.discount == 100.0
    assert result.breakdown.cleaning == 80.0
    assert result.breakdown.service_fee == 120.0
    assert result.breakdown.other_mandatory_fees == 40.0
    assert result.breakdown.tourist_tax == 60.0
    assert result.breakdown.deposit == 300.0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["line_items"].__setitem__(0, {
                "label": "Mystery charge",
                "amount": 1200.0,
                "currency": "EUR",
                "included_in_displayed_total": True,
            }),
            "unknown monetary line",
        ),
        (lambda value: value.__setitem__("displayed_total", 1390.0), "does not reconcile"),
        (
            lambda value: value["line_items"][0].pop("included_in_displayed_total"),
            "does not state whether",
        ),
        (lambda value: value["line_items"][0].pop("currency"), "no explicit currency"),
        (
            lambda value: value["line_items"][1].__setitem__("amount", 100.0),
            "not signed negative",
        ),
        (
            lambda value: value["line_items"][6].pop("refundable"),
            "not explicitly marked refundable",
        ),
    ],
)
def test_ambiguous_or_inconsistent_evidence_is_rejected(case, mutation, message):
    platform, ref, _ = case
    snapshot = _snapshot(platform, ref)
    mutation(snapshot)

    result = _parse(case, snapshot)

    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None
    assert message in result.notes


def test_request_identity_mismatch_is_rejected(case):
    platform, ref, _ = case
    snapshot = _snapshot(platform, ref)
    snapshot["guests"] = 3

    result = _parse(case, snapshot)

    assert result.outcome == Outcome.ERROR
    assert "request mismatch for guests" in result.notes


def test_unavailable_snapshot_returns_no_price(case):
    platform, ref, _ = case
    snapshot = _snapshot(platform, ref)
    snapshot.update({
        "outcome": "unavailable",
        "outcome_detail": "No property availability for these dates",
        "displayed_total": None,
        "line_items": [],
    })

    result = _parse(case, snapshot)

    assert result.outcome == Outcome.UNAVAILABLE
    assert result.comparable_total is None


def test_minimum_stay_requires_a_longer_rule_than_requested(case):
    platform, ref, _ = case
    snapshot = _snapshot(platform, ref)
    snapshot.update({
        "outcome": "min_stay",
        "minimum_stay_nights": 90,
        "displayed_total": None,
        "line_items": [],
    })
    result = _parse(case, snapshot)
    assert result.outcome == Outcome.MIN_STAY
    assert "90 nights" in result.notes

    contradictory = deepcopy(snapshot)
    contradictory["minimum_stay_nights"] = 8
    rejected = _parse(case, contradictory)
    assert rejected.outcome == Outcome.ERROR
    assert "contradicts" in rejected.notes


def test_offline_adapter_replays_exact_snapshot_without_a_browser(tmp_path, case):
    platform, ref, adapter_cls = case
    store = BrowserSnapshotStore(tmp_path)
    quote_url = expedia_group_quote_url(platform, ref, CHECKIN, CHECKOUT, 2, "EUR")
    store.save(platform, "pricing", quote_url, _snapshot(platform, ref))

    class OfflineSession:
        offline = True

        def load_snapshot(self, platform_name, kind, url):
            return store.load(platform_name, kind, url)

    result = adapter_cls(OfflineSession()).get_price(ref, CHECKIN, CHECKOUT, 2)
    assert result.outcome == Outcome.PRICED
    assert result.comparable_total == 1340.0


def test_diagnostic_capture_is_bounded_and_does_not_guess_a_price(case):
    platform, ref, _ = case

    class Locator:
        def evaluate_all(self, script):
            return [{
                "stid": "price-summary",
                "testid": "price-total",
                "role": "",
                "text": "€1,400 total",
                "attributes": {"aria-label": "€1,400 total"},
            }] * 100

    class Page:
        def locator(self, selector):
            return Locator()

    quote_url = expedia_group_quote_url(platform, ref, CHECKIN, CHECKOUT, 2, "EUR")
    snapshot = _capture_diagnostic_snapshot(
        Page(),
        platform=platform,
        ref=ref,
        quote_url=quote_url,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Public checkout body session_token=must-not-be-saved",
    )

    assert snapshot["outcome"] == "unknown"
    assert snapshot["displayed_total"] is None
    assert snapshot["line_items"] == []
    assert snapshot["diagnostics"]["capture_version"] == 2
    assert len(snapshot["diagnostics"]["scoped_price_elements"]) == 80
    assert "must-not-be-saved" not in str(snapshot)


def test_real_vrbo_summary_is_rejected_for_currency_and_itemisation():
    ref = PropertyRef(
        site="vrbo_expedia",
        raw_id="11180096",
        url="https://www.vrbo.com/11180096ha",
    )
    snapshot = _snapshot("vrbo_expedia", ref)
    snapshot.update({
        "outcome": "unknown",
        "displayed_total": None,
        "line_items": [],
        "diagnostics": {
            "capture_version": 1,
            "scoped_price_elements": [{
                "stid": "product-price-summary",
                "text": (
                    "Early booking $56 off $65 The previous price was $65 "
                    "$58 The current price is $58 $466 for 8 nights All fees included"
                ),
            }],
        },
    })

    result = parse_expedia_group_pricing_snapshot(
        "vrbo_expedia", ref, snapshot, CHECKIN, CHECKOUT, 2, "EUR"
    )

    assert result.outcome == Outcome.ERROR
    assert result.comparable_total is None
    assert result.headline_total is None
    assert "ambiguous dollar-denominated 466 total" in result.notes
    assert "despite requested EUR" in result.notes
    assert "does not separately quantify fees or taxes" in result.notes


def test_diagnostic_capture_opens_one_bounded_price_disclosure(case):
    platform, ref, _ = case
    clicks = []

    class Trigger:
        def evaluate(self, script):
            return {"stid": "price-details", "text": "Price details"}

        def click(self, timeout):
            clicks.append(timeout)

    class TriggerLocator:
        def count(self):
            return 1

        def nth(self, index):
            assert index == 0
            return Trigger()

    class ElementsLocator:
        def __init__(self, values):
            self.values = values

        def evaluate_all(self, script):
            return self.values

    class Page:
        def locator(self, selector):
            if 'product-price-summary"] button' in selector:
                return TriggerLocator()
            if '[role="dialog"]' in selector:
                return ElementsLocator([{
                    "stid": "price-breakdown",
                    "testid": "",
                    "role": "dialog",
                    "class_name": "uitk-dialog",
                    "text": "8 nights $400 Cleaning fee $20 Taxes $46 Total $466",
                    "attributes": {},
                }] * 30)
            if 'currency' in selector:
                return ElementsLocator([{
                    "stid": "currency-selector",
                    "text": "USD",
                    "aria_label": "Currency USD",
                    "title": "",
                }])
            return ElementsLocator([{
                "stid": "product-price-summary",
                "testid": "",
                "role": "",
                "text": "$466 for 8 nights All fees included",
                "attributes": {},
            }])

        def wait_for_timeout(self, milliseconds):
            assert milliseconds == 350

    quote_url = expedia_group_quote_url(platform, ref, CHECKIN, CHECKOUT, 2, "EUR")
    snapshot = _capture_diagnostic_snapshot(
        Page(),
        platform=platform,
        ref=ref,
        quote_url=quote_url,
        checkin=CHECKIN,
        checkout=CHECKOUT,
        guests=2,
        currency="EUR",
        body_text="Public checkout",
    )

    assert clicks == [1800]
    assert snapshot["displayed_total"] is None
    assert snapshot["line_items"] == []
    disclosure = snapshot["diagnostics"]["price_disclosures"][0]
    assert disclosure["trigger"]["text"] == "Price details"
    assert len(disclosure["elements"]) == 24
    assert snapshot["diagnostics"]["currency_indicators"][0]["text"] == "USD"


def test_each_expedia_group_pricing_class_has_a_one_navigation_budget(tmp_path):
    with EvidenceStore(tmp_path / "governor.db") as store:
        governor = RequestGovernor(store)
        for platform in ("vrbo_expedia", "expedia"):
            request_class = RequestClass(platform, "pricing")
            governor.before_network_request(request_class)
            with pytest.raises(RequestDeferred, match="budget exhausted"):
                governor.before_network_request(request_class)
