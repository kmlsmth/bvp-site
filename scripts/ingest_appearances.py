"""
Backfills recent pitcher box-score lines for one team -- the data bullpen
fatigue and starter recent-form trends are both built from (see
scripts/bullpen.py). Shares the same idempotent "pull what's missing, skip
what's already stored" pattern as the rest of ingestion: a completed
game's box score is fetched once, ever, not re-pulled every day it's
still inside the trailing lookback window.

Only ever looks at games *before* the ingestion date -- today's own game
is picked up by tomorrow's run, once it's actually final. That keeps this
simple and avoids ever touching a game still in progress (a live box
score mid-game isn't a useful "last appearance" data point, and re-fetching
it repeatedly would waste calls to a free public API).
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import mlb_api
import parsing

REQUEST_PAUSE_SECONDS = 0.3  # same politeness pause as the rest of ingestion
LOOKBACK_DAYS = 10  # comfortably covers the 7-day window bullpen fatigue needs

# detailedState values that mean "this game actually finished and has a
# real box score to pull" -- anything else (Scheduled, Pre-Game, Live,
# In Progress, Postponed, Suspended) is skipped, not an error.
_COMPLETED_STATUSES = {"final", "game over", "completed early"}


def backfill_team_appearances(conn, team_id: int, as_of_date: str,
                               lookback_days: int = LOOKBACK_DAYS) -> int:
    """Pulls box scores for team_id's completed games in the trailing
    `lookback_days` before as_of_date. Returns how many new games were
    fetched (0 is normal and expected once the window has already been
    backfilled on a prior run)."""
    as_of = date.fromisoformat(as_of_date)
    start = (as_of - timedelta(days=lookback_days)).isoformat()
    end = (as_of - timedelta(days=1)).isoformat()
    if end < start:
        return 0

    try:
        raw = mlb_api.get_team_schedule_range(team_id, start, end)
    except Exception as exc:
        print(f"    appearances: couldn't fetch schedule for team {team_id}: {exc}")
        return 0

    games = parsing.parse_schedule(raw)
    fetched = 0
    for game in games:
        game_pk = game["game_pk"]
        if db.appearance_exists(conn, game_pk):
            continue
        status = (game.get("status") or "").strip().lower()
        if status not in _COMPLETED_STATUSES:
            continue  # not finished yet, or postponed/suspended -- nothing to pull

        try:
            box_raw = mlb_api.get_boxscore(game_pk)
        except Exception as exc:
            print(f"    appearances: couldn't fetch boxscore for game {game_pk}: {exc}")
            continue

        rows = parsing.parse_boxscore(box_raw, game_pk, game["game_date"])
        for row in rows:
            db.upsert_player(conn, row["pitcher_id"], row["pitcher_name"],
                              role="pitcher", team_id=row["team_id"])
            db.upsert_pitcher_appearance(conn, row)
        conn.commit()
        fetched += 1
        time.sleep(REQUEST_PAUSE_SECONDS)

    return fetched


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    if len(sys.argv) <= 2:
        print("Usage: python3 scripts/ingest_appearances.py [YYYY-MM-DD] <team_id>")
        sys.exit(1)
    team_arg = int(sys.argv[2])
    conn = db.connect()
    try:
        n = backfill_team_appearances(conn, team_arg, target)
        print(f"team {team_arg}: {n} new game(s) backfilled")
    finally:
        conn.close()
