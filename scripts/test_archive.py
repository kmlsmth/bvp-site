"""
Daily data archive, on a REAL final box score: Dodgers 3 @ Braves 1,
2026-10-06 (game 849819), captured from the MLB Stats API
(data/sample_pages_and_archive.json).

Run: python3 scripts/test_archive.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["BVP_DATA_DIR"] = tempfile.mkdtemp()
os.environ["SKIP_SCHEDULER"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import archive  # noqa: E402
import db  # noqa: E402
import mlb_api  # noqa: E402
import parsing  # noqa: E402

FX = json.loads((ROOT / "data" / "sample_pages_and_archive.json").read_text())
BOX = FX["boxscore_849819"]


def test_parse_batting_real_box():
    rows = parsing.parse_boxscore_batting(BOX, 849819, "2026-10-06")
    by = {r["batter_id"]: r for r in rows}
    assert len(rows) == 23 and sum(r["started"] for r in rows) == 18, len(rows)
    # Hits add up to each team's total (Dodgers 8, Braves 5).
    assert sum(r["h"] for r in rows if not r["is_home"]) == 8
    assert sum(r["h"] for r in rows if r["is_home"]) == 5
    # Ha-Seong Kim started 9th and was pinch-hit for after 1 PA; Tellez
    # ("901") and Hicklen ("902") followed him in that spot.
    assert (by[673490]["batting_order"], by[673490]["started"], by[673490]["pa"]) == (9, 1, 1)
    assert (by[642133]["batting_order"], by[642133]["started"]) == (9, 0)
    assert (by[676551]["batting_order"], by[676551]["started"]) == (9, 0)
    # Kiké Hernández: 1-for-4 with a home run, 2 RBI, batting 9th for LA.
    k = by[571771]
    assert (k["batting_order"], k["h"], k["hr"], k["rbi"], k["is_home"]) == (9, 1, 1, 2, 0)
    res = parsing.parse_game_result(BOX, 849819, "2026-10-06", "D")
    assert (res["away_runs"], res["home_runs"], res["away_hits"], res["home_hits"]) == (3, 1, 8, 5)
    print("test_parse_batting_real_box OK: 23 hitters, 18 starters, 8 + 5 hits, Kim 9th 1 PA")


def test_schedule_finals_only():
    raw = {"dates": [{"date": "2026-10-06", "games": [
        {"gamePk": 1, "officialDate": "2026-10-06", "gameType": "D",
         "status": {"abstractGameState": "Final", "codedGameState": "F", "detailedState": "Final"}},
        {"gamePk": 2, "officialDate": "2026-10-06", "gameType": "D",
         "status": {"abstractGameState": "Live", "codedGameState": "I", "detailedState": "In Progress"}},
        {"gamePk": 3, "officialDate": "2026-10-06", "gameType": "R",
         "status": {"abstractGameState": "Final", "codedGameState": "D", "detailedState": "Postponed"}},
        {"gamePk": 4, "officialDate": "2026-10-06", "gameType": "R",
         "status": {"abstractGameState": "Final", "codedGameState": "O", "detailedState": "Game Over"}},
    ]}]}
    assert [g["game_pk"] for g in parsing.parse_schedule_finals(raw)] == [1, 4]
    print("test_schedule_finals_only OK")


def test_archive_and_projection_grading():
    import init_db
    init_db.init_db(db.DB_PATH)
    calls = {"box": 0}

    def fake_box(pk):
        calls["box"] += 1
        return BOX
    mlb_api.get_boxscore = fake_box
    mlb_api.get_schedule = lambda d: {"dates": [{"date": d, "games": [
        {"gamePk": 849819, "officialDate": d, "gameType": "D",
         "status": {"abstractGameState": "Final", "codedGameState": "F", "detailedState": "Final"}}]}]}
    conn = db.connect()
    c1 = archive.archive_date(conn, "2026-10-06")
    c2 = archive.archive_date(conn, "2026-10-06")   # already archived -> no second box score pull
    assert c1["archived"] == 1 and c2["already"] == 1 and calls["box"] == 1, (c1, c2, calls)
    n_bat = conn.execute("SELECT COUNT(*) FROM batter_game_results").fetchone()[0]
    n_pit = conn.execute("SELECT COUNT(*) FROM pitcher_appearances WHERE game_pk = 849819").fetchone()[0]
    assert n_bat == 23 and n_pit == 8, (n_bat, n_pit)   # Dodgers 3 pitchers + Braves 5
    sale = conn.execute("SELECT role, outs, pitches, strike_outs FROM pitcher_appearances "
                        "WHERE pitcher_id = 519242").fetchone()
    assert sale == ("starter", 15, 95, 7), sale
    conn.close()

    # Pre-game snapshot: a game 1 hour away with the Braves lineup posted.
    from api import app as app_module
    start = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lineup = json.dumps([{"order": i + 1, "id": pid} for i, pid in
                         enumerate(BOX["teams"]["home"]["battingOrder"])])
    conn = db.connect()
    db.upsert_team(conn, 144, "Atlanta Braves")
    db.upsert_team(conn, 119, "Los Angeles Dodgers")
    conn.execute("INSERT INTO games (game_pk, game_date, game_date_time, home_team_id, away_team_id, "
                 "home_lineup) VALUES (849819, '2026-10-06', ?, 144, 119, ?)", (start, lineup))
    conn.execute("UPDATE games SET away_probable_pitcher_id = 808967 WHERE game_pk = 849819")
    conn.commit()
    conn.close()

    def fake_estimates(pitcher_id, team, date, game_pk=None, is_reliever=False):
        rows = [{"id": pid, "lineup_spot": i + 1,
                 "est": {"avg_num": .22, "obp_num": .29, "slg_num": .36, "hit_tonight_num": .55 + i * .02,
                         "hr_tonight_num": .1, "pa_expected": 4.1}}
                for i, pid in enumerate(BOX["teams"]["home"]["battingOrder"])]
        rows.append({"id": 999, "lineup_spot": None, "est": {}})   # bench: not logged
        return {"rows": rows, "pitcher": {"share_of_game": .66}, "model_version": "v3", "batting_home": True}
    app_module.compute_estimates = fake_estimates
    n = app_module.snapshot_pregame_projections("2026-10-06")
    assert n == 9, n
    # Same pass again overwrites (still 9 rows, not 18).
    app_module.snapshot_pregame_projections("2026-10-06")
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM projection_log").fetchone()[0] == 9
    conn.close()

    rep = app_module.app.test_client().get("/api/archive").get_json()
    assert rep["counts"]["games"]["n"] == 1 and rep["counts"]["batter_lines"] == 23
    # Graded only against hitters who actually started. The fake lineup
    # above came from the box score's end-of-game batting order, which has
    # two subs in it (Yastrzemski, Hicklen), so 7 of the 9 are graded.
    assert rep["graded"] == 7, rep
    total_hits = sum(round(b["actual"] * b["n"]) for b in rep["hit_calibration"])
    assert total_hits == 4, rep   # Baldwin 0, Acuña 0, Olson 1, Albies 1, Harris 1, Dubón 1, Riley 0
    print(f"test_archive_and_projection_grading OK: {rep['hit_calibration']}")


if __name__ == "__main__":
    test_parse_batting_real_box()
    test_schedule_finals_only()
    test_archive_and_projection_grading()
    print("\nAll archive tests passed.")
