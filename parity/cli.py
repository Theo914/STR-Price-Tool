"""Command-line entry point.

  parity check <subject-url> --against URL [URL ...] --checkin YYYY-MM-DD
      --checkout YYYY-MM-DD [--guests N] [--format console|json|csv]

  parity batch <config.json>
      config.json: [{"subject": url, "against": [url, ...], "checkin": ...,
                      "checkout": ..., "guests": 2}, ...]

  parity portfolio <config.json> --output portfolio-results.json
      bounded, resumable quote-only workflow for one host and up to ten listings

Runs entirely on Theo's own machine, logged out, and never books anything --
every adapter only reads prices via GET/POST calls that Theo has already
manually verified are booking-free quote endpoints.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
import re
from urllib.parse import urlparse

import httpx

from parity.adapters.registry import resolve
from parity.adapters.factory import get_browser_adapter, get_http_adapter
from parity.adapters.browser.booking import (
    canonical_booking_url,
    price_booking_source,
)
from parity.adapters.browser.expedia_group_pricing import canonical_platform_url
from parity.core.models import (
    DiscoveryReport,
    IdentityDecision,
    IdentityReport,
    ManualVerification,
    Outcome,
    PriceResult,
    PropertyRef,
    QuoteReport,
)
from parity.http_control import (
    DEFAULT_REQUEST_BUDGETS,
    GovernedHttpClient,
    HttpFixtureStore,
    OfflineFixtureError,
    RequestDeferred,
    RequestGovernor,
)
from parity.discovery.engine import DiscoveryEngine, canonicalize_url
from parity.discovery.web_search import (
    PrecomputedProvider,
    WebLicenceSearchProvider,
    registration_variants,
)
from parity.discovery.providers import (
    AirbnbMapProvider,
    BookingMapProvider,
    BookingSearchProvider,
    HistoricalEvidenceProvider,
    KnownMappingsProvider,
)
from parity.core.date_policy import (
    candidate_date_windows,
    prefer_open_checkins,
    operating_today,
    select_common_minimum_stay_window,
    select_first_available_window,
    select_multi_channel_minimum_stay_window,
    validate_live_date_range,
)
from parity.core.discounts import build_discount_scenarios
from parity.core.pricing import (
    apply_quote_evidence_policy,
    compare_aligned_prices,
    normalise_currency_code,
)
from parity.core.orchestrator import identify_and_match, run_check
from parity.portfolio import (
    PORTFOLIO_STATE_VERSION,
    expand_portfolio_jobs,
    failed_result,
    file_sha256,
    job_fingerprint,
    load_portfolio_config,
    portfolio_csv,
    portfolio_manifest,
    result_from_quote,
)
from parity.report import (
    discovery_to_console,
    discovery_to_json,
    identity_to_console,
    identity_to_json,
    identity_verdicts_to_console,
    print_report,
    quote_to_console,
    quote_to_json,
    quote_verdict_to_console,
)
from parity.storage import EvidenceStore
from parity.dashboard import write_dashboard
from parity.listing_input import (
    normalise_listing_inputs,
    normalise_travelstaytion_listing,
)
from parity.automatic import (
    best_review_candidate,
    evidence_root_for,
    first_verified_candidate,
    infer_listing_currency,
    load_historical_candidate_refs,
    ranked_candidates_per_channel,
)
from parity.app import create_app_server


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _any_needs_browser(urls: list[str]) -> bool:
    for url in urls:
        m = resolve(url)
        if m and m.needs_browser:
            return True
    return False


@contextmanager
def _maybe_browser_session(
    urls: list[str],
    headless: bool,
    *,
    offline: bool = False,
    record_fixtures: bool = False,
    fixtures_dir: str = ".parity/fixtures",
    fixture_ttl_days: int = 30,
    governor=None,
    interactive: bool = False,
):
    """Only launches a browser if one of the URLs actually needs it (Vrbo/
    Expedia, Booking.com, Guesty) -- most checks are pure HTTP and
    shouldn't pay the cost of a browser launch at all."""
    if not _any_needs_browser(urls):
        yield None
        return
    from parity.adapters.browser.browser_session import BrowserSession
    from parity.browser_control import BrowserSnapshotStore

    if offline:
        print("[parity] replaying structured browser snapshots...", file=sys.stderr)
    else:
        print(
            "[parity] launching signed browser for Vrbo/Expedia, Booking.com, Marriott or Guesty...",
            file=sys.stderr,
        )
    with BrowserSession(
        headless=headless,
        snapshot_store=BrowserSnapshotStore(fixtures_dir, ttl_days=fixture_ttl_days),
        governor=governor,
        offline=offline,
        record_fixtures=record_fixtures,
        interactive=interactive,
    ) as session:
        yield session


def _request_budgets(args: argparse.Namespace) -> dict[tuple[str, str], int]:
    if args.fixture_ttl_days <= 0:
        raise ValueError("--fixture-ttl-days must be greater than zero")
    if args.rate_limit_cooldown_hours < 0:
        raise ValueError("--rate-limit-cooldown-hours cannot be negative")
    for option in (
        "airbnb_page_budget",
        "airbnb_pricing_budget",
        "airbnb_search_budget",
        "airbnb_image_budget",
        "booking_page_budget",
        "booking_search_budget",
        "booking_pricing_budget",
        "booking_image_budget",
        "travelstaytion_page_budget",
        "travelstaytion_pricing_budget",
        "travelstaytion_image_budget",
        "vrbo_page_budget",
        "vrbo_pricing_budget",
        "expedia_page_budget",
        "expedia_pricing_budget",
        "expedia_group_image_budget",
        "marriott_page_budget",
        "marriott_image_budget",
    ):
        if getattr(args, option) < 0:
            raise ValueError(f"--{option.replace('_', '-')} cannot be negative")
    budgets = dict(DEFAULT_REQUEST_BUDGETS)
    budgets[("airbnb", "page")] = args.airbnb_page_budget
    budgets[("airbnb", "pricing")] = args.airbnb_pricing_budget
    budgets[("airbnb", "search")] = args.airbnb_search_budget
    budgets[("airbnb", "image")] = args.airbnb_image_budget
    budgets[("booking", "page")] = args.booking_page_budget
    budgets[("booking", "search")] = args.booking_search_budget
    budgets[("booking", "pricing")] = args.booking_pricing_budget
    budgets[("booking", "image")] = args.booking_image_budget
    budgets[("travelstaytion", "page")] = args.travelstaytion_page_budget
    budgets[("travelstaytion", "pricing")] = args.travelstaytion_pricing_budget
    budgets[("travelstaytion", "image")] = args.travelstaytion_image_budget
    budgets[("vrbo_expedia", "page")] = args.vrbo_page_budget
    budgets[("vrbo_expedia", "pricing")] = args.vrbo_pricing_budget
    budgets[("expedia", "page")] = args.expedia_page_budget
    budgets[("expedia", "pricing")] = args.expedia_pricing_budget
    budgets[("expedia_group", "image")] = args.expedia_group_image_budget
    budgets[("marriott", "page")] = args.marriott_page_budget
    budgets[("marriott", "image")] = args.marriott_image_budget
    return budgets


@contextmanager
def _http_client(args: argparse.Namespace):
    """One governed client per CLI command; always prints honest request use."""
    if args.offline and args.record_fixtures:
        raise ValueError("--offline and --record-fixtures cannot be used together")
    with EvidenceStore(args.governor_db) as telemetry_store:
        governor = RequestGovernor(
            telemetry_store,
            budgets=_request_budgets(args),
            rate_limit_cooldown_seconds=args.rate_limit_cooldown_hours * 3600,
        )
        fixture_store = HttpFixtureStore(
            args.fixtures_dir,
            ttl_days=args.fixture_ttl_days,
        )
        # Ignore ambient proxy variables.  Offline mode must not initialise a
        # proxy transport, and live audits need a stable, explicit point of
        # sale rather than whatever VPN/proxy happens to be set in the shell.
        # PARITY_TRUST_ENV_PROXY=1 opts in for sandboxes whose only egress is
        # a configured proxy (e.g. cloud development containers).
        base_client = httpx.Client(
            timeout=25.0,
            follow_redirects=True,
            trust_env=os.environ.get("PARITY_TRUST_ENV_PROXY") == "1",
        )
        client = GovernedHttpClient(
            base_client,
            fixture_store,
            governor,
            offline=args.offline,
            record_fixtures=args.record_fixtures,
        )
        try:
            yield client
        finally:
            client.close()
            for line in governor.summary_lines():
                print(f"[parity] {line}", file=sys.stderr)


