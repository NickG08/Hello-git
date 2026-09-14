#!/usr/bin/env python3
"""CLI entrypoint for the scraping pipeline - what cron/systemd/the scheduler
container calls. Run from the repo root: `python scripts/run_pipeline.py`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kzbonds.pipeline import run  # noqa: E402

if __name__ == "__main__":
    result = run()
    print(f"Done. run_id={result.get('run_id')} n_bonds={result.get('n_bonds')}")
