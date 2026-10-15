from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from parity.core.date_policy import (
    candidate_date_windows,
    operating_today,
    select_common_minimum_stay_window,
    select_first_available_window,
    select_multi_channel_minimum_stay_window,
    validate_live_date_range,
)
from parity.core.models import Outcome, PriceResult, PropertyRef


TODAY = date(2026, 10, 12)


def test_live_date_range_rejects_past_and_near_term_checkins():
    with pytest.raises(ValueError, match="too close or has passed"):
        validate_live_date_range(
            date(2026, 10, 11),
            date(2026, 10, 18),
            today=TODAY,
        )
    with pytest.raises(ValueError, match="earliest permitted check-in is 2026-11-11"):
        validate_live_date_range(
            date(2026, 11, 10),
            date(2026, 11, 17),
            today=TODAY,
        )


def test_live_date_range_accepts_exact_minimum_lead_boundary():
    validate_live_date_range(
        date(2026, 11, 11),
        date(2026, 11, 18),
        today=TODAY,
    )


def test_candidate_windows_are_deterministic_monday_to_monday_weeks():
    windows = candidate_date_windows(today=TODAY)

    assert len(windows) == 8
    assert windows[0].checkin == date(2026, 12, 14)
    assert windows[0].checkout == date(2026, 12, 21)
    assert windows[0].nights == 7
    assert windows[1].checkin == date(2026, 12, 21)
    assert all(window.checkin.weekday() == 0 for window in windows)


def test_candidate_windows_reject_invalid_policy_values():
    with pytest.raises(ValueError, match="cannot be less"):
        candidate_date_windows(
            today=TODAY,
            min_lead_days=60,
            target_lead_days=30,
        )
    with pytest.raises(ValueError, match="stay_nights"):
        candidate_date_windows(today=TODAY, stay_nights=0)
    with pytest.raises(ValueError, match="search_weeks"):
        candidate_date_windows(today=TODAY, search_weeks=0)


def test_utc_operating_date_is_explicit_and_injectable():
    observed = datetime(2026, 10, 12, 23, 59, tzinfo=timezone.utc)
    assert operating_today("UTC", now=observed) == TODAY


def test_candidate_window_never_precedes_minimum_lead():
    windows = candidate_date_windows(
        today=TODAY,
        min_lead_days=30,
        target_lead_days=30,
        stay_nights=3,
        search_weeks=2,
        preferred_weekday=2,
    )

    assert windows[0].checkin >= TODAY + timedelta(days=30)
    assert windows[0].checkout - windows[0].checkin == timedelta(days=3)


def _probe_result(window, outcome, notes=None):
    ref = PropertyRef(
        site="travelstaytion",
        raw_id="742313",
        url="https://www.travelstaytion.com/stay/742313",
    )
    return PriceResult(
        site="travelstaytion",
        ref=ref,
        outcome=outcome,
        checkin=window.checkin,
        checkout=window.checkout,
        guests=2,
        notes=notes,
    )


def test_selector_skips_only_unavailable_or_min_stay_windows():
    windows = candidate_date_windows(today=TODAY, search_weeks=4)
    outcomes = iter([Outcome.UNAVAILABLE, Outcome.MIN_STAY, Outcome.PRICED])
    results = []

    def probe(window):
        result = _probe_result(window, next(outcomes))
        results.append(result)
        return result

    selection = select_first_available_window(
        windows,
        probe,
    )

    assert selection.selected_index == 3
    assert selection.window == windows[2]
    assert [probe.outcome for probe in selection.probes] == [
        Outcome.UNAVAILABLE,
        Outcome.MIN_STAY,
        Outcome.PRICED,
    ]
    assert selection.price is results[2]


def test_selector_stops_on_uncertain_subject_error():
    windows = candidate_date_windows(today=TODAY, search_weeks=2)
    calls = []

    def probe(window):
        calls.append(window)
        return _probe_result(window, Outcome.ERROR, "bad response")

    with pytest.raises(ValueError, match="stopped.*error: bad response"):
        select_first_available_window(windows, probe)

    assert calls == [windows[0]]


def test_selector_explains_when_every_window_breaks_minimum_stay_rule():
    windows = candidate_date_windows(today=TODAY, search_weeks=2)

    with pytest.raises(ValueError, match=r"all 2 .*minimum-stay.*increase --stay-nights"):
        select_first_available_window(
            windows,
            lambda window: _probe_result(
                window,
                Outcome.MIN_STAY,
                "There is a minimum stay limitation",
            ),
        )