def _cmd_check(args: argparse.Namespace) -> int:
    if args.verified and (not args.verified_by or not args.verification_note):
        print("--verified requires both --verified-by and --verification-note", file=sys.stderr)
        return 2
    manual_verifications = {
        url: ManualVerification(
            url=url,
            reviewed_by=args.verified_by,
            note=args.verification_note,
        )
        for url in (args.verified or [])
    }
    candidate_urls = list(dict.fromkeys((args.against or []) + (args.verified or [])))
    all_urls = [args.subject] + candidate_urls
    with _http_client(args) as client:
        with EvidenceStore(args.hash_cache) as hash_cache:
            with _maybe_browser_session(
                all_urls,
                headless=args.headless,
                offline=args.offline,
                record_fixtures=args.record_fixtures,
                fixtures_dir=args.fixtures_dir,
                fixture_ttl_days=args.fixture_ttl_days,
                governor=client.governor,
            ) as browser_session:
                report = run_check(
                    subject_url=args.subject,
                    candidate_urls=candidate_urls,
                    checkin=_parse_date(args.checkin),
                    checkout=_parse_date(args.checkout),
                    guests=args.guests,
                    client=client,
                    browser_session=browser_session,
                    manual_verifications=manual_verifications,
                    photo_hash_cache=hash_cache,
                )
    print_report(report, fmt=args.format)
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    jobs = json.loads(open(args.config).read())
    exit_code = 0

    all_urls = [
        url
        for job in jobs
        for url in (
            [job["subject"]]
            + job.get("against", [])
            + [entry.get("url", "") for entry in job.get("verified_against", []) if isinstance(entry, dict)]
        )
        if url
    ]
    with _http_client(args) as client:
        with EvidenceStore(args.hash_cache) as hash_cache:
            with _maybe_browser_session(
                all_urls,
                headless=args.headless,
                offline=args.offline,
                record_fixtures=args.record_fixtures,
                fixtures_dir=args.fixtures_dir,
                fixture_ttl_days=args.fixture_ttl_days,
                governor=client.governor,
            ) as browser_session:
                for i, job in enumerate(jobs):
                    print(f"=== [{i + 1}/{len(jobs)}] {job['subject']} ===", file=sys.stderr)
                    try:
                        verified_entries = job.get("verified_against", [])
                        manual_verifications = {}
                        for entry in verified_entries:
                            if not isinstance(entry, dict) or not all(
                                entry.get(key) for key in ("url", "reviewed_by", "note")
                            ):
                                raise ValueError(
                                    "each verified_against entry requires url, reviewed_by and note"
                                )
                            manual_verifications[entry["url"]] = ManualVerification(
                                url=entry["url"],
                                reviewed_by=entry["reviewed_by"],
                                note=entry["note"],
                            )
                        candidate_urls = list(dict.fromkeys(
                            job.get("against", []) + list(manual_verifications)
                        ))
                        report = run_check(
                            subject_url=job["subject"],
                            candidate_urls=candidate_urls,
                            checkin=_parse_date(job["checkin"]),
                            checkout=_parse_date(job["checkout"]),
                            guests=job.get("guests", 2),
                            client=client,
                            browser_session=browser_session,
                            manual_verifications=manual_verifications,
                            photo_hash_cache=hash_cache,
                        )
                        print_report(report, fmt=args.format)
                        print()
                    except Exception as e:
                        print(f"FAILED: {job['subject']}: {e}", file=sys.stderr)
                        exit_code = 1
    return exit_code


def _portfolio_candidate_ref(job: dict, requested_currency: str) -> tuple[PropertyRef, PropertyRef]:
    """Load one exact persisted mapping for a portfolio comparison."""
    subject = _travelstaytion_subject_ref(job["subject"])
    subject.extra["quote_currency"] = requested_currency
    requested_site = job["channel"]
    with EvidenceStore(job["db"]) as store:
        candidates = [
            ref for ref in store.load_verified_refs(subject)
            if ref.site == requested_site
        ]

    def normalise(site: str, url: str) -> str:
        if site == "booking":
            return canonical_booking_url(url)
        return canonicalize_url(url)

    wanted = normalise(requested_site, job["candidate_url"])
    candidates = [
        ref for ref in candidates
        if normalise(requested_site, ref.url) == wanted
    ]
    if not candidates:
        raise ValueError(
            f"no unexpired verified {job['channel']} mapping matching "
            f"{job['candidate_url']} exists for {job['subject']}; run parity verify first"
        )
    if len(candidates) > 1:
        raise ValueError(
            f"{len(candidates)} verified {job['channel']} mappings exist for the exact URL"
        )
    candidate = candidates[0]
    candidate.extra["quote_currency"] = requested_currency
    return subject, candidate


def _calendar_preferred_windows(
    subject_adapter, subject, windows, today, min_lead_days, *, fail_when_closed=False
):
    """Use the subject's open calendar dates when it publishes them.

    With ``fail_when_closed`` a calendar that publishes no open check-in at
    all (TravelStaytion's covers two years) stops the check with a plain
    reason instead of probing weeks that cannot be booked.
    """
    reader = getattr(subject_adapter, "open_checkin_dates", None)
    if reader is None:
        return windows
    try:
        open_dates = reader(subject)
    except (OfflineFixtureError, RequestDeferred):
        raise
    except Exception as error:
        print(f"[parity] calendar unavailable ({error}); using weekly windows", file=sys.stderr)
        return windows
    if fail_when_closed and open_dates is not None and not open_dates:
        raise ValueError(
            "TravelStaytion's calendar has no open check-in dates at all, so this "
            "listing cannot be booked or compared right now (closed or fully booked)"
        )
    preferred = prefer_open_checkins(
        windows, open_dates, earliest=today + timedelta(days=min_lead_days)
    )
    if preferred is not windows:
        print(
            "[parity] using open TravelStaytion check-in dates from its calendar: "
            + ", ".join(window.checkin.isoformat() for window in preferred),
            file=sys.stderr,
        )
    elif open_dates is not None:
        print(
            f"[parity] calendar has no open check-in from {min_lead_days} days out; "
            "using weekly windows",
            file=sys.stderr,
        )
    return windows if preferred is windows else preferred


def _select_portfolio_shared_minimum(
    listing: dict,
    listing_jobs: list[dict],
    args: argparse.Namespace,
    work_dir: Path,
) -> dict:
    """Select and price one stay shared by TS, Airbnb and Booking.com."""
    if args.offline:
        raise ValueError("shared automatic minimum-stay selection requires live checks")
    requested_currency = normalise_currency_code(listing["currency"])
    subject: PropertyRef | None = None
    candidates: dict[str, PropertyRef] = {}
    for job in listing_jobs:
        loaded_subject, candidate = _portfolio_candidate_ref(job, requested_currency)
        if subject is None:
            subject = loaded_subject
        candidates[job["channel"]] = candidate
    assert subject is not None

    if "booking" in candidates:
        if not args.enable_booking_pricing_capture:
            raise ValueError(
                "live Booking pricing capture is locked; add "
                "--enable-booking-pricing-capture for this controlled portfolio run"
            )
        if not args.record_fixtures:
            raise ValueError(
                "--enable-booking-pricing-capture requires --record-fixtures"
            )
        candidates["booking"].extra["booking_pricing_capture_enabled"] = True

    today = operating_today(args.date_timezone)
    windows = candidate_date_windows(
        today=today,
        min_lead_days=args.min_lead_days,
        target_lead_days=listing["target_lead_days"],
        stay_nights=1,
        search_weeks=listing["auto_date_search_weeks"],
    )
    fixtures_dir = work_dir / "shared-fixtures" / listing["listing_id"]
    governor_db = work_dir / "governors" / f"{listing['listing_id']}-shared.db"
    shared_values = dict(vars(args))
    shared_values.update({
        "fixtures_dir": str(fixtures_dir),
        "governor_db": str(governor_db),
    })
    shared_args = argparse.Namespace(**shared_values)

    with _http_client(shared_args) as client:
        subject_adapter = get_http_adapter(subject.site, client=client)
        windows = _calendar_preferred_windows(
            subject_adapter, subject, windows, today, args.min_lead_days,
            fail_when_closed=True,
        )

        def subject_probe(window):
            restriction_reader = getattr(subject_adapter, "minimum_stay_for_checkin", None)
            calendar_minimum = (
                restriction_reader(subject, window.checkin)
                if window.nights == 1 and restriction_reader is not None
                else None
            )
            if calendar_minimum is not None and calendar_minimum > window.nights:
                result = PriceResult(
                    site=subject.site,
                    ref=subject,
                    outcome=Outcome.MIN_STAY,
                    checkin=window.checkin,
                    checkout=window.checkout,
                    guests=listing["guests"],
                    minimum_stay_nights=calendar_minimum,
                    notes=(
                        "TravelStaytion public calendar requires a minimum stay of "
                        f"{calendar_minimum} nights"
                    ),
                )
            else:
                result = subject_adapter.get_price(
                    subject,
                    window.checkin,
                    window.checkout,
                    listing["guests"],
                )
            print(
                "[parity] shared minimum-stay TravelStaytion probe "
                f"{window.checkin.isoformat()} to {window.checkout.isoformat()}: "
                f"{result.outcome.value}"
                f"{f' (minimum {result.minimum_stay_nights} nights)' if result.minimum_stay_nights else ''}"
                f"{' -- ' + result.notes if result.notes else ''}",
                file=sys.stderr,
            )
            return result

        with _maybe_browser_session(
            [candidate.url for candidate in candidates.values()],
            args.headless,
            offline=False,
            record_fixtures=args.record_fixtures,
            fixtures_dir=str(fixtures_dir),
            fixture_ttl_days=args.fixture_ttl_days,
            governor=client.governor,
        ) as browser_session:
            adapters = {
                channel: (
                    get_browser_adapter(candidate.site, session=browser_session)
                    if candidate.site == "booking"
                    else get_http_adapter(candidate.site, client=client)
                )
                for channel, candidate in candidates.items()
            }
            candidate_probes = []
            for channel in sorted(candidates):
                candidate = candidates[channel]
                adapter = adapters[channel]

                def probe(window, *, _channel=channel, _candidate=candidate, _adapter=adapter):
                    result = _adapter.get_price(
                        _candidate,
                        window.checkin,
                        window.checkout,
                        listing["guests"],
                    )
                    print(
                        f"[parity] shared minimum-stay {_channel} probe "
                        f"{window.checkin.isoformat()} to {window.checkout.isoformat()}: "
                        f"{result.outcome.value}"
                        f"{f' (minimum {result.minimum_stay_nights} nights)' if result.minimum_stay_nights else ''}"
                        f"{' -- ' + result.notes if result.notes else ''}",
                        file=sys.stderr,
                    )
                    return result

                candidate_probes.append((channel, probe))

            selection = select_multi_channel_minimum_stay_window(
                windows,
                subject_probe,
                candidate_probes,
                max_adjustments=listing["minimum_stay_max_adjustments"],
            )

    apply_quote_evidence_policy(selection.subject_price, requested_currency)
    for price in selection.candidate_prices.values():
        apply_quote_evidence_policy(price, requested_currency)
    date_policy = {
        "mode": "shared_automatic_minimum_stay",
        "operating_date": today.isoformat(),
        "timezone": args.date_timezone,
        "minimum_lead_days": args.min_lead_days,
        "target_lead_days": listing["target_lead_days"],
        "comparison_stay_nights": selection.minimum_stay_nights,
        "subject_minimum_stay_nights": selection.subject_minimum_stay_nights,
        "candidate_minimum_stay_nights": selection.candidate_minimum_stay_nights,
        "search_weeks": listing["auto_date_search_weeks"],
        "selected_window_index": selection.selected_index,
        "actual_lead_days": (selection.window.checkin - today).days,
        "probes": [
            {
                "index": item.index,
                "site": item.site,
                "checkin": item.window.checkin.isoformat(),
                "checkout": item.window.checkout.isoformat(),
                "outcome": item.outcome.value,
                "minimum_stay_nights": item.minimum_stay_nights,
                "notes": item.notes,
            }
            for item in selection.probes
        ],
    }
    for candidate in candidates.values():
        candidate.extra["date_policy"] = date_policy
    for label, reason in selection.dropped.items():
        print(f"[parity] {label} left out of the shared stay: {reason}", file=sys.stderr)
    print(
        "[parity] shared stay selected for TravelStaytion, Airbnb and Booking: "
        f"{selection.window.checkin.isoformat()} to "
        f"{selection.window.checkout.isoformat()}; "
        f"{selection.minimum_stay_nights} night(s); "
        f"window {selection.selected_index}/{len(windows)}",
        file=sys.stderr,
    )
    return {
        "selection": selection,
        "subject": subject,
        "candidates": candidates,
        "fixtures_dir": str(fixtures_dir),
    }


