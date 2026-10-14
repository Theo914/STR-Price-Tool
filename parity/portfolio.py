"""Validation and reporting helpers for the bounded host portfolio workflow."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any


MAX_PORTFOLIO_LISTINGS = 10
SUPPORTED_PORTFOLIO_CHANNELS = {"airbnb", "booking"}
PORTFOLIO_STATE_VERSION = 1
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _resolve_path(value: object, label: str, base_dir: Path) -> str:
    raw = _required_text(value, label)
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else (base_dir / path).resolve())


def _slug(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return slug[:48] or "listing"


def load_portfolio_config(path: Path) -> dict[str, Any]:
    """Load one host portfolio and reject ambiguous or unsafe shapes."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"could not read portfolio config: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"portfolio config is not valid JSON: {error}") from error

    if isinstance(payload, list):
        payload = {"host": path.stem, "listings": payload}
    if not isinstance(payload, dict):
        raise ValueError("portfolio config must be an object with a listings array")
    listings = payload.get("listings")
    if not isinstance(listings, list) or not listings:
        raise ValueError("portfolio config requires a non-empty listings array")
    if len(listings) > MAX_PORTFOLIO_LISTINGS:
        raise ValueError(
            f"portfolio contains {len(listings)} listings; maximum is "
            f"{MAX_PORTFOLIO_LISTINGS}"
        )
    defaults = payload.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("portfolio defaults must be an object")

    host = payload.get("host", path.stem)
    host = _required_text(host, "portfolio host")
    return {
        "host": host,
        "defaults": defaults,
        "listings": listings,
    }