def test_common_minimum_stay_uses_higher_channel_restriction():
    windows = candidate_date_windows(today=TODAY, stay_nights=1, search_weeks=1)
    calls = []

    def result(site, window, outcome, minimum=None):
        calls.append((site, window.nights, outcome))
        return PriceResult(
            site=site,
            ref=PropertyRef(site=site, raw_id="1", url=f"https://example.test/{site}/1"),
            outcome=outcome,
            checkin=window.checkin,
            checkout=window.checkout,
            guests=2,
            currency="GBP" if outcome == Outcome.PRICED else None,
            comparable_total=100.0 if outcome == Outcome.PRICED else None,
            headline_total=120.0 if outcome == Outcome.PRICED else None,
            minimum_stay_nights=minimum,
        )

    def subject_probe(window):
        if window.nights < 4:
            return result("travelstaytion", window, Outcome.MIN_STAY, 4)
        return result("travelstaytion", window, Outcome.PRICED, 4)

    def candidate_probe(window):
        if window.nights < 10:
            return result("booking", window, Outcome.MIN_STAY, 10)
        return result("booking", window, Outcome.PRICED, 10)

    selection = select_common_minimum_stay_window(
        windows,
        subject_probe,
        candidate_probe,
    )

    assert selection.minimum_stay_nights == 10
    assert selection.subject_minimum_stay_nights == 4
    assert selection.candidate_minimum_stay_nights == 10
    assert selection.window.nights == 10
    assert selection.subject_price.outcome == Outcome.PRICED
    assert selection.candidate_price.outcome == Outcome.PRICED
    assert calls == [
        ("travelstaytion", 1, Outcome.MIN_STAY),
        ("travelstaytion", 4, Outcome.PRICED),
        ("booking", 4, Outcome.MIN_STAY),
        ("travelstaytion", 10, Outcome.PRICED),
        ("booking", 10, Outcome.PRICED),
    ]


def test_multi_channel_minimum_stay_uses_longest_rule_and_reprices_every_site():
    windows = candidate_date_windows(today=TODAY, stay_nights=1, search_weeks=1)
    calls = []

    def result(site, window, outcome, minimum=None):
        calls.append((site, window.nights, outcome))
        return PriceResult(
            site=site,
            ref=PropertyRef(site=site, raw_id="1", url=f"https://example.test/{site}/1"),
            outcome=outcome,
            checkin=window.checkin,
            checkout=window.checkout,
            guests=2,
            currency="AUD" if outcome == Outcome.PRICED else None,
            comparable_total=100.0 if outcome == Outcome.PRICED else None,
            headline_total=100.0 if outcome == Outcome.PRICED else None,
            minimum_stay_nights=minimum,
        )

    def subject(window):
        return (
            result("travelstaytion", window, Outcome.MIN_STAY, 2)
            if window.nights < 2
            else result("travelstaytion", window, Outcome.PRICED, 2)
        )

    def airbnb(window):
        return (
            result("airbnb", window, Outcome.MIN_STAY, 3)
            if window.nights < 3
            else result("airbnb", window, Outcome.PRICED, 3)
        )

    def booking(window):
        return (
            result("booking", window, Outcome.MIN_STAY, 5)
            if window.nights < 5
            else result("booking", window, Outcome.PRICED, 5)
        )

    selection = select_multi_channel_minimum_stay_window(
        windows,
        subject,
        [("airbnb", airbnb), ("booking", booking)],
    )

    assert selection.minimum_stay_nights == 5
    assert selection.subject_minimum_stay_nights == 2
    assert selection.candidate_minimum_stay_nights == {"airbnb": 3, "booking": 5}
    assert set(selection.candidate_prices) == {"airbnb", "booking"}
    assert calls == [
        ("travelstaytion", 1, Outcome.MIN_STAY),
        ("travelstaytion", 2, Outcome.PRICED),
        ("airbnb", 2, Outcome.MIN_STAY),
        ("travelstaytion", 3, Outcome.PRICED),
        ("airbnb", 3, Outcome.PRICED),
        ("booking", 3, Outcome.MIN_STAY),
        ("travelstaytion", 5, Outcome.PRICED),
        ("airbnb", 5, Outcome.PRICED),
        ("booking", 5, Outcome.PRICED),
    ]