def _resume_report(
    job: dict,
    execution: dict,
    quote_file: Path,
    state_file: Path,
) -> dict | None:
    if not quote_file.is_file() or not state_file.is_file():
        return None
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        report = json.loads(quote_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        state.get("state_version") == PORTFOLIO_STATE_VERSION
        and state.get("fingerprint") == job_fingerprint(job, execution)
        and state.get("status") in {"PRICED", "UNAVAILABLE"}
        and state.get("quote_sha256") == file_sha256(quote_file)
        and isinstance(report, dict)
    ):
        return report
    return None


def _cmd_portfolio(args: argparse.Namespace) -> int:
    """Run up to ten persisted, verified quote mappings with safe resume."""
    config_path = Path(args.config)
    config = load_portfolio_config(config_path)
    listings, jobs = expand_portfolio_jobs(config, config_path=config_path)
    if args.offline and any(job["auto_minimum_stay"] for job in jobs):
        raise ValueError(
            "offline portfolio replay requires explicit checkin and checkout dates"
        )

    work_dir = Path(args.work_dir).resolve()
    quote_dir = work_dir / "quotes"
    state_dir = work_dir / "state"
    governor_dir = work_dir / "governors"
    for directory in (quote_dir, state_dir, governor_dir):
        directory.mkdir(parents=True, exist_ok=True)

    execution = {
        "state_version": PORTFOLIO_STATE_VERSION,
        "offline": args.offline,
        "record_fixtures": args.record_fixtures,
        "fixture_ttl_days": args.fixture_ttl_days,
        "date_timezone": args.date_timezone,
        "min_lead_days": args.min_lead_days,
        "rate_limit_cooldown_hours": args.rate_limit_cooldown_hours,
        "enable_booking_pricing_capture": args.enable_booking_pricing_capture,
        "budgets": _request_budgets(args),
    }
    if not args.offline and any(job["auto_minimum_stay"] for job in jobs):
        execution["operating_date"] = operating_today(args.date_timezone).isoformat()
    # JSON object keys cannot be tuples; preserve the exact budget values in a
    # stable text-keyed form for the resume fingerprint.
    execution["budgets"] = {
        f"{site}:{kind}": value
        for (site, kind), value in execution["budgets"].items()
    }

    jobs_by_listing = {
        listing["listing_id"]: [
            job for job in jobs if job["listing_id"] == listing["listing_id"]
        ]
        for listing in listings
    }
    shared_selections: dict[str, dict] = {}
    shared_resume_reports: dict[str, dict[str, dict]] = {}
    shared_failures: dict[str, str] = {}
    for listing in listings:
        listing_jobs = jobs_by_listing[listing["listing_id"]]
        if not listing["auto_minimum_stay"] or len(listing_jobs) <= 1:
            continue
        resumable: dict[str, dict] = {}
        if not args.force:
            for job in listing_jobs:
                quote_file = quote_dir / f"{job['job_id']}.json"
                state_file = state_dir / f"{job['job_id']}.json"
                report = _resume_report(job, execution, quote_file, state_file)
                if report is not None:
                    resumable[job["job_id"]] = report
        if len(resumable) == len(listing_jobs):
            shared_resume_reports[listing["listing_id"]] = resumable
            continue
        try:
            shared_selections[listing["listing_id"]] = (
                _select_portfolio_shared_minimum(
                    listing,
                    listing_jobs,
                    args,
                    work_dir,
                )
            )
        except (OfflineFixtureError, RequestDeferred, ValueError, OSError) as error:
            shared_failures[listing["listing_id"]] = str(error)

    results = []
    for position, job in enumerate(jobs, start=1):
        quote_file = quote_dir / f"{job['job_id']}.json"
        state_file = state_dir / f"{job['job_id']}.json"
        fingerprint = job_fingerprint(job, execution)
        print(
            f"[parity] portfolio job {position}/{len(jobs)}: "
            f"{job['listing_name']} / {job['channel']}",
            file=sys.stderr,
        )

        report_data = (
            shared_resume_reports.get(job["listing_id"], {}).get(job["job_id"])
        )
        if report_data is None and not args.force and job["listing_id"] not in shared_selections:
            report_data = _resume_report(job, execution, quote_file, state_file)
        if report_data is not None:
            result = result_from_quote(
                job,
                report_data,
                output_file=quote_file,
                resumed=True,
            )
            results.append(result)
            print(
                f"[parity] portfolio resume hit: {job['job_id']} "
                f"({result['status']})",
                file=sys.stderr,
            )
            continue

        quote_values = dict(vars(args))
        quote_values.update({
            "subject": job["subject"],
            "channel": job["channel"],
            "candidate_url": job["candidate_url"],
            "db": job["db"],
            "checkin": job["checkin"],
            "checkout": job["checkout"],
            "auto_dates": False,
            "auto_minimum_stay": job["auto_minimum_stay"],
            "target_lead_days": job["target_lead_days"],
            "stay_nights": 7,
            "auto_date_search_weeks": job["auto_date_search_weeks"],
            "minimum_stay_max_adjustments": job["minimum_stay_max_adjustments"],
            "guests": job["guests"],
            "currency": job["currency"],
            "format": "json",
            "output": str(quote_file),
            "fixtures_dir": job["fixtures_dir"] or args.fixtures_dir,
            "governor_db": str(governor_dir / f"{job['job_id']}.db"),
            "enable_expedia_group_pricing_capture": False,
        })
        try:
            if job["listing_id"] in shared_failures:
                raise ValueError(shared_failures[job["listing_id"]])
            shared = shared_selections.get(job["listing_id"])
            result_job = job
            if shared is not None:
                selection = shared["selection"]
                if job["channel"] in selection.dropped:
                    raise ValueError(
                        f"{job['channel']} could not be priced, so it was left out "
                        f"while the other sites were compared: {selection.dropped[job['channel']]}"
                    )
                candidate = shared["candidates"][job["channel"]]
                price = selection.candidate_prices[job["channel"]]
                candidate_minimum = selection.candidate_minimum_stay_nights[
                    job["channel"]
                ]
                report = QuoteReport(
                    subject=shared["subject"],
                    candidate=candidate,
                    price=price,
                    subject_price=selection.subject_price,
                    comparison=compare_aligned_prices(selection.subject_price, price),
                    minimum_stay_nights=selection.minimum_stay_nights,
                    comparison_stay_nights=selection.minimum_stay_nights,
                    subject_minimum_stay_nights=(
                        selection.subject_minimum_stay_nights
                    ),
                    candidate_minimum_stay_nights=candidate_minimum,
                    candidate_minimum_stay_evidence=(
                        "exact"
                        if candidate_minimum is not None
                        else "accepted_comparison_duration_only"
                    ),
                    discount_scenarios=build_discount_scenarios(
                        price,
                        selection.subject_price,
                    ),
                )
                quote_file.write_text(
                    quote_to_json(report) + "\n",
                    encoding="utf-8",
                )
                print(f"wrote {quote_file} as UTF-8", file=sys.stderr)
                print(quote_verdict_to_console(report), file=sys.stderr)
                report_data = json.loads(quote_file.read_text(encoding="utf-8"))
                result_job = dict(job)
                result_job["fixtures_dir"] = shared["fixtures_dir"]
            else:
                _cmd_quote(argparse.Namespace(**quote_values))
                report_data = json.loads(quote_file.read_text(encoding="utf-8"))
            result = result_from_quote(
                result_job,
                report_data,
                output_file=quote_file,
                resumed=False,
            )
            if result["status"] in {"PRICED", "UNAVAILABLE"}:
                state_file.write_text(
                    json.dumps(
                        {
                            "state_version": PORTFOLIO_STATE_VERSION,
                            "fingerprint": fingerprint,
                            "status": result["status"],
                            "quote_sha256": file_sha256(quote_file),
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        except (OfflineFixtureError, RequestDeferred, ValueError, OSError) as error:
            message = str(error)
            status = (
                "IDENTITY_FAILED"
                if "no unexpired verified" in message
                else "PRICE_FAILED"
            )
            result = failed_result(job, status, message)
            print(
                f"[parity] portfolio {status}: {job['job_id']} -- {message}",
                file=sys.stderr,
            )
        results.append(result)

    manifest = portfolio_manifest(
        host=config["host"],
        config_path=config_path,
        listings=listings,
        results=results,
    )
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    csv_path = output_path.with_suffix(".csv")
    csv_path.write_text(portfolio_csv(manifest), encoding="utf-8", newline="")
    print(f"wrote {output_path} as UTF-8", file=sys.stderr)
    print(f"wrote {csv_path} as UTF-8", file=sys.stderr)
    counts = ", ".join(
        f"{status}={count}" for status, count in sorted(manifest["status_counts"].items())
    )
    print(
        f"[parity] PORTFOLIO: {manifest['listing_count']} listing(s), "
        f"{manifest['comparison_count']} comparison(s); {counts}",
        file=sys.stderr,
    )
    return 1 if any(
        result["status"] in {"IDENTITY_FAILED", "PRICE_FAILED", "REVIEW_REQUIRED"}
        for result in results
    ) else 0


def _cmd_discover(args: argparse.Namespace) -> int:
    _, subject_url = normalise_travelstaytion_listing(args.subject)
    with _http_client(args) as client:
        subject_match = resolve(subject_url)
        if subject_match is None or subject_match.needs_browser:
            print("discover currently requires an HTTP-identifiable subject URL", file=sys.stderr)
            return 2
        subject = get_http_adapter(subject_match.site, client=client).identify(subject_url)

        with EvidenceStore(args.db) as store:
            providers = [KnownMappingsProvider(store)]
            requested_channels = set(args.channels)
            unsupported = requested_channels - {"airbnb", "booking"}
            if unsupported:
                print(
                    f"unsupported discovery channel(s): {', '.join(sorted(unsupported))}; "
                    "currently implemented: airbnb, booking",
                    file=sys.stderr,
                )
                return 2
            if "airbnb" in requested_channels:
                providers.append(AirbnbMapProvider(client))
            if requested_channels:
                with _maybe_browser_session(
                    ["https://www.booking.com/"],
                    headless=args.headless,
                    offline=args.offline,
                    record_fixtures=args.record_fixtures,
                    fixtures_dir=args.fixtures_dir,
                    fixture_ttl_days=args.fixture_ttl_days,
                    governor=client.governor,
                    interactive=args.interactive_captcha and not args.headless,
                ) as browser_session:
                    if not args.no_web_search:
                        providers.insert(1, WebLicenceSearchProvider(
                            browser_session,
                            interactive=args.interactive_captcha and not args.headless,
                            notify=lambda message: print(message, file=sys.stderr),
                        ))
                    if "booking" in requested_channels:
                        if subject.lat is not None and subject.lng is not None:
                            providers.append(BookingMapProvider(browser_session))
                        providers.append(BookingSearchProvider(browser_session))
                    report = DiscoveryEngine(
                        providers,
                        limit_per_site=args.limit_per_site,
                    ).discover(subject)
            else:
                report = DiscoveryEngine(
                    providers,
                    limit_per_site=args.limit_per_site,
                ).discover(subject)
            store.save_candidates(subject, report.candidates)

    rendered = discovery_to_json(report) if args.format == "json" else discovery_to_console(report)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"wrote {output_path} as UTF-8", file=sys.stderr)
    else:
        print(rendered)
    return 0


_CHANNEL_PROVIDERS = {
    "airbnb": ("web_licence_search", "airbnb_map_search"),
    "booking": ("web_licence_search", "booking_map_search", "booking_public_search"),
}


def _channel_discovery_summary(discovery, channel: str) -> str:
    """Explain a channel with no candidate: which searches ran and how each
    ended, so a block, a layout change and an honest zero read differently."""
    parts = []
    for name in _CHANNEL_PROVIDERS.get(channel, ()):
        if name in discovery.provider_errors:
            parts.append(f"{name} FAILED: {discovery.provider_errors[name]}")
        elif name in discovery.provider_diagnostics:
            diagnostic = discovery.provider_diagnostics[name].split(" | ", 1)[0]
            parts.append(f"{name}: {diagnostic}")
    if not parts:
        return f"No likely {channel} listing was found."
    return f"No likely {channel} listing was found. " + "; ".join(parts)


def _verify_ranked_candidates(
    client, subject, subject_url, report, browser_session, hash_store, store
):
    """Identity-check ranked, not-yet-verified candidates; persist proofs."""
    ranked = ranked_candidates_per_channel(report)
    refs = [
        candidate.ref
        for channel in ("airbnb", "booking")
        for candidate in ranked[channel]
        if candidate.ref.extra.get("verified_mapping") is not True
    ]
    if not refs:
        return []
    _, matches = identify_and_match(
        client=client,
        subject_url=subject_url,
        candidate_urls=[ref.url for ref in refs],
        browser_session=browser_session,
        photo_hash_cache=hash_store,
        stop_after_verified_sites=True,
    )
    for match in matches:
        if match.is_verified():
            store.save_verified_mapping(subject, match)
    return matches


def _run_web_licence_search(args, subject, browser_session, known_sites, providers) -> set[str]:
    """Run licence-first web search once; return the channels it surfaced."""
    if getattr(args, "no_web_search", False) or browser_session is None:
        return set()
    if {"airbnb", "booking"} <= set(known_sites):
        return set()
    provider = WebLicenceSearchProvider(
        browser_session,
        interactive=bool(getattr(args, "interactive_captcha", False)) and not args.headless,
        skip_sites=set(known_sites),
        notify=lambda message: print(message, file=sys.stderr),
    )
    print(
        "[parity] searching the web for the licence number"
        f" ({', '.join(registration_variants(subject)) or 'none published; title search only'})",
        file=sys.stderr,
    )
    try:
        result = provider.discover(subject)
    except Exception as error:  # reported in diagnostics; fallbacks still run
        print(f"[parity] web licence search failed: {error}", file=sys.stderr)
        providers.append(PrecomputedProvider(provider.name, None, error=str(error)))
        return set()
    for outcome in provider.outcomes:
        print(f"[parity] web search: {outcome.line()}", file=sys.stderr)
    providers.append(PrecomputedProvider(provider.name, result))
    return {ref.site for ref in result.refs}


def _automatic_placeholder_result(
    *,
    listing_id: str,
    listing_name: str,
    subject_url: str,
    channel: str,
    status: str,
    candidate_url: str | None,
    currency: str,
    guests: int,
    error: str,
    review_candidates: list[dict] | None = None,
) -> dict:
    return {
        "job_id": f"{listing_id}-{channel}",
        "listing_id": listing_id,
        "listing_name": listing_name,
        "subject_url": subject_url,
        "channel": channel,
        "candidate_url": candidate_url,
        "status": status,
        "checkin": None,
        "checkout": None,
        "nights": None,
        "guests": guests,
        "currency": currency,
        "travelstaytion_guest_total": None,
        "ota_guest_total": None,
        "difference": None,
        "difference_percent": None,
        "verdict": None,
        "breakdown_status": None,
        "discount_scenarios": [],
        "quote_output": None,
        "fixtures_dir": None,
        "resumed": False,
        "error": error,
        "review_candidates": review_candidates or [],
    }


def _cmd_auto(args: argparse.Namespace) -> int:
    """Discover, verify and price one to ten TravelStaytion listing IDs."""
    requested = normalise_listing_inputs(args.listings)
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    mapping_db = work_dir / "verified-mappings.db"
    hash_db = work_dir / "photo-hashes.db"
    config_path = work_dir / "portfolio.json"
    output_path = Path(args.output).resolve()
    fixtures_dir = Path(args.fixtures_dir)
    if args.fixtures_dir == ".parity/fixtures":
        fixtures_dir = work_dir / "fixtures"
        args.fixtures_dir = str(fixtures_dir)

    records: list[dict] = []
    evidence_root = evidence_root_for(work_dir)
    args.record_fixtures = True
    with _http_client(args) as client:
        with EvidenceStore(mapping_db) as store, EvidenceStore(hash_db) as hash_store:
            with _maybe_browser_session(
                ["https://www.booking.com/"],
                headless=args.headless,
                offline=False,
                record_fixtures=True,
                fixtures_dir=str(fixtures_dir),
                fixture_ttl_days=args.fixture_ttl_days,
                governor=client.governor,
                interactive=args.interactive_captcha and not args.headless,
            ) as browser_session:
                for position, (raw_id, subject_url) in enumerate(requested, start=1):
                    print(
                        f"[parity] automatic listing {position}/{len(requested)}: "
                        f"TravelStaytion {raw_id}",
                        file=sys.stderr,
                    )
                    subject = get_http_adapter(
                        "travelstaytion", client=client
                    ).identify(subject_url)
                    currency = infer_listing_currency(subject, args.currency)
                    known = store.load_verified_refs(subject)
                    known_sites = {ref.site for ref in known}
                    providers = [KnownMappingsProvider(store)]
                    historical = load_historical_candidate_refs(
                        evidence_root,
                        subject,
                        # The configured governor database may also be an older
                        # all-purpose evidence store, so it remains eligible.
                        exclude={mapping_db, hash_db},
                    )
                    if historical:
                        print(
                            f"[parity] prior local identity candidates: {len(historical)}",
                            file=sys.stderr,
                        )
                        providers.append(HistoricalEvidenceProvider(historical))
                    # Each channel's own map/search first: it needs no search
                    # engine and works for listings nobody has checked before.
                    if "airbnb" in args.channels and "airbnb" not in known_sites:
                        providers.append(AirbnbMapProvider(client, photo_hash_cache=hash_store))
                    if "booking" in args.channels and "booking" not in known_sites:
                        if subject.lat is not None and subject.lng is not None:
                            providers.append(BookingMapProvider(browser_session))
                        providers.append(BookingSearchProvider(browser_session))
                    discovery = DiscoveryEngine(
                        providers,
                        limit_per_site=args.limit_per_site,
                    ).discover(subject)
                    matches = _verify_ranked_candidates(
                        client, subject, subject_url, discovery, browser_session, hash_store, store
                    )
                    verified_sites = set(known_sites) | {
                        match.ref.site for match in matches if match.is_verified()
                    }
                    # Web search is the last resort, only for unmatched channels.
                    excluded = {"airbnb", "booking"} - set(args.channels)
                    missing = set(args.channels) - verified_sites
                    if missing:
                        web_providers: list = []
                        _run_web_licence_search(
                            args, subject, browser_session, verified_sites | excluded, web_providers
                        )
                        if web_providers:
                            checked = {canonicalize_url(match.ref.url) for match in matches}
                            web_report = DiscoveryEngine(
                                web_providers, limit_per_site=args.limit_per_site
                            ).discover(subject)
                            discovery.provider_diagnostics.update(web_report.provider_diagnostics)
                            discovery.provider_errors.update(web_report.provider_errors)
                            known_urls = {canonicalize_url(c.ref.url) for c in discovery.candidates}
                            additions = [
                                candidate
                                for candidate in web_report.candidates
                                if candidate.ref.site in missing
                                and canonicalize_url(candidate.ref.url) not in known_urls
                                and canonicalize_url(candidate.ref.url) not in checked
                            ]
                            if additions:
                                discovery.candidates.extend(additions)
                                discovery.candidates.sort(
                                    key=lambda item: (item.ref.site, -item.score, item.ref.url)
                                )
                                matches.extend(_verify_ranked_candidates(
                                    client,
                                    subject,
                                    subject_url,
                                    DiscoveryReport(subject=subject, candidates=additions),
                                    browser_session,
                                    hash_store,
                                    store,
                                ))
                    store.save_candidates(subject, discovery.candidates)
                    ranked = ranked_candidates_per_channel(discovery)
                    matches_by_url = {
                        canonicalize_url(match.ref.url): match for match in matches
                    }
                    channels = []
                    placeholders = []
                    name = subject.title or f"TravelStaytion {raw_id}"
                    local_listing_id = f"auto-{raw_id}"
                    for channel in args.channels:
                        channel_candidates = ranked[channel]
                        review_candidates = [
                            {
                                "url": item.ref.url,
                                "title": item.ref.title,
                                "score": item.score,
                                "identity_status": (
                                    "verified_mapping"
                                    if item.ref.extra.get("verified_mapping") is True
                                    else (
                                        matches_by_url[canonicalize_url(item.ref.url)].decision.value
                                        if canonicalize_url(item.ref.url) in matches_by_url
                                        else "not_checked_after_verified"
                                    )
                                ),
                            }
                            for item in channel_candidates
                        ][: args.limit_per_site]
                        verified_choice = first_verified_candidate(
                            channel_candidates, matches_by_url
                        )
                        review_choice = best_review_candidate(
                            channel_candidates, matches_by_url
                        )
                        choice = verified_choice or review_choice
                        if choice is None:
                            detail = _channel_discovery_summary(discovery, channel)
                            placeholders.append(_automatic_placeholder_result(
                                listing_id=local_listing_id,
                                listing_name=name,
                                subject_url=subject_url,
                                channel=channel,
                                status="NOT_FOUND",
                                candidate_url=None,
                                currency=currency,
                                guests=args.guests,
                                error=detail or f"No likely {channel} listing was found.",
                                review_candidates=review_candidates,
                            ))
                            continue
                        candidate, match = choice
                        if verified_choice is not None:
                            channels.append({
                                "channel": channel,
                                "candidate_url": candidate.ref.url,
                                "db": str(mapping_db),
                            })
                            continue
                        evidence = "; ".join(
                            item.detail for item in (match.evidence if match else [])
                        )
                        placeholders.append(_automatic_placeholder_result(
                            listing_id=local_listing_id,
                            listing_name=name,
                            subject_url=subject_url,
                            channel=channel,
                            status="IDENTITY_UNVERIFIED",
                            candidate_url=candidate.ref.url,
                            currency=currency,
                            guests=args.guests,
                            error=(
                                "Likely match found, but identity was not proven; "
                                "no price was requested."
                                + (f" {evidence}" if evidence else "")
                            ),
                            review_candidates=review_candidates,
                        ))
                    records.append({
                        "raw_id": raw_id,
                        "name": name,
                        "subject": subject_url,
                        "currency": currency,
                        "channels": channels,
                        "placeholders": placeholders,
                    })

    priced_records = [record for record in records if record["channels"]]
    manifest = {
        "schema_version": 1,
        "host": args.host,
        "config_path": str(config_path),
        "listing_count": 0,
        "comparison_count": 0,
        "status_counts": {},
        "listings": [],
        "results": [],
    }
    if priced_records:
        config = {
            "host": args.host,
            "defaults": {
                "guests": args.guests,
                "target_lead_days": args.target_lead_days,
                "auto_date_search_weeks": args.auto_date_search_weeks,
                "minimum_stay_max_adjustments": args.minimum_stay_max_adjustments,
            },
            "listings": [
                {
                    "name": record["name"],
                    "subject": record["subject"],
                    "currency": record["currency"],
                    "auto_minimum_stay": True,
                    "channels": record["channels"],
                }
                for record in priced_records
            ],
        }
        config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        portfolio_values = dict(vars(args))
        portfolio_values.update({
            "config": str(config_path),
            "output": str(output_path),
            "work_dir": str(work_dir / "portfolio"),
            "force": args.force,
            "offline": False,
            "record_fixtures": True,
            "enable_booking_pricing_capture": True,
        })
        _cmd_portfolio(argparse.Namespace(**portfolio_values))
        manifest = json.loads(output_path.read_text(encoding="utf-8"))

    listing_by_subject = {
        listing["subject"]: listing for listing in manifest.get("listings", [])
    }
    for record in records:
        listing = listing_by_subject.get(record["subject"])
        if listing is None:
            listing = {
                "listing_id": f"auto-{record['raw_id']}",
                "name": record["name"],
                "subject": record["subject"],
                "currency": record["currency"],
                "guests": args.guests,
                "checkin": None,
                "checkout": None,
                "auto_minimum_stay": True,
                "target_lead_days": args.target_lead_days,
                "auto_date_search_weeks": args.auto_date_search_weeks,
                "minimum_stay_max_adjustments": args.minimum_stay_max_adjustments,
                "channels": record["channels"],
            }
            manifest.setdefault("listings", []).append(listing)
            listing_by_subject[record["subject"]] = listing
        for placeholder in record["placeholders"]:
            placeholder["listing_id"] = listing["listing_id"]
            placeholder["job_id"] = f"{listing['listing_id']}-{placeholder['channel']}"
            manifest.setdefault("results", []).append(placeholder)

    counts: dict[str, int] = {}
    for result in manifest["results"]:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    manifest["listing_count"] = len(manifest["listings"])
    manifest["comparison_count"] = len(manifest["results"])
    manifest["status_counts"] = counts
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    output_path.with_suffix(".csv").write_text(
        portfolio_csv(manifest), encoding="utf-8", newline=""
    )
    dashboard_path = Path(args.dashboard_output).resolve()
    write_dashboard(
        output_path,
        dashboard_path,
        quotes_dir=work_dir / "portfolio" / "quotes",
    )
    print(f"wrote {dashboard_path} as UTF-8", file=sys.stderr)
    if args.compact and not _open_compact_dashboard(dashboard_path):
        webbrowser.open(dashboard_path.as_uri())
    print(
        f"[parity] AUTOMATIC: {manifest['listing_count']} listing(s); "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())),
        file=sys.stderr,
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    """Verify identity without requesting or comparing any prices."""
    _, subject_url = normalise_travelstaytion_listing(args.subject)
    candidate_urls = list(dict.fromkeys(args.against))
    all_urls = [subject_url] + candidate_urls
    with _http_client(args) as client:
        with EvidenceStore(args.hash_cache) as hash_cache:
            with _maybe_browser_session(
                all_urls,
                headless=args.headless,
                offline=args.offline,
                record_fixtures=args.record_fixtures,
                fixtures_dir=args.fixtures_dir,
                fixture_ttl_days=args.fixture_ttl_days,
                governor=client.governor,
            ) as browser_session:
                subject, matches = identify_and_match(
                    client=client,
                    subject_url=subject_url,
                    candidate_urls=candidate_urls,
                    browser_session=browser_session,
                    photo_hash_cache=hash_cache,
                )

    with EvidenceStore(args.db) as store:
        for match in matches:
            if match.is_verified():
                store.save_verified_mapping(subject, match)

    report = IdentityReport(subject=subject, matches=matches)
    rendered = identity_to_json(report) if args.format == "json" else identity_to_console(report)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"wrote {output_path} as UTF-8", file=sys.stderr)
        verdicts = identity_verdicts_to_console(report)
        if verdicts:
            print(verdicts, file=sys.stderr)
    else:
        print(rendered)
    return 0


