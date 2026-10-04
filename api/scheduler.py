"""
In-process daily scheduler.

Why this exists: Railway (our hosting target) doesn't let a "cron job"
service and a "web" service share a persistent volume — each service gets
its own separate disk. Rather than run two services and a second database,
we run one service that both serves the API and, in a background thread,
checks once an hour whether today's game data has been pulled yet. If not,
it runs the ingestion job.

This is deliberately simple (a sleep loop, not a real cron library) and
safe to restart: `ingest_date` checks the ingestion_runs table before doing
any work, so a redeploy or crash never causes duplicate runs or a stuck
state — it just checks again next hour.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ingest_daily  # noqa: E402

CHECK_INTERVAL_SECONDS = 60 * 60  # check once an hour


def _loop() -> None:
    while True:
        today = date.today().isoformat()
        try:
            ingest_daily.ingest_date(today)
        except Exception:
            # Don't let a bad day (API hiccup, unexpected response shape)
            # take down the scheduler thread -- just log and try again
            # next hour.
            print(f"[scheduler] ingestion for {today} failed:")
            traceback.print_exc()
        # ingest_date() above only does its full (expensive) work once per
        # date -- so on every other hourly tick this is what actually
        # catches a probable pitcher MLB announces later in the day (it
        # has its own internal error handling, so no try/except needed
        # here). See ingest_daily.refresh_probable_pitchers for why this
        # is a separate, cheap pass rather than folded into ingest_date.
        ingest_daily.refresh_probable_pitchers(today)
        time.sleep(CHECK_INTERVAL_SECONDS)


def start() -> None:
    thread = threading.Thread(target=_loop, name="daily-ingestion", daemon=True)
    thread.start()
    print("[scheduler] background daily ingestion thread started")
