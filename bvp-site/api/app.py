"""
Minimal backend API for the batter-vs-pitcher tool.

Built on Flask (already available, no extra install needed) + the plain
sqlite3 database the ingestion job fills in. Three endpoints:

  GET /api/games?date=YYYY-MM-DD
      Today's (or any date's) games with probable starting pitchers.

  GET /api/matchup?batter=<id>&pitcher=<id>
      Career totals + season-by-season history for one batter vs one
      pitcher.

  GET /api/players?q=<search text>
      Name search, so a front end can let someone type "Judge" and get
      back matching players with their ids.

In production, this process also runs a background thread (see
scheduler.py) that keeps the database topped up daily -- there is no
separate cron service, since Railway can't share one disk between two
services. Set SKIP_SCHEDULER=1 to disable that thread (handy for local
dev when you'd rather run scripts/ingest_daily.py by hand).

Run locally:  python3 api/app.py   (serves on http://localhost:8000)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parent.parent
# Make both scripts/ and this file's own directory importable regardless of
# whether this module is run directly (python3 api/app.py) or imported as
# api.app (production, via wsgi.py) -- in the latter case Python does not
# automatically add api/ itself to sys.path the way it does for a script.
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as db_module  # noqa: E402  (shares BVP_DATA_DIR / DB_PATH logic)
import init_db as init_db_module  # noqa: E402

DB_PATH = db_module.DB_PATH

# Idempotent: CREATE TABLE IF NOT EXISTS, so this is safe to run on every
# boot. Makes sure the database (and its parent dir, e.g. a fresh Railway
# volume) exists before anything tries to query it.
init_db_module.init_db(DB_PATH)

app = Flask(__name__)

if not os.environ.get("SKIP_SCHEDULER"):
    import scheduler
    scheduler.start()


def query_db(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/games")
def games():
    game_date = request.args.get("date")
    if not game_date:
        return jsonify({"error": "date query param is required, e.g. ?date=2026-10-02"}), 400

    rows = query_db(
        """
        SELECT g.game_pk, g.game_date, g.game_type, g.status,
               ht.name AS home_team, at.name AS away_team,
               hp.full_name AS home_probable_pitcher, g.home_probable_pitcher_id,
               ap.full_name AS away_probable_pitcher, g.away_probable_pitcher_id
        FROM games g
        LEFT JOIN teams ht ON ht.id = g.home_team_id
        LEFT JOIN teams at ON at.id = g.away_team_id
        LEFT JOIN players hp ON hp.id = g.home_probable_pitcher_id
        LEFT JOIN players ap ON ap.id = g.away_probable_pitcher_id
        WHERE g.game_date = ?
        ORDER BY g.game_pk
        """,
        (game_date,),
    )
    return jsonify(rows)


@app.get("/api/matchup")
def matchup():
    batter_id = request.args.get("batter", type=int)
    pitcher_id = request.args.get("pitcher", type=int)
    if not batter_id or not pitcher_id:
        return jsonify({"error": "batter and pitcher query params (player ids) are required"}), 400

    career = query_db(
        """
        SELECT mc.*, b.full_name AS batter_name, p.full_name AS pitcher_name
        FROM matchup_career mc
        JOIN players b ON b.id = mc.batter_id
        JOIN players p ON p.id = mc.pitcher_id
        WHERE mc.batter_id = ? AND mc.pitcher_id = ?
        """,
        (batter_id, pitcher_id),
    )
    seasons = query_db(
        """
        SELECT ms.*, ot.name AS opponent_name
        FROM matchup_season ms
        LEFT JOIN teams ot ON ot.id = ms.opponent_id
        WHERE ms.batter_id = ? AND ms.pitcher_id = ?
        ORDER BY ms.season
        """,
        (batter_id, pitcher_id),
    )

    if not career:
        return jsonify({"career": None, "seasons": [], "message": "No recorded history between these two players."})

    return jsonify({"career": career[0], "seasons": seasons})


@app.get("/api/players")
def players():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"error": "q (search text) must be at least 2 characters"}), 400

    rows = query_db(
        "SELECT id, full_name, role, team_id FROM players WHERE full_name LIKE ? ORDER BY full_name LIMIT 25",
        (f"%{q}%",),
    )
    return jsonify(rows)


@app.get("/api/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
