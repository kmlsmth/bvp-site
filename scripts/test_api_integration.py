"""
End-to-end test of the Flask API against a populated test database --
exercises the actual HTTP layer (routes, query params, JSON shapes), not
just the underlying SQL/stat functions, which is exactly where a wiring
mistake (wrong column name, missing param, bad join) tends to hide.

Run: python3 scripts/test_api_integration.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Point the database at a throwaway temp dir and disable the background
# scheduler BEFORE importing api.app, since both are read at import time.
_tmpdir = tempfile.mkdtemp()
os.environ["BVP_DATA_DIR"] = _tmpdir
os.environ["SKIP_SCHEDULER"] = "1"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import init_db  # noqa: E402

init_db.init_db(db.DB_PATH)

# Seed a minimal but realistic slate: one game today, two teams, one
# venue with park factors set, and a week of bullpen history for each
# team (so the fatigue grade has something real to compute).
conn = db.connect()
conn.execute("INSERT INTO teams (id, name) VALUES (147, 'New York Yankees')")
conn.execute("INSERT INTO teams (id, name) VALUES (139, 'Tampa Bay Rays')")
conn.execute(
    "INSERT INTO venues (id, name, lat, lon, azimuth_angle, hr_factor, hit_factor, updated_at) "
    "VALUES (3, 'Fenway Park', 42.35, -71.10, 45.0, 104.5, 101.2, datetime('now'))"
)
conn.execute(
    "INSERT INTO players (id, full_name, role, team_id) VALUES (605483, 'Blake Snell', 'pitcher', 147)"
)
conn.execute(
    "INSERT INTO games (game_pk, game_date, game_date_time, game_type, status, venue_id, "
    "home_team_id, away_team_id, home_probable_pitcher_id, away_probable_pitcher_id) "
    "VALUES (777001, '2026-10-05', '2026-10-05T23:10:00Z', 'R', 'Scheduled', 3, 147, 139, 605483, NULL)"
)

# Blake Snell's last two starts (role='starter'), inside the lookback
# window -api/lineup's recent_form should average these.
conn.execute(
    "INSERT INTO pitcher_appearances (game_pk, pitcher_id, team_id, game_date, role, outs, "
    "pitches, batters_faced, earned_runs, base_on_balls, strike_outs, hits) "
    "VALUES (700001, 605483, 147, '2026-09-29', 'starter', 18, 94, 23, 2, 1, 7, 4)"
)
conn.execute(
    "INSERT INTO pitcher_appearances (game_pk, pitcher_id, team_id, game_date, role, outs, "
    "pitches, batters_faced, earned_runs, base_on_balls, strike_outs, hits) "
    "VALUES (700002, 605483, 147, '2026-10-04', 'starter', 17, 101, 25, 3, 2, 6, 5)"
)

# A home-team reliever who pitched twice in the trailing window, and an
# away-team reliever used once -- enough to make the two teams' fatigue
# scores actually differ.
conn.execute(
    "INSERT INTO players (id, full_name, role, team_id) VALUES (111222, 'Home Reliever', 'pitcher', 147)"
)
conn.execute(
    "INSERT INTO pitcher_appearances (game_pk, pitcher_id, team_id, game_date, role, outs, pitches) "
    "VALUES (700001, 111222, 147, '2026-09-29', 'reliever', 3, 20)"
)
conn.execute(
    "INSERT INTO pitcher_appearances (game_pk, pitcher_id, team_id, game_date, role, outs, pitches) "
    "VALUES (700002, 111222, 147, '2026-10-04', 'reliever', 3, 22)"
)
conn.execute(
    "INSERT INTO players (id, full_name, role, team_id) VALUES (333444, 'Away Reliever', 'pitcher', 139)"
)
conn.execute(
    "INSERT INTO pitcher_appearances (game_pk, pitcher_id, team_id, game_date, role, outs, pitches) "
    "VALUES (700003, 333444, 139, '2026-09-30', 'reliever', 3, 15)"
)
conn.commit()
conn.close()

from api.app import app as flask_app  # noqa: E402  (must come after DB is seeded/env is set)


def main() -> None:
    client = flask_app.test_client()

    # --- /api/games: park factors + per-team bullpen fatigue -----------
    resp = client.get("/api/games?date=2026-10-05")
    assert resp.status_code == 200, resp.status_code
    games = resp.get_json()
    assert len(games) == 1, games
    g = games[0]
    assert g["venue_hr_factor"] == 104.5, g["venue_hr_factor"]
    assert g["venue_hit_factor"] == 101.2, g["venue_hit_factor"]
    assert g["home_bullpen_fatigue"]["relievers_used"] == 1, g["home_bullpen_fatigue"]
    assert g["home_bullpen_fatigue"]["weekly_pitches"] == 42, g["home_bullpen_fatigue"]  # 20 + 22
    assert g["away_bullpen_fatigue"]["weekly_pitches"] == 15, g["away_bullpen_fatigue"]
    # Home bullpen threw more and more recently -> strictly more fatigued.
    assert g["home_bullpen_fatigue"]["score"] > g["away_bullpen_fatigue"]["score"], (
        g["home_bullpen_fatigue"], g["away_bullpen_fatigue"])
    print(f"/api/games OK: park factors + fatigue -> "
          f"home={g['home_bullpen_fatigue']}, away={g['away_bullpen_fatigue']}")

    # --- /api/bullpen: per-reliever breakdown ---------------------------
    resp = client.get("/api/bullpen?team=147&date=2026-10-05")
    assert resp.status_code == 200, resp.status_code
    bp = resp.get_json()
    assert bp["team"]["name"] == "New York Yankees", bp["team"]
    assert bp["fatigue"]["relievers_used"] == 1
    assert len(bp["relievers"]) == 1
    assert bp["relievers"][0]["id"] == 111222
    assert bp["relievers"][0]["appearances"] == 2
    assert bp["relievers"][0]["pitches"] == 42
    assert bp["relievers"][0]["last_appearance"] == "2026-10-04"
    print(f"/api/bullpen OK: {bp['relievers']}")

    resp = client.get("/api/bullpen?team=147")  # missing required date param
    assert resp.status_code == 400, resp.status_code
    print("/api/bullpen missing-param guard OK (400)")

    # --- /api/lineup: starter recent-form line --------------------------
    resp = client.get("/api/lineup?pitcher=605483&opponent_team=139")
    assert resp.status_code == 200, resp.status_code
    lineup = resp.get_json()
    form = lineup["pitcher"]["recent_form"]
    assert form["starts_counted"] == 2, form
    # Combined: 18+17=35 outs (11.2 IP), 5 ER -> ERA = 5*9/(35/3) = 3.857...
    assert form["era"] == "3.86", form["era"]
    print(f"/api/lineup OK: recent_form={form}")

    print("\nAll API integration tests passed.")


if __name__ == "__main__":
    main()
