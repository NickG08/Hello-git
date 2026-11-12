"""Central configuration, loaded from environment (.env)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("KZBONDS_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Where the unified snapshots live. Defaults to a local SQLite file under
# data/ so the whole thing runs with zero external services; point DB_URL
# at Postgres (e.g. Yandex Managed PostgreSQL) later without code changes.
DB_URL = os.getenv("DB_URL", f"sqlite:///{DATA_DIR / 'kzbonds.db'}")

KASE_CACHE_FILE = DATA_DIR / "kase_characteristics_cache.json"
AIX_CACHE_FILE = DATA_DIR / "aix_profiles_cache.json"

KASE_CACHE_HOURS = int(os.getenv("KASE_CACHE_HOURS", "24"))
AIX_CACHE_DAYS = int(os.getenv("AIX_CACHE_DAYS", "7"))

MAX_CONCURRENT_REQUESTS = int(os.getenv("KZBONDS_MAX_CONCURRENT_REQUESTS", "15"))
REQUEST_TIMEOUT = int(os.getenv("KZBONDS_REQUEST_TIMEOUT", "20"))

# How often the background scheduler (docker/scheduler_loop.py) refreshes
# data, in hours. Both exchanges only reprice once per trading session, so
# there is no point polling more often than this.
PIPELINE_INTERVAL_HOURS = float(os.getenv("KZBONDS_PIPELINE_INTERVAL_HOURS", "6"))

# A yield is only shown when the price behind it comes from a deal at most
# this many days old. Measured on KASE: within 30 days an old price shifts
# the yield by a few hundredths of a point (1.5 pp at worst); past 90 days
# the error reaches whole points, and bonds close to maturity blow up.
YTM_MAX_PRICE_AGE_DAYS = int(os.getenv("KZBONDS_YTM_MAX_PRICE_AGE_DAYS", "30"))

# The note's top-yields list leaves out bonds closer to maturity than this. A
# price a few weeks old, annualised over the few weeks left, gives a yield of
# tens of percent that nobody can actually earn. The table still shows them.
NOTE_MIN_DAYS_TO_MATURITY = int(os.getenv("KZBONDS_NOTE_MIN_DAYS_TO_MATURITY", "90"))

ANALYSIS_NOTE_PATH = DATA_DIR / "latest_note.md"

# When kzbonds.scheduler refreshes the data: these times, Monday to Friday,
# in SCHEDULE_TZ (Astana time).
#   10:45  KASE opens direct deals at 10:30; nothing trades before that.
#   11:40  both exchanges hold their opening auction at 11:30, and KASE
#          publishes in batches every 3-6 minutes.
#   hourly through the session.
#   17:30  AIX closes at 17:05 and updates its reference prices by 17:20.
#   17:50  KASE closes at 17:30; its last changes arrived at 17:39 when measured.
SCHEDULE_TZ = os.getenv("KZBONDS_SCHEDULE_TZ", "Asia/Almaty")
SCHEDULE_TIMES = os.getenv(
    "KZBONDS_SCHEDULE_TIMES", "10:45,11:40,12:45,13:45,14:45,15:45,16:45,17:30,17:50"
)

# The page kzbonds.site builds and kzbonds.serve hands out.
SITE_INDEX_PATH = DATA_DIR / "site" / "index.html"
