"""
Player / team pages and the game summary, on REAL MLB Stats API responses
captured 2026-10-06/07 (data/sample_pages_and_archive.json): Austin Riley,
Yoshinobu Yamamoto, the Braves, every team's splits vs LHP / RHP.

Every AVG / OBP / SLG / OPS the page helpers compute is checked against
MLB's own displayed value for the same line (292 values, all equal).

Run: python3 scripts/test_pages.py
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
import mlb_api  # noqa: E402
import pages  # noqa: E402

FX = json.loads((ROOT / "data" / "sample_pages_and_archive.json").read_text())
ATL = json.loads((ROOT / "data" / "sample_estimates_atl_vs_yamamoto.json").read_text())
RILEY, YAMAMOTO, BRAVES, DODGERS = 663586, 808967, 144, 119


def test_lines_match_mlb_exactly():
    n, bad = 0, []
    for key in ("league_team_splits", "riley_yearbyyear", "riley_splits_2026", "braves_splits"):
        for block in FX[key]["stats"]:
            for sp in block["splits"]:
                line = pages.hit_line(pages.hit_counts(sp["stat"]))
                for k in ("avg", "obp", "slg", "ops"):
                    n += 1
                    if line[k] != sp["stat"][k]:
                        bad.append((key, k, line[k], sp["stat"][k]))
    for sp in FX["yamamoto_yearbyyear"]["stats"][0]["splits"]:
        line = pages.pit_line(pages.pit_counts(sp["stat"]))
        assert (line["era"], line["whip"]) == (sp["stat"]["era"], sp["stat"]["whip"]), (line, sp["stat"])
    assert not bad and n == 292, (n, bad)
    # League ranks by OPS: Braves 21st vs LHP (.696), 11th vs RHP (.730).
    r = pages.league_ranks(FX["league_team_splits"])
    assert (r["L"][BRAVES], r["R"][BRAVES], r["teams"]["L"]) == (21, 11, 30), r
    print(f"test_lines_match_mlb_exactly OK: {n} values equal MLB's; Braves 21st vs LHP, 11th vs RHP")


def _setup():
    import init_db
    init_db.init_db(db.DB_PATH)
    conn = db.connect()
    db.upsert_team(conn, BRAVES, "Atlanta Braves")
    db.upsert_team(conn, DODGERS, "Los Angeles Dodgers")
    db.upsert_player(conn, YAMAMOTO, "Yoshinobu Yamamoto", role="pitcher")
    db.upsert_player(conn, 519242, "Chris Sale", role="pitcher")
    db.upsert_player(conn, RILEY, "Austin Riley", role="batter")
    conn.execute("INSERT INTO games (game_pk, game_date, game_date_time, game_type, home_team_id, away_team_id, "
                 "home_probable_pitcher_id, away_probable_pitcher_id) VALUES "
                 "(849819, '2026-10-06', '2026-10-06T20:08:00Z', 'D', 144, 119, 519242, 808967)")
    # Riley vs Yamamoto, as stored on the live site: 1-for-5 (2025 1-for-3, 2026 0-for-2).
    db.upsert_matchup_career(conn, {"batter_id": RILEY, "pitcher_id": YAMAMOTO, "plate_appearances": 5,
                                    "at_bats": 5, "hits": 1, "doubles": 1, "triples": 0, "home_runs": 0,
                                    "base_on_balls": 0, "hit_by_pitch": 0, "strike_outs": 2})
    conn.commit()
    conn.close()

    mlb_api.get_person_page = lambda pid: FX["person_riley"] if pid == RILEY else FX["person_yamamoto"]
    mlb_api.get_year_by_year = lambda pid, group: FX["riley_yearbyyear"] if group == "hitting" else FX["yamamoto_yearbyyear"]
    mlb_api.get_hitters_vs_hand = lambda ids, season, code: {"people": [{"id": RILEY, "stats": FX["riley_splits_2026"]["stats"]}]}
    mlb_api.get_hitting_gamelog = lambda pid, season: FX["riley_gamelog"]
    mlb_api.get_pitcher_vs_hand = lambda pid, season: ATL["pitcher_vs"]
    mlb_api.get_pitching_stats = lambda pid, season, types, game_types="R": FX["yamamoto_gamelog"]
    mlb_api.get_person = lambda pid: {"people": [{"id": pid, "fullName": "x",
                                                  "pitchHand": {"code": "R" if pid == YAMAMOTO else "L"}}]}
    mlb_api.get_team_hitting = lambda tid, season: FX["braves_splits"]
    mlb_api.get_league_team_splits = lambda season: FX["league_team_splits"]
    mlb_api.get_team_roster = lambda tid, roster_type="active": ATL["roster"]


def test_player_pages():
    from api.app import app
    c = app.test_client()
    p = c.get(f"/api/player?id={RILEY}&date=2026-10-06").get_json()
    assert p["kind"] == "hitter" and p["position"] == "3B" and p["team"]["name"] == "Atlanta Braves", p
    assert [s["season"] for s in p["seasons"]] == ["2026", "2025", "2024"]
    assert p["seasons"][0]["line"]["ops"] == ".654" and p["vs_hand"]["L"]["ops"] == ".663"
    last7 = p["recent"][0]
    assert (last7["games"], last7["line"]["avg"], last7["line"]["hr"]) == (7, ".091", 1), last7
    assert p["game_log"][0]["date"] == "2026-10-06" and p["game_log"][0]["postseason"]
    # Today: Braves host the Dodgers, Yamamoto (RHP) starting; Riley is 1-for-5 vs him.
    t = p["today"]
    assert t["opponent"] == "Los Angeles Dodgers" and t["opp_starter"]["id"] == YAMAMOTO
    assert t["opp_starter"]["throws"] == "R" and t["h2h"]["ab"] == 5 and t["h2h"]["h"] == 1, t
    assert t["h2h"]["slg"] == ".400", t["h2h"]   # a double: 2 TB / 5 AB

    y = c.get(f"/api/player?id={YAMAMOTO}&date=2026-10-07").get_json()
    assert y["kind"] == "pitcher" and y["role"] == "starter", y
    assert y["seasons"][0]["line"]["era"] == "2.53" and y["vs_hand"]["R"]["avg"] == ".197", y["vs_hand"]
    first = y["recent"][0]
    assert (first["date"], first["ip"], first["so"], first["postseason"]) == ("2026-10-06", "7.0", 10, True), first
    assert len(y["recent"]) == 5
    print("test_player_pages OK: Riley (hitter) and Yamamoto (pitcher)")


def test_team_page_and_summary():
    from api.app import app
    c = app.test_client()
    t = c.get(f"/api/team?id={BRAVES}&date=2026-10-06").get_json()
    h = t["hitting"]
    assert h["season"]["ops"] == ".718" and h["L"]["ops"] == ".696" and h["L_rank"] == 21 and h["R_rank"] == 11, h
    assert h["league"]["L"]["ops"] == ".711" and len(t["hitters"]) == 13, t
    assert t["today"]["opponent"] == "Los Angeles Dodgers"

    s = c.get("/api/summary?game=849819&date=2026-10-06").get_json()
    # Braves (home) face Yamamoto (RHP) -> their vs-RHP line; Dodgers face Sale (LHP).
    assert s["sides"]["home"]["vs"] == "R" and s["sides"]["home"]["line"]["ops"] == ".730"
    assert s["sides"]["home"]["rank"] == 11 and s["sides"]["away"]["vs"] == "L", s
    print("test_team_page_and_summary OK: Braves .696 vs LHP (21st), .730 vs RHP (11th)")


if __name__ == "__main__":
    test_lines_match_mlb_exactly()
    _setup()
    test_player_pages()
    test_team_page_and_summary()
    print("\nAll page tests passed.")
