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

from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent.parent
# Make both scripts/ and this file's own directory importable regardless of
# whether this module is run directly (python3 api/app.py) or imported as
# api.app (production, via wsgi.py) -- in the latter case Python does not
# automatically add api/ itself to sys.path the way it does for a script.
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as db_module  # noqa: E402  (shares BVP_DATA_DIR / DB_PATH logic)
import init_db as init_db_module  # noqa: E402
import stats as stats_module  # noqa: E402

DB_PATH = db_module.DB_PATH
WEB_DIR = ROOT / "web"

# Idempotent: CREATE TABLE IF NOT EXISTS, so this is safe to run on every
# boot. Makes sure the database (and its parent dir, e.g. a fresh Railway
# volume) exists before anything tries to query it.
init_db_module.init_db(DB_PATH)

app = Flask(__name__)

if not os.environ.get("SKIP_SCHEDULER"):
    import scheduler
    scheduler.start()


@app.get("/")
def index():
    """The whole front end is one self-contained page (web/index.html) --
    it does its own routing client-side (see the hash-based router in that
    file), so this is the only page route the server needs."""
    return send_from_directory(WEB_DIR, "index.html")


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
        SELECT g.game_pk, g.game_date, g.game_date_time, g.game_type, g.status,
               g.home_team_id, ht.name AS home_team,
               g.away_team_id, at.name AS away_team,
               hp.full_name AS home_probable_pitcher, g.home_probable_pitcher_id,
               ap.full_name AS away_probable_pitcher, g.away_probable_pitcher_id,
               v.id AS venue_id, v.name AS venue_name, v.azimuth_angle AS venue_azimuth_angle,
               v.roof_type AS venue_roof_type,
               gw.wind_speed_mph, gw.wind_dir_deg, gw.wind_dir_compass,
               gw.temp_f, gw.sky, gw.forecast_time
        FROM games g
        LEFT JOIN teams ht ON ht.id = g.home_team_id
        LEFT JOIN teams at ON at.id = g.away_team_id
        LEFT JOIN players hp ON hp.id = g.home_probable_pitcher_id
        LEFT JOIN players ap ON ap.id = g.away_probable_pitcher_id
        LEFT JOIN venues v ON v.id = g.venue_id
        LEFT JOIN game_weather gw ON gw.game_pk = g.game_pk
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


@app.get("/api/lineup")
def lineup():
    """Every batter on one team's roster against one probable pitcher, in
    a single call -- what the front end's full stat-line table needs.

    Fetching this one batter/pitcher pair at a time (like /api/matchup
    above) would mean ~13 separate calls per lineup table; this does it
    in one, which is what makes the pitcher-switcher dropdown feel
    instant instead of spinning for a few seconds on every switch.
    """
    pitcher_id = request.args.get("pitcher", type=int)
    opponent_team_id = request.args.get("opponent_team", type=int)
    if not pitcher_id or not opponent_team_id:
        return jsonify({"error": "pitcher and opponent_team query params (ids) are required"}), 400

    pitcher_rows = query_db("SELECT full_name FROM players WHERE id = ?", (pitcher_id,))
    team_rows = query_db("SELECT name FROM teams WHERE id = ?", (opponent_team_id,))
    pitcher_name = pitcher_rows[0]["full_name"] if pitcher_rows else None
    team_name = team_rows[0]["name"] if team_rows else None

    roster = query_db(
        """
        SELECT id, full_name FROM players
        WHERE team_id = ? AND role IN ('batter', 'both')
        ORDER BY full_name
        """,
        (opponent_team_id,),
    )

    careers = {
        row["batter_id"]: row
        for row in query_db("SELECT * FROM matchup_career WHERE pitcher_id = ?", (pitcher_id,))
    }
    season_rows = query_db(
        """
        SELECT ms.*, ot.name AS opponent_name
        FROM matchup_season ms
        LEFT JOIN teams ot ON ot.id = ms.opponent_id
        WHERE ms.pitcher_id = ?
        ORDER BY ms.season
        """,
        (pitcher_id,),
    )
    seasons_by_batter: dict[int, list[dict]] = {}
    for s in season_rows:
        seasons_by_batter.setdefault(s["batter_id"], []).append(s)

    with_history = []
    no_history = []
    for batter in roster:
        career = careers.get(batter["id"])
        if career is None:
            no_history.append({"id": batter["id"], "name": batter["full_name"]})
            continue
        seasons = [
            {
                "season": s["season"],
                "opponent_name": s["opponent_name"],
                **stats_module.compute_batting_stats(s),
            }
            for s in seasons_by_batter.get(batter["id"], [])
        ]
        with_history.append({
            "id": batter["id"],
            "name": batter["full_name"],
            "stats": stats_module.compute_batting_stats(career),
            "seasons": seasons,
        })

    with_history.sort(key=lambda b: b["stats"]["pa"] or 0, reverse=True)

    team_totals_raw = stats_module.sum_raw_counts(
        [careers[b["id"]] for b in roster if b["id"] in careers]
    )
    team_totals = stats_module.compute_batting_stats(team_totals_raw)

    return jsonify({
        "pitcher": {"id": pitcher_id, "name": pitcher_name},
        "opponent_team": {"id": opponent_team_id, "name": team_name},
        "rows": with_history,
        "no_history": no_history,
        "team_totals": team_totals,
    })


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
