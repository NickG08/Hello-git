#!/usr/bin/env python3
"""
Background scheduler for the containerized deployment: runs the pipeline
once at startup, then every PIPELINE_INTERVAL_HOURS. Deliberately not
cron - keeps the scheduler service a single long-running process with no
extra OS packages, identical behavior locally and on a Yandex Cloud VM.
"""

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kzbonds import config
from kzbonds.pipeline import run

if __name__ == "__main__":
    interval_seconds = config.PIPELINE_INTERVAL_HOURS * 3600
    while True:
        try:
            result = run()
            print(
                f"[scheduler] run_id={result.get('run_id')} n_bonds={result.get('n_bonds')}"
            )
        except Exception:
            print("[scheduler] pipeline run failed:")
            traceback.print_exc()
        time.sleep(interval_seconds)