def _travelstaytion_subject_ref(url: str) -> PropertyRef:
    _, url = normalise_travelstaytion_listing(url)
    match = resolve(url)
    path_match = re.fullmatch(r"/stay/(\d+)/?", urlparse(url).path)
    if match is None or match.site != "travelstaytion" or path_match is None:
        raise ValueError("quote currently requires a TravelStaytion /stay/<id> subject URL")
    return PropertyRef(site="travelstaytion", raw_id=path_match.group(1), url=url)


def _cmd_quote(args: argparse.Namespace) -> int:
    """Price one persisted verified mapping without repeating identity work."""
    subject_price = None
    comparison = None
    selected_minimum_stay = None
    subject_minimum_stay = None
    candidate_minimum_stay = None
    candidate_minimum_evidence = None
    requested_currency = normalise_currency_code(args.currency)
    if args.guests <= 0:
        raise ValueError("--guests must be greater than zero")
    if args.auto_dates and args.auto_minimum_stay:
        raise ValueError("choose either --auto-dates or --auto-minimum-stay, not both")
    if args.auto_minimum_stay and args.channel not in {"airbnb", "booking"}:
        raise ValueError(
            "--auto-minimum-stay currently supports only Airbnb and Booking.com"
        )
    automatic_dates = args.auto_dates or args.auto_minimum_stay
    if automatic_dates:
        if args.checkin or args.checkout:
            raise ValueError(
                "automatic date selection cannot be combined with --checkin or --checkout"
            )
        if args.offline:
            raise ValueError("automatic date selection requires live availability checks")
        if (
            args.auto_dates
            and args.auto_date_search_weeks > args.travelstaytion_pricing_budget
        ):
            raise ValueError(
                "--auto-date-search-weeks cannot exceed "
                "--travelstaytion-pricing-budget"
            )
        checkin = None
        checkout = None
    else:
        if not args.checkin or not args.checkout:
            raise ValueError(
                "provide both --checkin and --checkout, or use an automatic date mode"
            )
        checkin = _parse_date(args.checkin)
        checkout = _parse_date(args.checkout)
        if checkout <= checkin:
            raise ValueError("--checkout must be after --checkin")

    today = operating_today(args.date_timezone) if not args.offline else None
    date_policy_evidence = None
    if not args.offline and not automatic_dates:
        assert checkin is not None and checkout is not None and today is not None
        validate_live_date_range(
            checkin,
            checkout,
            today=today,
            min_lead_days=args.min_lead_days,
        )
        date_policy_evidence = {
            "mode": "explicit",
            "operating_date": today.isoformat(),
            "timezone": args.date_timezone,
            "minimum_lead_days": args.min_lead_days,
            "actual_lead_days": (checkin - today).days,
        }
        print(
            "[parity] date policy accepted: "
            f"{checkin.isoformat()} to {checkout.isoformat()}; "
            f"lead {(checkin - today).days} days; {args.date_timezone}",
            file=sys.stderr,
        )

    subject = _travelstaytion_subject_ref(args.subject)
    subject.extra["quote_currency"] = requested_currency
    requested_site = "vrbo_expedia" if args.channel == "vrbo" else args.channel
    with EvidenceStore(args.db) as store:
        candidates = [
            ref for ref in store.load_verified_refs(subject)
            if ref.site == requested_site
        ]
    if args.candidate_url:
        wanted = args.candidate_url.rstrip("/")
        if requested_site in {"vrbo_expedia", "expedia"}:
            wanted = canonical_platform_url(requested_site, wanted)
        candidates = [ref for ref in candidates if ref.url.rstrip("/") == wanted]
    if not candidates:
        qualifier = f" matching {args.candidate_url}" if args.candidate_url else ""
        raise ValueError(
            f"no unexpired verified {args.channel} mapping{qualifier} exists for {args.subject}; "
            "run parity verify first"
        )
    if len(candidates) > 1:
        raise ValueError(
            f"{len(candidates)} verified {args.channel} mappings exist; select one with --candidate-url"
        )

    candidate = candidates[0]
    candidate.extra["quote_currency"] = requested_currency
    if date_policy_evidence is not None:
        candidate.extra["date_policy"] = date_policy_evidence
    expedia_group_site = candidate.site in {"vrbo_expedia", "expedia"}
    if (
        candidate.site == "booking"
        and not args.offline
        and not args.enable_booking_pricing_capture
    ):
        raise ValueError(
            "live Booking pricing capture is locked; use --offline with an exact "
            "saved pricing snapshot, or explicitly add --enable-booking-pricing-capture "
            "for one controlled diagnostic capture"
        )
    if (
        candidate.site == "booking"
        and args.enable_booking_pricing_capture
        and (args.offline or not args.record_fixtures)
    ):
        raise ValueError(
            "--enable-booking-pricing-capture is live diagnostic mode and requires "
            "--record-fixtures (without --offline)"
        )
    candidate.extra["booking_pricing_capture_enabled"] = (
        args.enable_booking_pricing_capture
    )
    if (
        expedia_group_site
        and not args.offline
        and not args.enable_expedia_group_pricing_capture
    ):
        display = "Vrbo" if candidate.site == "vrbo_expedia" else "Expedia"
        raise ValueError(
            f"live {display} pricing capture is locked; use --offline with an exact "
            "saved pricing snapshot, or explicitly add "
            "--enable-expedia-group-pricing-capture for one controlled diagnostic capture"
        )
    if (
        expedia_group_site
        and args.enable_expedia_group_pricing_capture
        and (args.offline or not args.record_fixtures)
    ):
        raise ValueError(
            "--enable-expedia-group-pricing-capture is live diagnostic mode and "
            "requires --record-fixtures (without --offline)"
        )
    candidate.extra["expedia_group_pricing_capture_enabled"] = (
        args.enable_expedia_group_pricing_capture
    )
    with _http_client(args) as client:
        if args.auto_minimum_stay:
            assert today is not None
            windows = candidate_date_windows(
                today=today,
                min_lead_days=args.min_lead_days,
                target_lead_days=args.target_lead_days,
                stay_nights=1,
                search_weeks=args.auto_date_search_weeks,
            )
            subject_adapter = get_http_adapter(subject.site, client=client)
            windows = _calendar_preferred_windows(
                subject_adapter, subject, windows, today, args.min_lead_days,
                fail_when_closed=True,
            )

            def subject_probe(window):
                restriction_reader = getattr(
                    subject_adapter,
                    "minimum_stay_for_checkin",
                    None,
                )
                calendar_minimum = (
                    restriction_reader(subject, window.checkin)
                    if window.nights == 1 and restriction_reader is not None
                    else None
                )
                if calendar_minimum is not None and calendar_minimum > window.nights:
                    result = PriceResult(
                        site=subject.site,
                        ref=subject,
                        outcome=Outcome.MIN_STAY,
                        checkin=window.checkin,
                        checkout=window.checkout,
                        guests=args.guests,
                        minimum_stay_nights=calendar_minimum,
                        notes=(
                            "TravelStaytion public calendar requires a minimum "
                            f"stay of {calendar_minimum} nights"
                        ),
                    )
                else:
                    result = subject_adapter.get_price(
                        subject,
                        window.checkin,
                        window.checkout,
                        args.guests,
                    )
                print(
                    "[parity] minimum-stay TravelStaytion probe "
                    f"{window.checkin.isoformat()} to {window.checkout.isoformat()}: "
                    f"{result.outcome.value}"
                    f"{f' (minimum {result.minimum_stay_nights} nights)' if result.minimum_stay_nights else ''}"
                    f"{' -- ' + result.notes if result.notes else ''}",
                    file=sys.stderr,
                )
                return result

            with _maybe_browser_session(
                [candidate.url],
                args.headless,
                offline=args.offline,
                record_fixtures=args.record_fixtures,
                fixtures_dir=args.fixtures_dir,
                fixture_ttl_days=args.fixture_ttl_days,
                governor=client.governor,
            ) as browser_session:
                candidate_adapter = (
                    get_browser_adapter(candidate.site, session=browser_session)
                    if candidate.site in {"booking", "vrbo_expedia", "expedia"}
                    else get_http_adapter(candidate.site, client=client)
                )

                def candidate_probe(window):
                    result = candidate_adapter.get_price(
                        candidate,
                        window.checkin,
                        window.checkout,
                        args.guests,
                    )
                    print(
                        f"[parity] minimum-stay {candidate.site} probe "
                        f"{window.checkin.isoformat()} to {window.checkout.isoformat()}: "
                        f"{result.outcome.value}"
                        f"{f' (minimum {result.minimum_stay_nights} nights)' if result.minimum_stay_nights else ''}"
                        f"{' -- ' + result.notes if result.notes else ''}",
                        file=sys.stderr,
                    )
                    return result

                selection = select_common_minimum_stay_window(
                    windows,
                    subject_probe,
                    candidate_probe,
                    max_adjustments=args.minimum_stay_max_adjustments,
                )

            checkin = selection.window.checkin
            checkout = selection.window.checkout
            subject_price = selection.subject_price
            price = selection.candidate_price
            selected_minimum_stay = selection.minimum_stay_nights
            subject_minimum_stay = selection.subject_minimum_stay_nights
            candidate_minimum_stay = selection.candidate_minimum_stay_nights
            candidate_minimum_evidence = (
                "exact"
                if candidate_minimum_stay is not None
                else "accepted_comparison_duration_only"
            )
            date_policy_evidence = {
                "mode": "automatic_minimum_stay",
                "operating_date": today.isoformat(),
                "timezone": args.date_timezone,
                "minimum_lead_days": args.min_lead_days,
                "target_lead_days": args.target_lead_days,
                "minimum_stay_nights": selection.minimum_stay_nights,
                "comparison_stay_nights": selection.minimum_stay_nights,
                "subject_minimum_stay_nights": subject_minimum_stay,
                "candidate_minimum_stay_nights": candidate_minimum_stay,
                "candidate_minimum_stay_evidence": candidate_minimum_evidence,
                "search_weeks": args.auto_date_search_weeks,
                "selected_window_index": selection.selected_index,
                "actual_lead_days": (checkin - today).days,
                "probes": [
                    {
                        "index": item.index,
                        "checkin": item.window.checkin.isoformat(),
                        "checkout": item.window.checkout.isoformat(),
                        "outcome": item.outcome.value,
                        "site": item.site,
                        "minimum_stay_nights": item.minimum_stay_nights,
                        "notes": item.notes,
                    }
                    for item in selection.probes
                ],
            }
            candidate.extra["date_policy"] = date_policy_evidence
            print(
                "[parity] common minimum stay selected: "
                f"{checkin.isoformat()} to {checkout.isoformat()}; "
                f"{selection.minimum_stay_nights} night(s); "
                f"window {selection.selected_index}/{len(windows)}",
                file=sys.stderr,
            )
            print(
                "[parity]   TravelStaytion minimum for this check-in: "
                f"{subject_minimum_stay} night(s)",
                file=sys.stderr,
            )
            if candidate_minimum_stay is None:
                print(
                    f"[parity]   {candidate.site} accepts "
                    f"{selection.minimum_stay_nights} night(s); exact minimum not exposed",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[parity]   {candidate.site} minimum for this check-in: "
                    f"{candidate_minimum_stay} night(s)",
                    file=sys.stderr,
                )
        else:
            if args.auto_dates:
                assert today is not None
                windows = candidate_date_windows(
                    today=today,
                    min_lead_days=args.min_lead_days,
                    target_lead_days=args.target_lead_days,
                    stay_nights=args.stay_nights,
                    search_weeks=args.auto_date_search_weeks,
                )
                subject_adapter = get_http_adapter(subject.site, client=client)
                windows = _calendar_preferred_windows(
                    subject_adapter, subject, windows, today, args.min_lead_days,
                    fail_when_closed=True,
                )

                def probe(window):
                    result = subject_adapter.get_price(
                        subject,
                        window.checkin,
                        window.checkout,
                        args.guests,
                    )
                    print(
                        "[parity] auto-date TravelStaytion probe "
                        f"{window.checkin.isoformat()} to {window.checkout.isoformat()}: "
                        f"{result.outcome.value}"
                        f"{' -- ' + result.notes if result.notes else ''}",
                        file=sys.stderr,
                    )
                    return result

                selection = select_first_available_window(windows, probe)
                checkin = selection.window.checkin
                checkout = selection.window.checkout
                subject_price = selection.price
                date_policy_evidence = {
                    "mode": "automatic",
                    "operating_date": today.isoformat(),
                    "timezone": args.date_timezone,
                    "minimum_lead_days": args.min_lead_days,
                    "target_lead_days": args.target_lead_days,
                    "stay_nights": args.stay_nights,
                    "search_weeks": args.auto_date_search_weeks,
                    "selected_window_index": selection.selected_index,
                    "actual_lead_days": (checkin - today).days,
                    "probes": [
                        {
                            "index": item.index,
                            "checkin": item.window.checkin.isoformat(),
                            "checkout": item.window.checkout.isoformat(),
                            "outcome": item.outcome.value,
                            "notes": item.notes,
                        }
                        for item in selection.probes
                    ],
                }
                candidate.extra["date_policy"] = date_policy_evidence
                print(
                    "[parity] automatic dates selected: "
                    f"{checkin.isoformat()} to {checkout.isoformat()}; "
                    f"window {selection.selected_index}/{len(windows)}",
                    file=sys.stderr,
                )
            else:
                assert checkin is not None and checkout is not None
                subject_price = get_http_adapter(
                    subject.site,
                    client=client,
                ).get_price(subject, checkin, checkout, args.guests)

            assert checkin is not None and checkout is not None
            if candidate.site in {"booking", "vrbo_expedia", "expedia"}:
                with _maybe_browser_session(
                    [candidate.url],
                    args.headless,
                    offline=args.offline,
                    record_fixtures=args.record_fixtures,
                    fixtures_dir=args.fixtures_dir,
                    fixture_ttl_days=args.fixture_ttl_days,
                    governor=client.governor,
                ) as browser_session:
                    price = get_browser_adapter(
                        candidate.site,
                        session=browser_session,
                    ).get_price(candidate, checkin, checkout, args.guests)
            else:
                price = get_http_adapter(candidate.site, client=client).get_price(
                    candidate,
                    checkin,
                    checkout,
                    args.guests,
                )

        if subject_price is not None:
            apply_quote_evidence_policy(subject_price, requested_currency)
        apply_quote_evidence_policy(price, requested_currency)
        if subject_price is not None and price.is_priced():
            comparison = compare_aligned_prices(subject_price, price)

    report = QuoteReport(
        subject=subject,
        candidate=candidate,
        price=price,
        subject_price=subject_price,
        comparison=comparison,
        minimum_stay_nights=selected_minimum_stay,
        comparison_stay_nights=selected_minimum_stay,
        subject_minimum_stay_nights=subject_minimum_stay,
        candidate_minimum_stay_nights=candidate_minimum_stay,
        candidate_minimum_stay_evidence=candidate_minimum_evidence,
        discount_scenarios=build_discount_scenarios(price, subject_price),
    )
    rendered = quote_to_json(report) if args.format == "json" else quote_to_console(report)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"wrote {output_path} as UTF-8", file=sys.stderr)
        print(quote_verdict_to_console(report), file=sys.stderr)
    else:
        print(rendered)
    return 0


