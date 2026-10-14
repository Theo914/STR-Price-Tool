"""Deterministic guardrails and candidate windows for live price dates."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Sequence

from parity.core.models import Outcome, PriceResult


DEFAULT_MIN_LEAD_DAYS = 30
DEFAULT_TARGET_LEAD_DAYS = 60
DEFAULT_STAY_NIGHTS = 7
DEFAULT_SEARCH_WEEKS = 8
MONDAY = 0


@dataclass(frozen=True)
class DateWindow:
    checkin: date
    checkout: date

    @property
    def nights(self) -> int:
        return (self.checkout - self.checkin).days


@dataclass(frozen=True)
class DateProbe:
    index: int
    window: DateWindow
    outcome: Outcome
    notes: str | None


@dataclass(frozen=True)
class AutomaticDateSelection:
    window: DateWindow
    selected_index: int
    probes: tuple[DateProbe, ...]
    price: PriceResult


@dataclass(frozen=True)
class MinimumStayProbe:
    index: int
    site: str
    window: DateWindow
    outcome: Outcome
    minimum_stay_nights: int | None
    notes: str | None


@dataclass(frozen=True)
class CommonMinimumStaySelection:
    """One exact stay accepted and priced by both aligned channels."""

    window: DateWindow
    selected_index: int
    minimum_stay_nights: int
    subject_minimum_stay_nights: int
    candidate_minimum_stay_nights: int | None
    probes: tuple[MinimumStayProbe, ...]
    subject_price: PriceResult
    candidate_price: PriceResult


@dataclass(frozen=True)
class MultiChannelMinimumStaySelection:
    """One exact stay accepted and priced by TravelStaytion and every OTA."""

    window: DateWindow
    selected_index: int
    minimum_stay_nights: int
    subject_minimum_stay_nights: int
    candidate_minimum_stay_nights: dict[str, int | None]
    probes: tuple[MinimumStayProbe, ...]
    subject_price: PriceResult
    candidate_prices: dict[str, PriceResult]


def operating_today(name: str = "system-local", *, now: datetime | None = None) -> date:
    """Return today's date in the explicitly selected operating clock."""
    if name == "UTC":
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).date()
    if name == "system-local":
        current = now or datetime.now().astimezone()
        if current.tzinfo is None:
            return current.date()
        return current.astimezone().date()
    raise ValueError("date timezone must be 'system-local' or 'UTC'")


def validate_live_date_range(
    checkin: date,
    checkout: date,
    *,
    today: date,
    min_lead_days: int = DEFAULT_MIN_LEAD_DAYS,
) -> None:
    """Reject malformed, past or accidentally near-term live date ranges."""
    if isinstance(min_lead_days, bool) or min_lead_days < 0:
        raise ValueError("--min-lead-days cannot be negative")
    if checkout <= checkin:
        raise ValueError("--checkout must be after --checkin")
    earliest = today + timedelta(days=min_lead_days)
    if checkin < earliest:
        lead = (checkin - today).days
        raise ValueError(
            "live pricing check-in is too close or has passed: "
            f"{checkin.isoformat()} is {lead} day(s) from {today.isoformat()}; "
            f"earliest permitted check-in is {earliest.isoformat()} "
            f"(--min-lead-days {min_lead_days})"
        )


def candidate_date_windows(
    *,
    today: date,
    min_lead_days: int = DEFAULT_MIN_LEAD_DAYS,
    target_lead_days: int = DEFAULT_TARGET_LEAD_DAYS,
    stay_nights: int = DEFAULT_STAY_NIGHTS,
    search_weeks: int = DEFAULT_SEARCH_WEEKS,
    preferred_weekday: int = MONDAY,
) -> list[DateWindow]:
    """Generate bounded weekly windows for a later availability-selection step."""
    for label, value in (
        ("min_lead_days", min_lead_days),
        ("target_lead_days", target_lead_days),
        ("stay_nights", stay_nights),
        ("search_weeks", search_weeks),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label} must be an integer")
    if min_lead_days < 0:
        raise ValueError("min_lead_days cannot be negative")
    if target_lead_days < min_lead_days:
        raise ValueError("target_lead_days cannot be less than min_lead_days")
    if stay_nights <= 0:
        raise ValueError("stay_nights must be greater than zero")
    if search_weeks <= 0:
        raise ValueError("search_weeks must be greater than zero")
    if preferred_weekday not in range(7):
        raise ValueError("preferred_weekday must be between 0 and 6")

    target = today + timedelta(days=target_lead_days)
    weekday_offset = (preferred_weekday - target.weekday()) % 7
    first_checkin = target + timedelta(days=weekday_offset)
    return [
        DateWindow(
            checkin=first_checkin + timedelta(weeks=index),
            checkout=first_checkin
            + timedelta(weeks=index, days=stay_nights),
        )
        for index in range(search_weeks)
    ]