def expand_portfolio_jobs(
    config: dict[str, Any],
    *,
    config_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalise up to ten listings into one quote job per verified channel."""
    defaults = config["defaults"]
    base_dir = config_path.resolve().parent
    normalised_listings: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []

    for index, raw_listing in enumerate(config["listings"], start=1):
        if not isinstance(raw_listing, dict):
            raise ValueError(f"listing {index} must be an object")
        subject = _required_text(raw_listing.get("subject"), f"listing {index} subject")
        name = _required_text(
            raw_listing.get("name") or subject.rstrip("/").rsplit("/", 1)[-1],
            f"listing {index} name",
        )
        currency = _required_text(
            raw_listing.get("currency", defaults.get("currency")),
            f"listing {index} currency",
        ).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(f"listing {index} currency must be a three-letter code")
        guests = _positive_int(
            raw_listing.get("guests", defaults.get("guests", 2)),
            f"listing {index} guests",
        )
        checkin = raw_listing.get("checkin", defaults.get("checkin"))
        checkout = raw_listing.get("checkout", defaults.get("checkout"))
        if bool(checkin) != bool(checkout):
            raise ValueError(
                f"listing {index} must provide both checkin and checkout, or neither"
            )
        explicit_dates = bool(checkin and checkout)
        auto_minimum_stay = raw_listing.get(
            "auto_minimum_stay",
            defaults.get("auto_minimum_stay", not explicit_dates),
        )
        if not isinstance(auto_minimum_stay, bool):
            raise ValueError(f"listing {index} auto_minimum_stay must be true or false")
        if explicit_dates and auto_minimum_stay:
            raise ValueError(
                f"listing {index} cannot combine explicit dates with auto_minimum_stay"
            )
        if not explicit_dates and not auto_minimum_stay:
            raise ValueError(
                f"listing {index} requires explicit dates or auto_minimum_stay"
            )

        raw_channels = raw_listing.get("channels")
        if not isinstance(raw_channels, list) or not raw_channels:
            raise ValueError(f"listing {index} requires a non-empty channels array")
        if len(raw_channels) > len(SUPPORTED_PORTFOLIO_CHANNELS):
            raise ValueError(f"listing {index} contains too many channel entries")
        listing_id = f"{index:02d}-{_slug(name)}"
        target_lead_days = _positive_int(
            raw_listing.get("target_lead_days", defaults.get("target_lead_days", 60)),
            f"listing {index} target_lead_days",
        )
        auto_date_search_weeks = _positive_int(
            raw_listing.get(
                "auto_date_search_weeks",
                defaults.get("auto_date_search_weeks", 8),
            ),
            f"listing {index} auto_date_search_weeks",
        )
        minimum_stay_max_adjustments = _positive_int(
            raw_listing.get(
                "minimum_stay_max_adjustments",
                defaults.get("minimum_stay_max_adjustments", 3),
            ),
            f"listing {index} minimum_stay_max_adjustments",
        )
        channels: list[dict[str, Any]] = []
        seen_channels: set[str] = set()
        for channel_index, raw_channel in enumerate(raw_channels, start=1):
            if not isinstance(raw_channel, dict):
                raise ValueError(
                    f"listing {index} channel {channel_index} must be an object"
                )
            channel = _required_text(
                raw_channel.get("channel"),
                f"listing {index} channel {channel_index} name",
            ).lower()
            if channel not in SUPPORTED_PORTFOLIO_CHANNELS:
                raise ValueError(
                    f"listing {index} channel {channel!r} is unsupported; "
                    "use airbnb or booking"
                )
            if channel in seen_channels:
                raise ValueError(f"listing {index} repeats channel {channel}")
            seen_channels.add(channel)
            candidate_url = _required_text(
                raw_channel.get("candidate_url"),
                f"listing {index} {channel} candidate_url",
            )
            db_value = raw_channel.get("db", raw_listing.get("db", defaults.get("db")))
            if db_value is None:
                raise ValueError(f"listing {index} {channel} requires a mapping db path")
            db = _resolve_path(db_value, f"listing {index} {channel} db", base_dir)
            fixture_value = raw_channel.get(
                "fixtures_dir",
                raw_listing.get("fixtures_dir", defaults.get("fixtures_dir")),
            )
            fixtures_dir = (
                _resolve_path(
                    fixture_value,
                    f"listing {index} {channel} fixtures_dir",
                    base_dir,
                )
                if fixture_value is not None
                else None
            )
            channel_record = {
                "channel": channel,
                "candidate_url": candidate_url,
                "db": db,
                "fixtures_dir": fixtures_dir,
            }
            channels.append(channel_record)
            jobs.append({
                "job_id": f"{listing_id}-{channel}",
                "listing_id": listing_id,
                "listing_index": index,
                "listing_name": name,
                "subject": subject,
                "channel": channel,
                "candidate_url": candidate_url,
                "db": db,
                "fixtures_dir": fixtures_dir,
                "currency": currency,
                "guests": guests,
                "checkin": str(checkin) if explicit_dates else None,
                "checkout": str(checkout) if explicit_dates else None,
                "auto_minimum_stay": auto_minimum_stay,
                "target_lead_days": target_lead_days,
                "auto_date_search_weeks": auto_date_search_weeks,
                "minimum_stay_max_adjustments": minimum_stay_max_adjustments,
            })

        normalised_listings.append({
            "listing_id": listing_id,
            "name": name,
            "subject": subject,
            "currency": currency,
            "guests": guests,
            "checkin": str(checkin) if explicit_dates else None,
            "checkout": str(checkout) if explicit_dates else None,
            "auto_minimum_stay": auto_minimum_stay,
            "target_lead_days": target_lead_days,
            "auto_date_search_weeks": auto_date_search_weeks,
            "minimum_stay_max_adjustments": minimum_stay_max_adjustments,
            "channels": channels,
        })

    return normalised_listings, jobs


def job_fingerprint(job: dict[str, Any], execution: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"job": job, "execution": execution},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def status_from_quote(report: dict[str, Any]) -> str:
    price = report.get("price") or {}
    outcome = price.get("outcome")
    if outcome == "priced":
        return "PRICED" if report.get("comparison") else "REVIEW_REQUIRED"
    if outcome in {"unavailable", "min_stay"}:
        return "UNAVAILABLE"
    return "PRICE_FAILED"


def result_from_quote(
    job: dict[str, Any],
    report: dict[str, Any],
    *,
    output_file: Path,
    resumed: bool,
) -> dict[str, Any]:
    price = report.get("price") or {}
    subject_price = report.get("subject_price") or {}
    comparison = report.get("comparison") or {}
    checkin = price.get("checkin")
    checkout = price.get("checkout")
    nights = None
    if checkin and checkout:
        try:
            nights = (
                datetime.fromisoformat(checkout).date()
                - datetime.fromisoformat(checkin).date()
            ).days
        except ValueError:
            nights = None
    return {
        "job_id": job["job_id"],
        "listing_id": job["listing_id"],
        "listing_name": job["listing_name"],
        "subject_url": job["subject"],
        "channel": job["channel"],
        "candidate_url": job["candidate_url"],
        "status": status_from_quote(report),
        "checkin": checkin,
        "checkout": checkout,
        "nights": nights,
        "guests": price.get("guests", job["guests"]),
        "currency": price.get("currency", job["currency"]),
        "travelstaytion_guest_total": subject_price.get("headline_total"),
        "ota_guest_total": price.get("headline_total"),
        "difference": comparison.get("candidate_minus_subject"),
        "difference_percent": comparison.get("candidate_difference_percent"),
        "verdict": comparison.get("verdict"),
        "breakdown_status": price.get("breakdown_status"),
        "discount_scenarios": report.get("discount_scenarios", []),
        "quote_output": str(output_file),
        "fixtures_dir": job.get("fixtures_dir"),
        "resumed": resumed,
        "error": None,
    }


def failed_result(job: dict[str, Any], status: str, error: str) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "listing_id": job["listing_id"],
        "listing_name": job["listing_name"],
        "subject_url": job["subject"],
        "channel": job["channel"],
        "candidate_url": job["candidate_url"],
        "status": status,
        "checkin": job.get("checkin"),
        "checkout": job.get("checkout"),
        "nights": None,
        "guests": job["guests"],
        "currency": job["currency"],
        "travelstaytion_guest_total": None,
        "ota_guest_total": None,
        "difference": None,
        "difference_percent": None,
        "verdict": None,
        "breakdown_status": None,
        "discount_scenarios": [],
        "quote_output": None,
        "fixtures_dir": job.get("fixtures_dir"),
        "resumed": False,
        "error": error,
    }


def portfolio_manifest(
    *,
    host: str,
    config_path: Path,
    listings: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for result in results:
        status = result["status"]
        counts[status] = counts.get(status, 0) + 1
    return {
        "schema_version": 1,
        "host": host,
        "config_path": str(config_path.resolve()),
        "listing_count": len(listings),
        "comparison_count": len(results),
        "status_counts": counts,
        "listings": listings,
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def portfolio_csv(manifest: dict[str, Any]) -> str:
    fields = [
        "listing_id",
        "listing_name",
        "subject_url",
        "channel",
        "candidate_url",
        "status",
        "checkin",
        "checkout",
        "nights",
        "guests",
        "currency",
        "travelstaytion_guest_total",
        "ota_guest_total",
        "difference",
        "difference_percent",
        "verdict",
        "breakdown_status",
        "quote_output",
        "fixtures_dir",
        "resumed",
        "error",
    ]
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(manifest["results"])
    return buffer.getvalue()