def _cmd_import_booking_source(args: argparse.Namespace) -> int:
    """Parse a locally saved Ctrl+U source file without contacting Booking."""
    requested_currency = normalise_currency_code(args.currency)
    checkin = _parse_date(args.checkin)
    checkout = _parse_date(args.checkout)
    if checkout <= checkin:
        raise ValueError("--checkout must be after --checkin")
    if args.guests <= 0:
        raise ValueError("--guests must be greater than zero")
    source_path = Path(args.source)
    if not source_path.is_file():
        raise ValueError(f"Booking source file does not exist: {source_path}")
    try:
        source_text = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ValueError(f"Could not read Booking source file: {error}") from error

    candidate_url = canonical_booking_url(args.candidate_url)
    raw_id = Path(urlparse(candidate_url).path).stem
    candidate = PropertyRef(
        site="booking",
        raw_id=raw_id,
        url=candidate_url,
    )
    price = price_booking_source(
        source_text,
        ref=candidate,
        checkin=checkin,
        checkout=checkout,
        guests=args.guests,
        currency=requested_currency,
    )
    apply_quote_evidence_policy(price, requested_currency)
    report = QuoteReport(subject=candidate, candidate=candidate, price=price)
    rendered = quote_to_json(report) if args.format == "json" else quote_to_console(report)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"wrote {output_path} as UTF-8", file=sys.stderr)
    else:
        print(rendered)
    print(quote_verdict_to_console(report), file=sys.stderr)
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    results_path = Path(args.results)
    output_path = Path(args.output)
    quotes_dir = Path(args.quotes_dir) if args.quotes_dir else None
    data = write_dashboard(results_path, output_path, quotes_dir=quotes_dir)
    print(f"wrote {output_path.resolve()} as UTF-8", file=sys.stderr)
    print(
        f"[parity] DASHBOARD: {len(data['listings'])} listing(s); "
        "self-contained and offline",
        file=sys.stderr,
    )
    if args.compact:
        if not _open_compact_dashboard(output_path):
            print(
                "[parity] Microsoft Edge app mode was not found; opening the "
                "default browser instead",
                file=sys.stderr,
            )
            webbrowser.open(output_path.resolve().as_uri())
    elif args.open:
        webbrowser.open(output_path.resolve().as_uri())
    return 0