def select_first_available_window(
    windows: list[DateWindow],
    probe: Callable[[DateWindow], PriceResult],
) -> AutomaticDateSelection:
    """Select the first TravelStaytion-priced window, failing closed on errors."""
    if not windows:
        raise ValueError("automatic date selection requires at least one candidate window")

    attempts: list[DateProbe] = []
    for index, window in enumerate(windows, start=1):
        result = probe(window)
        attempts.append(DateProbe(
            index=index,
            window=window,
            outcome=result.outcome,
            notes=result.notes,
        ))
        if result.outcome == Outcome.PRICED:
            return AutomaticDateSelection(
                window=window,
                selected_index=index,
                probes=tuple(attempts),
                price=result,
            )
        if result.outcome in {Outcome.UNAVAILABLE, Outcome.MIN_STAY}:
            continue
        detail = f": {result.notes}" if result.notes else ""
        raise ValueError(
            "automatic date selection stopped on TravelStaytion window "
            f"{index}/{len(windows)} with {result.outcome.value}{detail}"
        )

    if attempts and all(attempt.outcome == Outcome.MIN_STAY for attempt in attempts):
        raise ValueError(
            "automatic date selection found no priceable TravelStaytion window: "
            f"all {len(windows)} bounded weekly window(s) were rejected by "
            "minimum-stay rules; increase --stay-nights"
        )
    raise ValueError(
        "automatic date selection found no TravelStaytion availability in "
        f"{len(windows)} bounded weekly window(s)"
    )


def select_common_minimum_stay_window(
    windows: list[DateWindow],
    subject_probe: Callable[[DateWindow], PriceResult],
    candidate_probe: Callable[[DateWindow], PriceResult],
    *,
    max_adjustments: int = 3,
) -> CommonMinimumStaySelection:
    """Find one shortest stay that both channels explicitly price.

    ``windows`` supplies candidate check-in dates.  Their checkout values are
    ignored: discovery always starts with one night, so a priced result proves
    a one-night minimum and a ``MIN_STAY`` result must carry the exact required
    night count.  When either side raises the minimum, both sides are re-priced
    for that same expanded stay.  Generic unavailability is never guessed to
    be a minimum-stay rule.
    """
    selection = select_multi_channel_minimum_stay_window(
        windows,
        subject_probe,
        [("candidate", candidate_probe)],
        max_adjustments=max_adjustments,
    )
    return CommonMinimumStaySelection(
        window=selection.window,
        selected_index=selection.selected_index,
        minimum_stay_nights=selection.minimum_stay_nights,
        subject_minimum_stay_nights=selection.subject_minimum_stay_nights,
        candidate_minimum_stay_nights=(
            selection.candidate_minimum_stay_nights["candidate"]
        ),
        probes=selection.probes,
        subject_price=selection.subject_price,
        candidate_price=selection.candidate_prices["candidate"],
    )


