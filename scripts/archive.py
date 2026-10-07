"""
Daily data archive: what actually happened, saved from each final box
score, so the site builds its own history from here on:
  - game_results: final score per game;
  - batter_game_results: every hitter's line, with lineup spot and whether
    he started (real games-started data for the plate-appearance model);
  - pitcher_appearances: every pitcher's line (already used by Bullpen
    Watch / recent form -- this fills it for every game, not just the
    10-day backfill for teams playing today).
The site's own pre-game projections are saved separately (projection_log,
written by the API's pre-game snapshot -- see api/app.py).

Idempotent like the rest of ingestion: a game is archived once (once it's
Final) and skipped after that.
"""
from __future__ import annotations

import sys
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import mlb_api  # noqa: E402
import parsing  # noqa: E402

REQUEST_PAUSE_SECONDS = 0.3


def archive_game(conn, game_pk: int, game_date: str, game_type: str | None, box_raw: dict) -> dict:
    """Store one final game's box score. Returns counts."""
    batters = parsing.parse_boxscore_batting(box_raw, game_pk, game_date)
    pitchers = parsing.parse_boxscore(box_raw, game_pk, game_date)
    if not batters or not pitchers:
        # A Final game should always have both; don't mark it archived, so
        # it's retried on the next pass.
        return {"batters": 0, "pitchers": 0, "stored": False}
    teams = box_raw.get("teams") or {}
    for side in ("away", "home"):
        t = (teams.get(side) or {}).get("team") or {}
        if t.get("id"):
            db.upsert_team(conn, t["id"], t.get("name") or "Unknown")
    for row in batters:
        db.upsert_batter_result(conn, row)
    for row in pitchers:
        db.upsert_player(conn, row["pitcher_id"], row["pitcher_name"], role="pitcher", team_id=row["team_id"])
        db.upsert_pitcher_appearance(conn, row)
    db.upsert_game_result(conn, parsing.parse_game_result(box_raw, game_pk, game_date, game_type))
    conn.commit()
    return {"batters": len(batters), "pitchers": len(pitchers), "stored": True}


def archive_date(conn, target_date: str) -> dict:
    """Archive every Final game on target_date that isn't archived yet."""
    counts = {"date": target_date, "final": 0, "archived": 0, "already": 0}
    finals = parsing.parse_schedule_finals(mlb_api.get_schedule(target_date))
    counts["final"] = len(finals)
    for g in finals:
        if db.game_archived(conn, g["game_pk"]):
            counts["already"] += 1
            continue
        try:
            box = mlb_api.get_boxscore(g["game_pk"])
            res = archive_game(conn, g["game_pk"], g["game_date"] or target_date, g["game_type"], box)
            counts["archived"] += int(res["stored"])
        except Exception as exc:
            print(f"[archive] game {g['game_pk']}: {exc!r}")
            traceback.print_exc()
        time.sleep(REQUEST_PAUSE_SECONDS)
    return counts


def archive_recent(today: str, days_back: int = 1) -> list[dict]:
    """Today and the previous `days_back` dates (late games finish after
    midnight Eastern; a missed run is caught up the next hour/day)."""
    conn = db.connect()
    out = []
    try:
        d0 = date.fromisoformat(today)
        for i in range(days_back, -1, -1):
            d = (d0 - timedelta(days=i)).isoformat()
            try:
                c = archive_date(conn, d)
                out.append(c)
                if c["archived"]:
                    print(f"[archive] {d}: {c}")
            except Exception as exc:
                print(f"[archive] {d} failed: {exc!r}")
    finally:
        conn.close()
    return out


if __name__ == "__main__":
    # Backfill a range: python3 scripts/archive.py 2026-09-01 2026-09-30
    start = sys.argv[1]
    end = sys.argv[2] if len(sys.argv) > 2 else start
    conn = db.connect()
    try:
        d = date.fromisoformat(start)
        while d.isoformat() <= end:
            print(archive_date(conn, d.isoformat()))
            d += timedelta(days=1)
    finally:
        conn.close()
