"""
Storage layer. Every pipeline run appends a full snapshot (tagged with a
run_id/fetched_at) rather than overwriting - this is what lets the
dashboard show trend/history later even though the exact field set and
filters aren't final yet: new columns can be added without breaking old
snapshots (missing values just read back as NULL).

Backed by SQLite by default (DB_URL in .env); swapping to Postgres later
(e.g. Yandex Managed PostgreSQL) is a one-line env change, no code change,
since everything goes through a SQLAlchemy engine.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import create_engine, text

from kzbonds import config

TABLE = "bond_snapshots"

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(config.DB_URL)
    return _engine


def save_snapshot(df: pd.DataFrame) -> str:
    """Append a unified-schema DataFrame as one new snapshot. Returns run_id."""
    if df.empty:
        return ""
    run_id = uuid.uuid4().hex[:12]
    fetched_at = datetime.now(UTC).isoformat()

    out = df.copy()
    out.insert(0, "run_id", run_id)
    out.insert(1, "fetched_at", fetched_at)
    out["maturity_date"] = pd.to_datetime(
        out["maturity_date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")

    engine = get_engine()
    out.to_sql(TABLE, engine, if_exists="append", index=False)
    return run_id


def latest_run_id() -> str | None:
    engine = get_engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT run_id FROM {TABLE} ORDER BY fetched_at DESC LIMIT 1")
            ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def load_latest() -> pd.DataFrame:
    """Rows from the most recent pipeline run, or empty frame if none yet."""
    run_id = latest_run_id()
    if not run_id:
        return pd.DataFrame()
    engine = get_engine()
    df = pd.read_sql(
        text(f"SELECT * FROM {TABLE} WHERE run_id = :rid"),
        engine,
        params={"rid": run_id},
    )
    df["maturity_date"] = pd.to_datetime(df["maturity_date"], errors="coerce")
    return df


def load_history(ticker: str | None = None) -> pd.DataFrame:
    """All snapshots (optionally for one ticker), for trend charts."""
    engine = get_engine()
    try:
        if ticker:
            df = pd.read_sql(
                text(f"SELECT * FROM {TABLE} WHERE ticker = :t ORDER BY fetched_at"),
                engine,
                params={"t": ticker},
            )
        else:
            df = pd.read_sql(text(f"SELECT * FROM {TABLE} ORDER BY fetched_at"), engine)
        df["fetched_at"] = pd.to_datetime(df["fetched_at"], errors="coerce")
        df["maturity_date"] = pd.to_datetime(df["maturity_date"], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


def list_runs() -> pd.DataFrame:
    engine = get_engine()
    try:
        return pd.read_sql(
            text(
                f"SELECT run_id, fetched_at, COUNT(*) as n_bonds FROM {TABLE} GROUP BY run_id, fetched_at ORDER BY fetched_at DESC"
            ),
            engine,
        )
    except Exception:
        return pd.DataFrame(columns=["run_id", "fetched_at", "n_bonds"])