def _open_compact_url(url: str) -> bool:
    """Open a compact local Edge app window when available on Windows."""
    if sys.platform != "win32":
        return False
    candidates = []
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            candidates.append(
                Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            )
    edge = next((path for path in candidates if path.is_file()), None)
    if edge is None:
        return False
    subprocess.Popen(
        [
            str(edge),
            f"--app={url}",
            "--window-size=540,760",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def _open_compact_dashboard(output_path: Path) -> bool:
    return _open_compact_url(output_path.resolve().as_uri())


def _cmd_app(args: argparse.Namespace) -> int:
    server = create_app_server(Path(args.work_dir), port=args.port)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"[parity] Parity Desk running at {url}", file=sys.stderr)
    print("[parity] Press Ctrl+C in PowerShell to stop it.", file=sys.stderr)
    if not _open_compact_url(url):
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n[parity] Parity Desk stopped.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


def _add_network_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use saved HTTP fixtures only; any missing/stale response stops without network access",
    )
    parser.add_argument(
        "--record-fixtures",
        action="store_true",
        help="Save successful HTTP responses as inert fixtures for later offline replay",
    )
    parser.add_argument("--fixtures-dir", default=".parity/fixtures")
    parser.add_argument("--fixture-ttl-days", type=int, default=30)
    parser.add_argument("--governor-db", default=".parity/parity.db")
    parser.add_argument("--airbnb-page-budget", type=int, default=3)
    parser.add_argument("--airbnb-pricing-budget", type=int, default=1)
    parser.add_argument("--airbnb-search-budget", type=int, default=2)
    parser.add_argument("--airbnb-image-budget", type=int, default=40)
    parser.add_argument("--booking-page-budget", type=int, default=1)
    parser.add_argument("--booking-search-budget", type=int, default=1)
    parser.add_argument("--booking-pricing-budget", type=int, default=1)
    parser.add_argument("--booking-image-budget", type=int, default=40)
    parser.add_argument("--travelstaytion-page-budget", type=int, default=1)
    parser.add_argument("--travelstaytion-pricing-budget", type=int, default=8)
    parser.add_argument("--travelstaytion-image-budget", type=int, default=40)
    parser.add_argument("--vrbo-page-budget", type=int, default=1)
    parser.add_argument("--vrbo-pricing-budget", type=int, default=1)
    parser.add_argument("--expedia-page-budget", type=int, default=1)
    parser.add_argument("--expedia-pricing-budget", type=int, default=1)
    parser.add_argument("--expedia-group-image-budget", type=int, default=40)
    parser.add_argument("--marriott-page-budget", type=int, default=1)
    parser.add_argument("--marriott-image-budget", type=int, default=40)
    parser.add_argument("--rate-limit-cooldown-hours", type=float, default=6.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="parity")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Check one listing against known mirror listings")
    p_check.add_argument("subject", help="The TravelStaytion (or other) listing URL to check")
    p_check.add_argument("--against", nargs="*", default=[], help="URLs of the same property on other channels")
    p_check.add_argument(
        "--verified",
        nargs="*",
        default=[],
        help="Candidate URLs already reviewed by a human as the same physical property",
    )
    p_check.add_argument("--verified-by", help="Reviewer recorded with --verified")
    p_check.add_argument("--verification-note", help="Evidence/reason recorded with --verified")
    p_check.add_argument("--checkin", required=True, help="YYYY-MM-DD")
    p_check.add_argument("--checkout", required=True, help="YYYY-MM-DD")
    p_check.add_argument("--guests", type=int, default=2)
    p_check.add_argument("--hash-cache", default=".parity/photo-hashes.db")
    p_check.add_argument("--format", choices=["console", "json", "csv"], default="console")
    p_check.add_argument("--headless", action="store_true", default=False,
                          help="Run the browser (for Vrbo/Booking/Guesty) headless. "
                               "Default is a visible window, since these adapters are new "
                               "and worth watching the first several times.")
    _add_network_arguments(p_check)
    p_check.set_defaults(func=_cmd_check)

    p_batch = sub.add_parser("batch", help="Run a batch of checks from a JSON config file")
    p_batch.add_argument("config", help="Path to a JSON list of check jobs")
    p_batch.add_argument("--format", choices=["console", "json", "csv"], default="console")
    p_batch.add_argument("--hash-cache", default=".parity/photo-hashes.db")
    p_batch.add_argument("--headless", action="store_true", default=False)
    _add_network_arguments(p_batch)
    p_batch.set_defaults(func=_cmd_batch)

    p_portfolio = sub.add_parser(
        "portfolio",
        help="Run a resumable quote portfolio for one host and up to ten listings",
    )
    p_portfolio.add_argument("config", help="Path to the portfolio JSON config")
    p_portfolio.add_argument(
        "--output",
        default="portfolio-results.json",
        help="Write the combined JSON manifest here; a CSV is written beside it",
    )
    p_portfolio.add_argument(
        "--work-dir",
        default=".parity/portfolio",
        help="Directory for per-comparison quote reports and resume state",
    )
    p_portfolio.add_argument(
        "--force",
        action="store_true",
        help="Ignore matching completed state and rerun every comparison",
    )
    p_portfolio.add_argument(
        "--date-timezone",
        choices=["system-local", "UTC"],
        default="system-local",
    )
    p_portfolio.add_argument("--min-lead-days", type=int, default=30)
    p_portfolio.add_argument("--headless", action="store_true", default=False)
    p_portfolio.add_argument(
        "--enable-booking-pricing-capture",
        action="store_true",
        default=False,
        help="Allow controlled live Booking capture for portfolio jobs",
    )
    _add_network_arguments(p_portfolio)
    p_portfolio.set_defaults(func=_cmd_portfolio)

    p_auto = sub.add_parser(
        "auto",
        help=(
            "From one to ten TravelStaytion IDs: discover, verify, price and "
            "build the compact dashboard"
        ),
    )
    p_auto.add_argument(
        "listings",
        nargs="+",
        help="One to ten TravelStaytion numeric IDs or /stay/<id> URLs",
    )
    p_auto.add_argument("--host", default="TravelStaytion parity check")
    p_auto.add_argument("--guests", type=int, default=2)
    p_auto.add_argument(
        "--currency",
        default="AUTO",
        help="AUTO infers the local listing currency; a three-letter override is allowed",
    )
    # Many hosts open their OTA calendars only ~3 months ahead; 45 days sits
    # inside that horizon while keeping the 30-day minimum lead.
    p_auto.add_argument("--target-lead-days", type=int, default=45)
    p_auto.add_argument("--auto-date-search-weeks", type=int, default=8)
    p_auto.add_argument("--minimum-stay-max-adjustments", type=int, default=4)
    p_auto.add_argument("--limit-per-site", type=int, default=5)
    p_auto.add_argument("--min-lead-days", type=int, default=30)
    p_auto.add_argument(
        "--date-timezone",
        choices=["system-local", "UTC"],
        default="system-local",
    )
    p_auto.add_argument("--work-dir", default=".parity/automatic")
    p_auto.add_argument("--output", default="automatic-results.json")
    p_auto.add_argument("--dashboard-output", default="parity-dashboard.html")
    p_auto.add_argument("--force", action="store_true")
    p_auto.add_argument("--headless", action="store_true", default=False)
    p_auto.add_argument(
        "--channels",
        nargs="+",
        choices=["airbnb", "booking"],
        default=["airbnb", "booking"],
        help="OTA channels to discover, verify and price (default: both)",
    )
    p_auto.add_argument(
        "--interactive-captcha",
        action="store_true",
        help="Wait (up to 3 minutes) for the operator to clear a Booking.com or "
        "search-engine human check in the visible browser instead of skipping it "
        "(the app turns this on)",
    )
    p_auto.add_argument(
        "--no-web-search",
        action="store_true",
        help="Skip licence-first Bing/DuckDuckGo/Google search (use map/OTA search only)",
    )
    p_auto.add_argument(
        "--compact",
        action="store_true",
        help="Open the finished dashboard in a 540 x 760 Edge app window",
    )
    _add_network_arguments(p_auto)
    p_auto.set_defaults(
        func=_cmd_auto,
        record_fixtures=True,
        travelstaytion_page_budget=20,
        travelstaytion_pricing_budget=240,
        travelstaytion_image_budget=400,
        airbnb_search_budget=20,
        airbnb_page_budget=20,
        airbnb_pricing_budget=20,
        airbnb_image_budget=400,
        booking_search_budget=10,
        booking_page_budget=20,
        booking_pricing_budget=20,
        booking_image_budget=400,
    )

    p_app = sub.add_parser(
        "app",
        help="Open the localhost-only single-listing parity interface",
    )
    p_app.add_argument("--work-dir", default=".parity/app")
    p_app.add_argument(
        "--port",
        type=int,
        default=0,
        help="Localhost port; 0 selects an available port automatically",
    )
    p_app.set_defaults(func=_cmd_app)

    p_dashboard = sub.add_parser(
        "dashboard",
        help="Build a compact self-contained dashboard from portfolio results",
    )
    p_dashboard.add_argument("results", help="Path to portfolio-results.json")
    p_dashboard.add_argument(
        "--output",
        default="parity-dashboard.html",
        help="Write the standalone dashboard HTML here",
    )
    p_dashboard.add_argument(
        "--quotes-dir",
        help="Optional fallback directory containing per-channel quote JSON files",
    )
    dashboard_open = p_dashboard.add_mutually_exclusive_group()
    dashboard_open.add_argument(
        "--open",
        action="store_true",
        help="Open the generated dashboard in the default browser",
    )
    dashboard_open.add_argument(
        "--compact",
        action="store_true",
        help="Open a 540 x 760 Microsoft Edge app window on Windows",
    )
    p_dashboard.set_defaults(func=_cmd_dashboard)

    p_discover = sub.add_parser(
        "discover",
        help="Generate unverified external-listing candidates for one subject",
    )
    p_discover.add_argument("subject", help="TravelStaytion listing ID or URL")
    p_discover.add_argument(
        "--channels",
        nargs="+",
        default=["airbnb", "booking"],
        help="Discovery channels (airbnb and/or booking)",
    )
    p_discover.add_argument("--limit-per-site", type=int, default=5)
    p_discover.add_argument("--db", default=".parity/parity.db")
    p_discover.add_argument("--headless", action="store_true", default=False)
    p_discover.add_argument(
        "--interactive-captcha",
        action="store_true",
        help="Wait (up to 3 minutes) for the operator to clear a Booking.com or "
        "search-engine human check in the visible browser instead of skipping it "
        "(the app turns this on)",
    )
    p_discover.add_argument(
        "--no-web-search",
        action="store_true",
        help="Skip licence-first Bing/DuckDuckGo/Google search (use map/OTA search only)",
    )
    p_discover.add_argument("--format", choices=["console", "json"], default="console")
    p_discover.add_argument(
        "--output",
        help="Write the report directly as UTF-8 (recommended on Windows instead of > redirection)",
    )
    _add_network_arguments(p_discover)
    p_discover.set_defaults(func=_cmd_discover)

    p_verify = sub.add_parser(
        "verify",
        help="Verify candidate identity without requesting any prices",
    )
    p_verify.add_argument("subject", help="TravelStaytion listing ID or URL")
    p_verify.add_argument(
        "--against",
        nargs="+",
        required=True,
        help="Candidate URLs to verify as the same physical property",
    )
    p_verify.add_argument("--db", default=".parity/parity.db")
    p_verify.add_argument("--hash-cache", default=".parity/photo-hashes.db")
    p_verify.add_argument("--format", choices=["console", "json"], default="console")
    p_verify.add_argument("--output", help="Write the report directly as UTF-8")
    p_verify.add_argument("--headless", action="store_true", default=False)
    _add_network_arguments(p_verify)
    p_verify.set_defaults(func=_cmd_verify)

    p_quote = sub.add_parser(
        "quote",
        help="Price one persisted verified mapping without identity or discovery requests",
    )
    p_quote.add_argument("subject", help="TravelStaytion /stay/<id> URL")
    p_quote.add_argument(
        "--channel",
        choices=["airbnb", "booking", "vrbo", "expedia"],
        default="airbnb",
    )
    p_quote.add_argument("--candidate-url", help="Select one mapping when several are verified")
    p_quote.add_argument("--db", default=".parity/parity.db")
    p_quote.add_argument("--checkin", help="YYYY-MM-DD")
    p_quote.add_argument("--checkout", help="YYYY-MM-DD")
    p_quote.add_argument(
        "--auto-dates",
        action="store_true",
        help="Select the first TravelStaytion-available bounded future window",
    )
    p_quote.add_argument(
        "--auto-minimum-stay",
        action="store_true",
        help=(
            "Select one future window and the shortest stay explicitly priced "
            "by both TravelStaytion and the verified channel"
        ),
    )
    p_quote.add_argument(
        "--date-timezone",
        choices=["system-local", "UTC"],
        default="system-local",
        help="Operating clock used to reject past or near-term live dates",
    )
    p_quote.add_argument(
        "--min-lead-days",
        type=int,
        default=30,
        help="Minimum lead time for live quote check-in dates (default: 30)",
    )
    p_quote.add_argument(
        "--target-lead-days",
        type=int,
        default=60,
        help="Automatic-date target lead time (default: 60)",
    )
    p_quote.add_argument(
        "--stay-nights",
        type=int,
        default=7,
        help="Automatic-date stay length (default: 7)",
    )
    p_quote.add_argument(
        "--auto-date-search-weeks",
        type=int,
        default=8,
        help="Maximum weekly TravelStaytion windows to probe (default: 8)",
    )
    p_quote.add_argument(
        "--minimum-stay-max-adjustments",
        type=int,
        default=3,
        help="Maximum restriction-driven stay-length adjustments per window (default: 3)",
    )
    p_quote.add_argument("--guests", type=int, default=2)
    p_quote.add_argument("--currency", default="EUR")
    p_quote.add_argument("--format", choices=["console", "json"], default="console")
    p_quote.add_argument("--output", help="Write the report directly as UTF-8")
    p_quote.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Run an explicitly enabled browser pricing capture without a visible window",
    )
    p_quote.add_argument(
        "--enable-booking-pricing-capture",
        action="store_true",
        default=False,
        help=(
            "Allow one controlled live Booking diagnostic capture. This may still return "
            "ERROR; arbitrary visible money text is never accepted as a quote."
        ),
    )
    p_quote.add_argument(
        "--enable-expedia-group-pricing-capture",
        action="store_true",
        default=False,
        help=(
            "Allow one controlled live Vrbo or Expedia diagnostic capture. This may "
            "still return ERROR; arbitrary visible money text is never accepted as a quote."
        ),
    )
    _add_network_arguments(p_quote)
    p_quote.set_defaults(func=_cmd_quote)

    p_import_booking = sub.add_parser(
        "import-booking-source",
        help="Parse a locally saved Booking Ctrl+U source file without network access",
    )
    p_import_booking.add_argument(
        "--source",
        required=True,
        help="UTF-8 HTML or bounded source excerpt saved locally",
    )
    p_import_booking.add_argument(
        "--candidate-url",
        "--url",
        required=True,
        help="Exact Booking /hotel/...html property URL represented by the source",
    )
    p_import_booking.add_argument("--checkin", required=True, help="YYYY-MM-DD")
    p_import_booking.add_argument("--checkout", required=True, help="YYYY-MM-DD")
    p_import_booking.add_argument("--guests", type=int, default=2)
    p_import_booking.add_argument("--currency", default="EUR")
    p_import_booking.add_argument(
        "--format", choices=["console", "json"], default="console"
    )
    p_import_booking.add_argument("--output", help="Write the report directly as UTF-8")
    p_import_booking.set_defaults(func=_cmd_import_booking_source)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OfflineFixtureError, RequestDeferred, ValueError) as error:
        print(f"[parity] {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
