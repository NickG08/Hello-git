"""
Refreshes the data at fixed times of the trading day.

Both exchanges trade on weekdays between late morning and early evening,
Astana time, and nothing changes outside those hours. So instead of a fixed
interval the pipeline runs at the listed times (config.SCHEDULE_TIMES, in
config.SCHEDULE_TZ) from Monday to Friday, and the page is rebuilt after
each run.

A run on a public holiday is harmless: the exchanges return yesterday's
numbers and the snapshot repeats them.
"""

from __future__ import annotations

import logging
import time as time_module
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from kzbonds import config, db, site
from kzbonds.pipeline import run as run_pipeline

logger = logging.getLogger("kzbonds.scheduler")

_MAX_SLEEP_SECONDS = 60


def parse_times(spec: str) -> list[time]:
    """ "11:45, 12:45" -> sorted list of times."""
    times = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        hour, minute = part.split(":")
        times.append(time(int(hour), int(minute)))
    if not times:
        raise ValueError("schedule has no times")
    return sorted(set(times))


def _slots_on(day, times: list[time], tz: ZoneInfo) -> list[datetime]:
    if day.weekday() >= 5:  # Saturday, Sunday
        return []
    return [datetime.combine(day, t, tzinfo=tz) for t in times]


def next_run(now: datetime, times: list[time], tz: ZoneInfo) -> datetime:
    """The first scheduled moment strictly after `now`."""
    local = now.astimezone(tz)
    for offset in range(8):
        for slot in _slots_on(local.date() + timedelta(days=offset), times, tz):
            if slot > local:
                return slot
    raise RuntimeError("no scheduled run within a week")


def upcoming_runs(now: datetime, times: list[time], tz: ZoneInfo, days: int = 8) -> list[datetime]:
    """Every scheduled moment after `now` within `days` days, in order."""
    local = now.astimezone(tz)
    return [
        slot
        for offset in range(days + 1)
        for slot in _slots_on(local.date() + timedelta(days=offset), times, tz)
        if slot > local
    ]


def previous_run(now: datetime, times: list[time], tz: ZoneInfo) -> datetime:
    """The last scheduled moment at or before `now`."""
    local = now.astimezone(tz)
    for offset in range(8):
        for slot in reversed(_slots_on(local.date() - timedelta(days=offset), times, tz)):
            if slot <= local:
                return slot
    raise RuntimeError("no scheduled run within the past week")


def latest_snapshot_time() -> datetime | None:
    df = db.load_latest()
    if df.empty:
        return None
    stamp = pd.to_datetime(df["fetched_at"], errors="coerce", utc=True).max()
    return None if pd.isna(stamp) else stamp.to_pydatetime()


def is_stale(snapshot_time: datetime | None, now: datetime, times: list[time], tz: ZoneInfo) -> bool:
    """True when a scheduled run was missed, for example while the service was down."""
    return snapshot_time is None or snapshot_time < previous_run(now, times, tz)


def refresh() -> bool:
    """One pipeline run followed by a page rebuild. Never raises."""
    try:
        result = run_pipeline()
        if not result.get("run_id"):
            logger.error("Pipeline returned no data - the page keeps the previous snapshot.")
            return False
        site.build()
        return True
    except Exception:
        logger.exception("Refresh failed - the page keeps the previous snapshot.")
        return False


def _now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def run_forever() -> None:
    tz = ZoneInfo(config.SCHEDULE_TZ)
    times = parse_times(config.SCHEDULE_TIMES)
    logger.info("Schedule (%s, Mon-Fri): %s", config.SCHEDULE_TZ, ", ".join(t.strftime("%H:%M") for t in times))

    try:
        site.build()  # serve the stored snapshot straight away after a restart
    except Exception:
        logger.exception("Could not build the page from the stored snapshot.")

    if is_stale(latest_snapshot_time(), _now(tz), times, tz):
        logger.info("Stored snapshot is older than the last scheduled run - refreshing now.")
        refresh()

    while True:
        target = next_run(_now(tz), times, tz)
        logger.info("Next run at %s", target.strftime("%Y-%m-%d %H:%M %Z"))
        while (remaining := (target - _now(tz)).total_seconds()) > 0:
            time_module.sleep(min(remaining, _MAX_SLEEP_SECONDS))
        refresh()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_forever()
