"""
Homepage (/api/slate), the next-days pull, the site header and /teams --
on MLB's REAL scoreboard for 2026-10-23..28 (World Series, Brewers vs
Rays; Oct 25 is an off day), captured from the Stats API
(data/sample_schedule_ws_2026.json).

Run: python3 scripts/test_home.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["BVP_DATA_DIR"] = tempfile.mkdtemp()
os.environ["SKIP_SCHEDULER"] = "1"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import ingest_daily  # noqa: E402
import mlb_api  # noqa: E402
import parsing  # noqa: E402

WS = json.loads((ROOT / "data" / "sample_schedule_ws_2026.json").read_text())
SERIES = json.loads((ROOT / "data" / "sample_series_ws_2026.json").read_text())


def test_parse_scoreboard():
    rows = parsing.parse_scoreboard(WS)
    assert [r["game_pk"] for r in rows] == [849805, 849802, 849804, 849803, 849799]
    g1, g2, g3, g4, g5 = rows
    assert (g1["state"], g1["away"]["abbr"], g1["away"]["score"], g1["home"]["abbr"], g1["home"]["score"]) == \
        ("final", "TB", 2, "MIL", 3)
    assert g1["home"]["winner"] and not g1["away"]["winner"] and g1["innings"] == 9
    assert g2["series"] == {"label": "World Series · Game 2", "result": "MIL leads 2-0", "if_necessary": False}
    assert g3["state"] == "scheduled" and g3["date"] == "2026-10-26" and g3["time"] == "2026-10-27T00:00:00Z"
    assert g3["away"]["probable"] == {"id": 688107, "name": "Robert Gasser"}
    assert g3["home"]["probable"] == {"id": 642547, "name": "Freddy Peralta"}
    assert g3["venue"]["name"] == "Tropicana Field" and g3["home"]["short"] == "Rays"
    assert g4["away"]["probable"] is None and g5["series"]["if_necessary"] is True
    print("test_parse_scoreboard OK: WS G1-G2 finals, G3 probables, G5 'if necessary'")


def _setup_db():
    import init_db
    init_db.init_db(db.DB_PATH)
    conn = db.connect()
    db.upsert_team(conn, 158, "Milwaukee Brewers")
    db.upsert_team(conn, 139, "Tampa Bay Rays")
    db.upsert_player(conn, 688107, "Robert Gasser", role="pitcher")
    db.upsert_player(conn, 642547, "Freddy Peralta", role="pitcher")
    # G3 has been pulled ahead of time (inside the 3-day window); G4/G5 not yet.
    conn.execute("INSERT INTO games (game_pk, game_date, game_date_time, game_type, home_team_id, away_team_id, "
                 "home_probable_pitcher_id, away_probable_pitcher_id) VALUES "
                 "(849804, '2026-10-26', '2026-10-27T00:00:00Z', 'W', 139, 158, 642547, 688107)")
    conn.commit()
    conn.close()


def test_slate_off_day_shows_whats_next():
    from api import app as A
    _setup_db()
    calls = []

    def fake_board(start, end):
        calls.append((start, end))
        return WS
    mlb_api.get_scoreboard = fake_board
    A._baseball_today = lambda: "2026-10-25"
    c = A.app.test_client()
    d = c.get("/api/slate").get_json()
    assert calls == [("2026-10-21", "2026-11-01")], calls
    assert d["today"] == d["date"] == "2026-10-25" and d["days_ahead_pulled"] == 3
    assert [x["date"] for x in d["days"]] == ["2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28"]
    assert d["days"][0]["games"] == []                                  # off day
    g3 = d["days"][1]["games"][0]
    assert g3["ready"] is True and g3["start_pitcher_id"] == 688107, g3   # links to its game page
    assert g3["away"]["bullpen"] is not None and "label" in g3["away"]["bullpen"]
    assert d["days"][2]["games"][0]["ready"] is False                   # not pulled yet
    assert d["recent"]["date"] == "2026-10-24" and d["recent"]["games"][0]["home"]["score"] == 5
    # Same minute, same call: cached.
    c.get("/api/slate")
    assert len(calls) == 1

    # Browsing a date: just that day, no "coming up"; recent is the day before it.
    b = c.get("/api/slate?date=2026-10-24").get_json()
    assert [x["date"] for x in b["days"]] == ["2026-10-24"] and b["recent"]["date"] == "2026-10-23"
    assert b["days"][0]["games"][0]["state"] == "final"
    assert c.get("/api/slate?date=nope").status_code == 400

    def down(start, end):
        raise RuntimeError("MLB down")
    mlb_api.get_scoreboard = down
    A._cache.clear()
    assert c.get("/api/slate").status_code == 503
    print("test_slate_off_day_shows_whats_next OK: Oct 25 off day -> G3 (ready), G4, G5; last scores Oct 24")


def test_series_so_far():
    """Game 3's page lists the World Series games before it: MLB's real
    Brewers-Rays schedule (gameType W), with R/H/E and W/L/S."""
    from api import app as A
    calls = []
    mlb_api.get_series_games = lambda t, o, season, gt: calls.append((t, o, season, gt)) or SERIES
    conn = db.connect()
    conn.execute("INSERT OR REPLACE INTO games (game_pk, game_date, game_date_time, game_type, home_team_id, away_team_id) "
                 "VALUES (849805, '2026-10-23', '2026-10-24T00:00:00Z', 'W', 158, 139)")
    conn.execute("INSERT OR REPLACE INTO games (game_pk, game_date, game_type, home_team_id, away_team_id) "
                 "VALUES (1, '2026-09-01', 'R', 158, 139)")
    conn.commit()
    conn.close()
    c = A.app.test_client()
    d = c.get("/api/series?game=849804").get_json()
    assert calls == [(139, 158, "2026", "W")], calls
    gs = d["games"]
    assert [g["series_game"] for g in gs] == [1, 2, 3, 4, 5, 6, 7]
    assert [g["current"] for g in gs].index(True) == 2 and [g["ready"] for g in gs][:3] == [True, False, True]
    g1, g2 = gs[0], gs[1]
    assert g1["rhe"] == {"away": {"runs": 2, "hits": 7, "errors": 0}, "home": {"runs": 3, "hits": 8, "errors": 0}}
    assert g1["decisions"]["winner"]["name"] == "Jacob Misiorowski" and g1["decisions"]["save"]["name"] == "Trevor Megill"
    assert g2["rhe"]["home"] == {"runs": 5, "hits": 11, "errors": 1} and g2["decisions"]["loser"]["name"] == "Tyler Wells"
    assert gs[2]["rhe"] is None and gs[2]["decisions"] is None
    assert c.get("/api/series?game=1").get_json()["games"] == []        # regular season: nothing
    assert c.get("/api/series?game=999").status_code == 404
    print("test_series_so_far OK: G3 page -> G1 TB 2-3 MIL (W Misiorowski, S Megill), G2 TB 4-5 MIL")


def test_refresh_upcoming_pulls_next_three_days():
    seen = {"ingest": [], "probables": []}
    orig = (ingest_daily.ingest_date, ingest_daily.refresh_probable_pitchers)
    ingest_daily.ingest_date = lambda d, force=False, backfill_appearances=True: seen["ingest"].append((d, backfill_appearances)) or 1
    ingest_daily.refresh_probable_pitchers = lambda d, backfill_appearances=True: seen["probables"].append((d, backfill_appearances)) or 0
    weather = []
    import venues
    orig_w = venues.refresh_weather
    venues.refresh_weather = lambda conn, g: weather.append(g["game_pk"])
    try:
        out = ingest_daily.refresh_upcoming("2026-10-25")
    finally:
        ingest_daily.ingest_date, ingest_daily.refresh_probable_pitchers = orig
        venues.refresh_weather = orig_w
    days = ["2026-10-26", "2026-10-27", "2026-10-28"]
    assert seen["ingest"] == [(d, False) for d in days] and seen["probables"] == [(d, False) for d in days], seen
    assert weather == [849804], weather          # G3 is stored and has no forecast yet
    assert [o["date"] for o in out] == days
    print("test_refresh_upcoming_pulls_next_three_days OK")


def test_header_and_teams_page():
    from api.app import app
    c = app.test_client()
    home = c.get("/").get_data(as_text=True)
    assert "<title>At the Plate: MLB Batter vs Pitcher Stats &amp; Today&#x27;s Matchups</title>" in home \
        or "<title>At the Plate: MLB Batter vs Pitcher Stats &amp; Today's Matchups</title>" in home, home[:600]
    assert 'rel="canonical" href="http://localhost/"' in home
    assert home.count('class="sh-team"') == 30 and 'href="/team/milwaukee-brewers"' in home
    r = c.get("/teams")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "<h1>All 30 teams</h1>" in html and 'data-ssr="1"' in html
    for div in ("AL East", "AL Central", "AL West", "NL East", "NL Central", "NL West"):
        assert f'<h2 class="pg-title">{div}</h2>' in html, div
    import re
    assert len(re.findall(r'<span class="abbr-chip">[A-Z]+</span>[A-Z]', html)) == 30 and '<span class="abbr-chip">AZ</span>Arizona Diamondbacks' in html
    assert "<loc>http://localhost/teams</loc>" in c.get("/sitemap.xml").get_data(as_text=True)
    print("test_header_and_teams_page OK: header on every page, 30 team links, /teams by division")


if __name__ == "__main__":
    test_parse_scoreboard()
    test_slate_off_day_shows_whats_next()
    test_series_so_far()
    test_refresh_upcoming_pulls_next_three_days()
    test_header_and_teams_page()
    print("\nAll home tests passed.")