def test_common_minimum_stay_prices_one_night_when_both_accept_it():
    windows = candidate_date_windows(today=TODAY, stay_nights=1, search_weeks=1)

    def priced(site, window):
        return PriceResult(
            site=site,
            ref=PropertyRef(site=site, raw_id="1", url=f"https://example.test/{site}/1"),
            outcome=Outcome.PRICED,
            checkin=window.checkin,
            checkout=window.checkout,
            guests=2,
            currency="EUR",
            comparable_total=100.0,
            headline_total=100.0,
        )

    selection = select_common_minimum_stay_window(
        windows,
        lambda window: priced("travelstaytion", window),
        lambda window: priced("airbnb", window),
    )

    assert selection.minimum_stay_nights == 1
    assert selection.subject_minimum_stay_nights == 1
    assert selection.candidate_minimum_stay_nights == 1
    assert selection.window.nights == 1
    assert len(selection.probes) == 2


def test_common_minimum_stay_does_not_invent_a_lower_candidate_minimum():
    windows = candidate_date_windows(today=TODAY, stay_nights=1, search_weeks=1)

    def result(site, window, outcome, minimum=None):
        return PriceResult(
            site=site,
            ref=PropertyRef(site=site, raw_id="1", url=f"https://example.test/{site}/1"),
            outcome=outcome,
            checkin=window.checkin,
            checkout=window.checkout,
            guests=2,
            currency="EUR" if outcome == Outcome.PRICED else None,
            comparable_total=100.0 if outcome == Outcome.PRICED else None,
            headline_total=100.0 if outcome == Outcome.PRICED else None,
            minimum_stay_nights=minimum,
        )

    selection = select_common_minimum_stay_window(
        windows,
        lambda window: (
            result("travelstaytion", window, Outcome.MIN_STAY, 3)
            if window.nights < 3
            else result("travelstaytion", window, Outcome.PRICED)
        ),
        lambda window: result("airbnb", window, Outcome.PRICED),
    )

    assert selection.minimum_stay_nights == 3
    assert selection.subject_minimum_stay_nights == 3
    assert selection.candidate_minimum_stay_nights is None


def test_common_minimum_stay_rejects_unnumbered_restriction():
    windows = candidate_date_windows(today=TODAY, stay_nights=1, search_weeks=1)

    def unnumbered(window):
        return PriceResult(
            site="travelstaytion",
            ref=PropertyRef(
                site="travelstaytion",
                raw_id="1",
                url="https://www.travelstaytion.com/stay/1",
            ),
            outcome=Outcome.MIN_STAY,
            checkin=window.checkin,
            checkout=window.checkout,
            guests=2,
            notes="minimum stay limitation",
        )

    with pytest.raises(ValueError, match="without a valid higher night count"):
        select_common_minimum_stay_window(
            windows,
            unnumbered,
            lambda window: pytest.fail("candidate must not be probed"),
        )


def test_common_minimum_stay_does_not_reclassify_generic_unavailability():
    windows = candidate_date_windows(today=TODAY, stay_nights=1, search_weeks=2)
    candidate_calls = []

    def subject_probe(window):
        outcome = Outcome.UNAVAILABLE if window.checkin == windows[0].checkin else Outcome.PRICED
        return PriceResult(
            site="travelstaytion",
            ref=PropertyRef(
                site="travelstaytion", raw_id="1", url="https://www.travelstaytion.com/stay/1"
            ),
            outcome=outcome,
            checkin=window.checkin,
            checkout=window.checkout,
            guests=2,
            currency="EUR" if outcome == Outcome.PRICED else None,
            comparable_total=100.0 if outcome == Outcome.PRICED else None,
            headline_total=100.0 if outcome == Outcome.PRICED else None,
        )

    def candidate_probe(window):
        candidate_calls.append(window)
        return PriceResult(
            site="airbnb",
            ref=PropertyRef(site="airbnb", raw_id="1", url="https://airbnb.com/rooms/1"),
            outcome=Outcome.PRICED,
            checkin=window.checkin,
            checkout=window.checkout,
            guests=2,
            currency="EUR",
            comparable_total=110.0,
            headline_total=110.0,
        )

    selection = select_common_minimum_stay_window(
        windows,
        subject_probe,
        candidate_probe,
    )

    assert selection.selected_index == 2
    assert candidate_calls == [selection.window]


