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

ANALYSIS_NOTE_PATH = DATA_DIR / "latest_note.md"
