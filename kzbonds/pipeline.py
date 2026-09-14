"""
Orchestrates one full refresh: fetch KASE + AIX concurrently, normalize
into the unified schema, persist a snapshot, regenerate the analytical
note. This is what both the scheduler loop (docker/scheduler_loop.py) and
the dashboard's "Обновить сейчас" button call.
"""

from __future__ import annotations

import asyncio
import logging

import pandas as pd

from kzbonds import analysis, db, normalize
from kzbonds.sources import aix, kase

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("kzbonds.pipeline")


async def _fetch_both() -> tuple[pd.DataFrame, pd.DataFrame]:
    kase_task = kase.fetch_kase_bonds_async()
    aix_task = aix.fetch_aix_bonds_async()
    kase_df, aix_df = await asyncio.gather(kase_task, aix_task, return_exceptions=True)

    if isinstance(kase_df, Exception):
        logger.error("KASE fetch failed: %s", kase_df)
        kase_df = pd.DataFrame()
    if isinstance(aix_df, Exception):
        logger.error("AIX fetch failed: %s", aix_df)
        aix_df = pd.DataFrame()

    return kase_df, aix_df


async def run_async() -> dict:
    logger.info("Fetching KASE + AIX...")
    kase_df, aix_df = await _fetch_both()
    logger.info("KASE: %d bonds, AIX: %d bonds", len(kase_df), len(aix_df))

    unified = normalize.combine(kase_df, aix_df)
    if unified.empty:
        logger.warning("Both sources returned no data - nothing saved.")
        return {"run_id": None, "n_bonds": 0}

    run_id = db.save_snapshot(unified)
    note = analysis.generate_and_save(unified)
    logger.info("Snapshot %s saved (%d bonds). Note regenerated.", run_id, len(unified))

    return {"run_id": run_id, "n_bonds": len(unified), "note": note}


def run() -> dict:
    """Sync entrypoint for scripts/cron/the dashboard's refresh button."""
    return asyncio.run(run_async())


if __name__ == "__main__":
    result = run()
    print(f"Done. run_id={result.get('run_id')} n_bonds={result.get('n_bonds')}")
