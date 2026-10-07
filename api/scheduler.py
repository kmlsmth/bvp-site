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

The loop wakes every 10 minutes. Every wake runs the cheap pre-game pass
(ingest_daily.pregame_refresh: pitchers/lineups/weather for games starting
within ~3 hours); every 6th wake (hourly) also runs the daily ingestion
check and the full hourly refresh, as before.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ingest_daily  # noqa: E402

TICK_SECONDS = 10 * 60          # pre-game pass cadence
HOURLY_EVERY_N_TICKS = 6        # 6 x 10 min = the original hourly work


_hooks = {"on_tick": None, "on_hourly": None}


def _run_hook(name: str, today: str) -> None:
    fn = _hooks.get(name)
    if not fn:
        return
    try:
        fn(today)
    except Exception:
        print(f"[scheduler] {name} failed:")
        traceback.print_exc()


def _loop() -> None:
    tick = 0
    while True:
        today = ingest_daily.baseball_today()  # US Eastern, not the server's UTC clock
        if tick % HOURLY_EVERY_N_TICKS == 0:
            _hourly(today)
            _run_hook("on_hourly", today)      # daily archive: final box scores
        # Has its own error handling; a bad pass just waits for the next tick.
        ingest_daily.pregame_refresh(today)
        _run_hook("on_tick", today)            # daily archive: pre-game projections
        tick += 1
        time.sleep(TICK_SECONDS)


def _hourly(today: str) -> None:
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


def start(on_tick=None, on_hourly=None) -> None:
    """on_tick(today) runs after every pre-game pass (every 10 min);
    on_hourly(today) after the hourly work. Both are the daily archive's
    hooks (api/app.py), kept here as callbacks so this module doesn't import
    the web app."""
    _hooks["on_tick"], _hooks["on_hourly"] = on_tick, on_hourly
    thread = threading.Thread(target=_loop, name="daily-ingestion", daemon=True)
    thread.start()
    print("[scheduler] background daily ingestion thread started")
