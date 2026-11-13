from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from kzbonds.scheduler import is_stale, next_run, parse_times, previous_run, upcoming_runs

TZ = ZoneInfo("Asia/Almaty")
TIMES = parse_times("11:45,12:45,17:50")


def _astana(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def test_parse_times_sorts_and_drops_blanks_and_repeats():
    assert parse_times(" 17:50, 11:45 ,,11:45") == [time(11, 45), time(17, 50)]


def test_parse_times_rejects_an_empty_schedule():
    with pytest.raises(ValueError):
        parse_times(" , ")


def test_next_run_before_the_first_slot_is_today():
    # 12 November 2026 is a Thursday.
    assert next_run(_astana(2026, 11, 12, 9, 0), TIMES, TZ) == _astana(2026, 11, 12, 11, 45)


def test_next_run_between_slots_is_the_following_slot():
    assert next_run(_astana(2026, 11, 12, 12, 0), TIMES, TZ) == _astana(2026, 11, 12, 12, 45)


def test_next_run_exactly_on_a_slot_moves_to_the_one_after():
    assert next_run(_astana(2026, 11, 12, 12, 45), TIMES, TZ) == _astana(2026, 11, 12, 17, 50)


def test_next_run_after_the_last_slot_is_tomorrow():
    assert next_run(_astana(2026, 11, 12, 18, 0), TIMES, TZ) == _astana(2026, 11, 13, 11, 45)


def test_next_run_skips_the_weekend():
    friday_evening = _astana(2026, 11, 13, 18, 0)
    saturday_noon = _astana(2026, 11, 14, 12, 0)
    monday_first = _astana(2026, 11, 16, 11, 45)
    assert next_run(friday_evening, TIMES, TZ) == monday_first
    assert next_run(saturday_noon, TIMES, TZ) == monday_first


def test_next_run_reads_the_clock_in_astana_time():
    # 06:00 UTC is 11:00 in Astana, so the 11:45 slot is still ahead.
    utc_morning = datetime(2026, 11, 12, 6, 0, tzinfo=ZoneInfo("UTC"))
    assert next_run(utc_morning, TIMES, TZ) == _astana(2026, 11, 12, 11, 45)


def test_previous_run_on_monday_morning_is_friday_evening():
    assert previous_run(_astana(2026, 11, 16, 9, 0), TIMES, TZ) == _astana(2026, 11, 13, 17, 50)


def test_snapshot_from_the_last_slot_is_not_stale_overnight_or_at_the_weekend():
    snapshot = _astana(2026, 11, 13, 17, 51)
    assert not is_stale(snapshot, _astana(2026, 11, 13, 23, 0), TIMES, TZ)
    assert not is_stale(snapshot, _astana(2026, 11, 15, 12, 0), TIMES, TZ)


def test_snapshot_is_stale_when_a_slot_passed_without_a_run():
    snapshot = _astana(2026, 11, 12, 11, 46)
    assert is_stale(snapshot, _astana(2026, 11, 12, 13, 0), TIMES, TZ)


def test_no_snapshot_at_all_is_stale():
    assert is_stale(None, _astana(2026, 11, 12, 3, 0), TIMES, TZ)


def test_upcoming_runs_lists_the_rest_of_today_then_the_next_weekdays():
    runs = upcoming_runs(_astana(2026, 11, 13, 12, 0), TIMES, TZ, days=3)  # Friday noon
    assert runs[0] == _astana(2026, 11, 13, 12, 45)
    assert runs[1] == _astana(2026, 11, 13, 17, 50)
    assert runs[2] == _astana(2026, 11, 16, 11, 45)  # Monday, the weekend skipped
    assert all(a < b for a, b in zip(runs, runs[1:]))