def select_multi_channel_minimum_stay_window(
    windows: list[DateWindow],
    subject_probe: Callable[[DateWindow], PriceResult],
    candidate_probes: Sequence[tuple[str, Callable[[DateWindow], PriceResult]]],
    *,
    max_adjustments: int = 3,
) -> MultiChannelMinimumStaySelection:
    """Find the shortest stay priced by TravelStaytion and every supplied OTA.

    Any exact higher minimum reported by one site expands the stay for all
    sites.  Every previously priced site is then probed again for the expanded
    dates.  An unavailable result moves the complete group to the next weekly
    window; it is never guessed to be a minimum-stay restriction.
    """
    if not windows:
        raise ValueError("minimum-stay selection requires at least one candidate window")
    if not candidate_probes:
        raise ValueError("multi-channel minimum-stay selection requires an OTA")
    labels = [label for label, _ in candidate_probes]
    if len(set(labels)) != len(labels) or any(not label for label in labels):
        raise ValueError("multi-channel minimum-stay labels must be unique")
    if isinstance(max_adjustments, bool) or not isinstance(max_adjustments, int):
        raise ValueError("max_adjustments must be an integer")
    if max_adjustments <= 0:
        raise ValueError("max_adjustments must be greater than zero")

    probes: list[MinimumStayProbe] = []

    def record(index: int, result: PriceResult) -> None:
        probes.append(MinimumStayProbe(
            index=index,
            site=result.site,
            window=DateWindow(result.checkin, result.checkout),
            outcome=result.outcome,
            minimum_stay_nights=result.minimum_stay_nights,
            notes=result.notes,
        ))

    def required_nights(result: PriceResult, requested: int) -> int:
        minimum = result.minimum_stay_nights
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= requested:
            raise ValueError(
                f"{result.site} reported a minimum-stay restriction without "
                "a valid higher night count"
            )
        return minimum

    for index, seed in enumerate(windows, start=1):
        nights = 1
        adjustments = 0
        subject_minimum: int | None = None
        candidate_minimums: dict[str, int | None] = {
            label: None for label in labels
        }
        while True:
            window = DateWindow(seed.checkin, seed.checkin + timedelta(days=nights))
            subject = subject_probe(window)
            record(index, subject)

            if subject.outcome == Outcome.UNAVAILABLE:
                break
            if subject.outcome == Outcome.MIN_STAY:
                nights = required_nights(subject, nights)
                subject_minimum = nights
                adjustments += 1
                if adjustments > max_adjustments:
                    raise ValueError(
                        "minimum-stay selection exceeded its bounded adjustment limit"
                    )
                continue
            if subject.outcome != Outcome.PRICED:
                detail = f": {subject.notes}" if subject.notes else ""
                raise ValueError(
                    "minimum-stay selection stopped on TravelStaytion window "
                    f"{index}/{len(windows)} with {subject.outcome.value}{detail}"
                )
            if nights == 1:
                subject_minimum = 1
            if (
                subject.minimum_stay_nights is not None
                and subject.minimum_stay_nights > nights
            ):
                raise ValueError(
                    "TravelStaytion priced a stay shorter than its reported minimum"
                )

            candidate_prices: dict[str, PriceResult] = {}
            restart = False
            unavailable = False
            for label, candidate_probe in candidate_probes:
                candidate = candidate_probe(window)
                record(index, candidate)
                if candidate.outcome == Outcome.UNAVAILABLE:
                    unavailable = True
                    break
                if candidate.outcome == Outcome.MIN_STAY:
                    nights = required_nights(candidate, nights)
                    candidate_minimums[label] = nights
                    adjustments += 1
                    if adjustments > max_adjustments:
                        raise ValueError(
                            "minimum-stay selection exceeded its bounded adjustment limit"
                        )
                    restart = True
                    break
                if candidate.outcome != Outcome.PRICED:
                    detail = f": {candidate.notes}" if candidate.notes else ""
                    raise ValueError(
                        "minimum-stay selection stopped on candidate window "
                        f"{index}/{len(windows)} at {candidate.site} with "
                        f"{candidate.outcome.value}{detail}"
                    )
                if nights == 1:
                    candidate_minimums[label] = 1
                if (
                    candidate.minimum_stay_nights is not None
                    and candidate.minimum_stay_nights > nights
                ):
                    raise ValueError(
                        f"{candidate.site} priced a stay shorter than its reported minimum"
                    )
                candidate_prices[label] = candidate

            if unavailable:
                break
            if restart:
                continue
            return MultiChannelMinimumStaySelection(
                window=window,
                selected_index=index,
                minimum_stay_nights=nights,
                subject_minimum_stay_nights=subject_minimum or nights,
                candidate_minimum_stay_nights=candidate_minimums,
                probes=tuple(probes),
                subject_price=subject,
                candidate_prices=candidate_prices,
            )

    raise ValueError(
        "minimum-stay selection found no common priceable stay in "
        f"{len(windows)} bounded weekly window(s)"
    )