def test_open_calendar_dates_replace_blind_weekly_guesses():
    from parity.core.date_policy import DateWindow, prefer_open_checkins

    today = date(2026, 10, 14)
    windows = candidate_date_windows(today=today, target_lead_days=120, stay_nights=1, search_weeks=4)
    # Seasonal villa: only bookable until 31 December (rates not loaded later).
    open_dates = [date(2026, 10, 18) + timedelta(days=i) for i in range(75)]
    preferred = prefer_open_checkins(windows, open_dates, earliest=today + timedelta(days=30))

    assert len(preferred) == len(windows)
    assert all(window.checkin in open_dates for window in preferred)
    assert all(window.checkin >= today + timedelta(days=30) for window in preferred)
    assert all(window.nights == 1 for window in preferred)
    gaps = sorted(window.checkin for window in preferred)
    assert all((later - earlier).days >= 7 for earlier, later in zip(gaps, gaps[1:]))
    # Nearest to the 120-day target first.
    assert preferred[0].checkin == date(2026, 12, 31)
    assert isinstance(preferred[0], DateWindow)


def test_open_dates_inside_generated_windows_are_kept_first():
    from parity.core.date_policy import prefer_open_checkins

    today = date(2026, 10, 14)
    windows = candidate_date_windows(today=today, target_lead_days=120, stay_nights=2, search_weeks=3)
    open_dates = [windows[1].checkin, date(2027, 6, 1)]
    preferred = prefer_open_checkins(windows, open_dates, earliest=today)
    assert preferred[0].checkin == windows[1].checkin
    assert preferred[0].nights == 2


def test_no_usable_calendar_keeps_weekly_windows():
    from parity.core.date_policy import prefer_open_checkins

    today = date(2026, 10, 14)
    windows = candidate_date_windows(today=today, target_lead_days=120, stay_nights=1, search_weeks=3)
    assert prefer_open_checkins(windows, None, earliest=today) is windows
    assert prefer_open_checkins(windows, [], earliest=today) is windows
    too_soon = [today + timedelta(days=5)]
    assert prefer_open_checkins(windows, too_soon, earliest=today + timedelta(days=30)) is windows


def test_one_ota_failing_does_not_void_the_other_comparisons():
    """Operator run 337856: Booking returned an unrecognised page and the
    whole check (TravelStaytion and Airbnb too) failed."""
    from parity.core.date_policy import DateWindow, select_multi_channel_minimum_stay_window

    def result(site, window, outcome):
        return PriceResult(
            site=site,
            ref=PropertyRef(site=site, raw_id="1", url="u"),
            outcome=outcome,
            checkin=window.checkin,
            checkout=window.checkout,
            guests=2,
            comparable_total=100.0 if outcome == Outcome.PRICED else None,
            notes="Booking checkout snapshot has no supported availability outcome"
            if outcome == Outcome.ERROR else None,
        )

    windows = [DateWindow(date(2026, 11, 30), date(2026, 12, 1))]
    selection = select_multi_channel_minimum_stay_window(
        windows,
        lambda w: result("travelstaytion", w, Outcome.PRICED),
        [
            ("booking", lambda w: result("booking", w, Outcome.ERROR)),
            ("airbnb", lambda w: result("airbnb", w, Outcome.PRICED)),
        ],
    )
    assert set(selection.candidate_prices) == {"airbnb"}
    assert "no supported availability outcome" in selection.dropped["booking"]

    with pytest.raises(ValueError, match="every OTA failed"):
        select_multi_channel_minimum_stay_window(
            windows,
            lambda w: result("travelstaytion", w, Outcome.PRICED),
            [("booking", lambda w: result("booking", w, Outcome.ERROR))],
        )


def test_closed_travelstaytion_calendar_stops_shared_selection_plainly():
    # Live regression (756432): the calendar published no check-ins at all.
    from datetime import date, timedelta

    import pytest

    from parity.cli import _calendar_preferred_windows
    from parity.core.date_policy import candidate_date_windows

    class ClosedCalendar:
        def open_checkin_dates(self, ref):
            return []

    today = date(2026, 10, 15)
    windows = candidate_date_windows(
        today=today, min_lead_days=30, target_lead_days=45, stay_nights=1, search_weeks=8,
    )
    with pytest.raises(ValueError, match="no open check-in dates at all"):
        _calendar_preferred_windows(
            ClosedCalendar(), None, windows, today, 30, fail_when_closed=True
        )
    assert _calendar_preferred_windows(ClosedCalendar(), None, windows, today, 30) == windows
