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

    test_live_mlb_endpoints(client)

    print("\nAll API integration tests passed.")


def test_live_mlb_endpoints(client) -> None:
    """/api/pitcher, /api/staff and /api/lineup?fetch=1 call MLB live, so
    the network layer (mlb_api) is stubbed with small canned responses
    shaped like the real ones (field names checked against live calls)."""
    import mlb_api
    calls = {"vs": 0}

    def fake_person(pid):
        return {"people": [{"id": pid, "fullName": "Relief Arm" if pid == 555666 else "Blake Snell",
                            "pitchHand": {"code": "L", "description": "Left"}}]}

    def fake_pitching(pid, season, stat_types, game_types="R"):
        if stat_types == "season,career":
            assert game_types == "R", game_types  # season line is regular season only
            return {"stats": [
                {"type": {"displayName": "season"}, "splits": [{"stat": {
                    "outs": 540, "earnedRuns": 60, "hits": 130, "baseOnBalls": 50,
                    "strikeOuts": 200, "gamesStarted": 30}}]},
                {"type": {"displayName": "career"}, "splits": [{"stat": {
                    "outs": 3000, "earnedRuns": 330, "hits": 800, "baseOnBalls": 400,
                    "strikeOuts": 1200, "gamesStarted": 170}}]},
            ]}
        assert stat_types == "gameLog" and "D" in game_types, (stat_types, game_types)
        log = [{"date": f"2026-09-{d:02d}", "gameType": "R", "game": {"gamePk": d},
                "stat": {"gamesStarted": 1, "outs": 18, "earnedRuns": 2, "hits": 5,
                         "baseOnBalls": 2, "strikeOuts": 7}} for d in (1, 7, 13, 19, 25)]
        log.append({"date": "2026-10-03", "gameType": "D", "game": {"gamePk": 99},
                    "stat": {"gamesStarted": 1, "outs": 15, "earnedRuns": 0, "hits": 3,
                             "baseOnBalls": 1, "strikeOuts": 9}})
        log.append({"date": "2026-09-28", "gameType": "R", "game": {"gamePk": 98},
                    "stat": {"gamesStarted": 0, "outs": 3, "earnedRuns": 4, "hits": 4,
                             "baseOnBalls": 0, "strikeOuts": 0}})  # relief outing: excluded
        return {"stats": [{"type": {"displayName": "gameLog"}, "splits": log}]}

    def fake_roster(team_id, roster_type="active"):
        if team_id == 147:
            return {"roster": [
                {"person": {"id": 605483, "fullName": "Blake Snell"}, "position": {"type": "Pitcher"}},
                {"person": {"id": 111222, "fullName": "Home Reliever"}, "position": {"type": "Pitcher"}},
                {"person": {"id": 555666, "fullName": "Relief Arm"}, "position": {"type": "Pitcher"}},
                {"person": {"id": 147001, "fullName": "Home Hitter"}, "position": {"type": "Outfielder"}},
            ]}
        return {"roster": [
            {"person": {"id": 139001, "fullName": "Away Hitter One"}, "position": {"type": "Infielder"}},
            {"person": {"id": 139002, "fullName": "Away Hitter Two"}, "position": {"type": "Catcher"}},
            {"person": {"id": 333444, "fullName": "Away Reliever"}, "position": {"type": "Pitcher"}},
        ]}

    def fake_vs(batter_id, pitcher_id):
        calls["vs"] += 1
        if batter_id == 139002:
            return {"stats": []}  # never faced him
        return {"stats": [
            {"type": {"displayName": "vsPlayerTotal"}, "splits": [{
                "batter": {"id": batter_id}, "pitcher": {"id": pitcher_id},
                "stat": {"gamesPlayed": 3, "plateAppearances": 7, "atBats": 6, "hits": 2,
                         "homeRuns": 1, "baseOnBalls": 1, "strikeOuts": 2, "avg": ".333"}}]},
            {"type": {"displayName": "vsPlayer"}, "splits": [{
                "season": "2025", "batter": {"id": batter_id}, "pitcher": {"id": pitcher_id},
                "team": {"id": 139, "name": "Tampa Bay Rays"},
                "opponent": {"id": 147, "name": "New York Yankees"},
                "stat": {"gamesPlayed": 3, "plateAppearances": 7, "atBats": 6, "hits": 2,
                         "homeRuns": 1, "baseOnBalls": 1, "strikeOuts": 2, "avg": ".333"}}]},
        ]}

    mlb_api.get_person = fake_person
    mlb_api.get_pitching_stats = fake_pitching
    mlb_api.get_team_roster = fake_roster
    mlb_api.get_vs_player = fake_vs

    # --- /api/pitcher: starter overview card ----------------------------
    resp = client.get("/api/pitcher?id=605483&date=2026-10-05")
    assert resp.status_code == 200, resp.status_code
    p = resp.get_json()
    assert p["throws"] == "L" and p["season_year"] == 2026, p
    # Season: 540 outs = 180 IP, 60 ER -> 3.00 ERA; (130+50)/180 = 1.00 WHIP; 200*9/180 = 10.0
    assert p["season"]["era"] == "3.00" and p["season"]["whip"] == "1.00", p["season"]
    assert p["season"]["k9"] == "10.0" and p["season"]["ip_display"] == "180.0", p["season"]
    assert p["career"]["era"] == "2.97", p["career"]  # 330*9/1000
    last = p["last_starts"]
    # Last 5 STARTS by date: 10-03 (postseason), 09-25, 09-19, 09-13, 09-07 --
    # NOT the 09-28 relief outing and NOT the oldest start (09-01).
    assert last["starts_counted"] == 5 and last["postseason_starts"] == 1, last
    assert last["from_date"] == "2026-09-07" and last["to_date"] == "2026-10-03", last
    # outs 15+18*4 = 87 (29 IP), ER 8 -> 2.48 ERA; 87/5 = 17.4 -> 17 outs -> "5.2"
    assert last["era"] == "2.48" and last["ip_per_start"] == "5.2", last
    print(f"/api/pitcher OK: season ERA {p['season']['era']}, last 5 ERA {last['era']} "
          f"({last['ip_per_start']} IP/start), career {p['career']['era']}")

    # --- /api/staff: whole pitching staff, busiest first ----------------
    resp = client.get("/api/staff?team=147&date=2026-10-05")
    assert resp.status_code == 200, resp.status_code
    staff = resp.get_json()["pitchers"]
    names = [s["name"] for s in staff]
    assert "Home Hitter" not in names, names
    assert names[0] in ("Home Reliever", "Blake Snell"), names  # both pitched twice this week
    assert names[-1] == "Relief Arm" and staff[-1]["appearances"] == 0, staff
    print(f"/api/staff OK: {[(s['name'], s['appearances'], s['pitches']) for s in staff]}")

    # --- /api/lineup?fetch=1: a reliever with no stored history ---------
    resp = client.get("/api/lineup?pitcher=555666&opponent_team=139&fetch=1&pitcher_team=147")
    assert resp.status_code == 200, resp.status_code
    lu = resp.get_json()
    assert lu["pitcher"]["name"] == "Relief Arm", lu["pitcher"]
    assert lu["fetch_warning"] is None, lu["fetch_warning"]
    assert [r["name"] for r in lu["rows"]] == ["Away Hitter One"], lu["rows"]
    assert lu["rows"][0]["stats"]["h"] == 2, lu["rows"][0]["stats"]
    assert [b["name"] for b in lu["no_history"]] == ["Away Hitter Two"], lu["no_history"]
    assert calls["vs"] == 2, calls  # only the two position players, not the pitcher
    # Second view: already fetched -> no new MLB calls.
    client.get("/api/lineup?pitcher=555666&opponent_team=139&fetch=1&pitcher_team=147")
    assert calls["vs"] == 2, calls
    print(f"/api/lineup?fetch=1 OK: {lu['rows'][0]['name']} 2-for-6 vs reliever, cached on re-view")


if __name__ == "__main__":
    main()
