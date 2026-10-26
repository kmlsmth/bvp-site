"""
Player / team pages, on REAL MLB Stats API responses
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


def test_team_page():
    from api.app import app
    c = app.test_client()
    t = c.get(f"/api/team?id={BRAVES}&date=2026-10-06").get_json()
    h = t["hitting"]
    assert h["season"]["ops"] == ".718" and h["L"]["ops"] == ".696" and h["L_rank"] == 21 and h["R_rank"] == 11, h
    assert h["league"]["L"]["ops"] == ".711" and len(t["hitters"]) == 13, t
    assert t["today"]["opponent"] == "Los Angeles Dodgers"

    print("test_team_page OK: Braves .696 vs LHP (21st), .730 vs RHP (11th)")


def test_page_urls():
    """Search-friendly addresses: /player/austin-riley-663586, /team/atlanta-braves."""
    import slugs
    assert slugs.slugify("Ronald Acuña Jr.") == "ronald-acuna-jr"
    assert slugs.player_slug("Austin Riley", RILEY) == "austin-riley-663586"
    assert slugs.player_id_from_slug("austin-riley-663586") == RILEY == slugs.player_id_from_slug("663586")
    assert slugs.team_id_from_slug("atlanta-braves") == BRAVES == slugs.team_id_from_slug("144")
    assert slugs.team_slug(133) == "athletics" and slugs.team_id_from_slug("nope") is None
    assert len(slugs.TEAMS) == 30 and len(slugs.TEAM_SLUGS) == 30

    from api.app import app
    c = app.test_client()
    r = c.get("/player/austin-riley-663586")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "<title>Austin Riley Stats, Splits &amp; Matchups | At the Plate</title>" in html
    assert 'rel="canonical" href="http://localhost/player/austin-riley-663586"' in html
    assert '(3B, Atlanta Braves)' in html and 'data-ssr="1"' in html
    # The stats are in the HTML itself (search engines and no-JS readers see them).
    assert "Last 3 seasons" in html and ">.654<" in html and ">.663<" in html, "Riley's 2026 OPS / vs LHP OPS"
    assert 'href="/bvp/austin-riley-vs-yoshinobu-yamamoto-663586-808967"' in html, "faced-most list links to BvP pages"
    # Any other spelling with the right id -> 301 to the canonical address.
    r = c.get("/player/663586")
    assert r.status_code == 301 and r.headers["Location"].endswith("/player/austin-riley-663586"), r.headers
    assert c.get("/player/austin-rily-663586").status_code == 301
    assert c.get("/player/nobody").status_code == 404

    r = c.get("/team/atlanta-braves")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "<title>Atlanta Braves Hitting vs LHP &amp; RHP, Bullpen &amp; Roster" in html
    assert 'data-ssr="1"' in html and ">.696<" in html and ">21st<" in html, "team splits + rank in the HTML"
    assert c.get("/team/144").status_code == 301 and c.get("/team/144").headers["Location"].endswith("/team/atlanta-braves")
    assert c.get("/team/not-a-team").status_code == 404
    # The team page's data by slug, as the front end asks for it.
    t = c.get("/api/team?slug=atlanta-braves&date=2026-10-06").get_json()
    assert t["id"] == BRAVES and t["hitting"]["L_rank"] == 21

    sm = c.get("/sitemap.xml").get_data(as_text=True)
    assert "<loc>http://localhost/team/atlanta-braves</loc>" in sm and "/player/austin-riley-663586</loc>" in sm
    assert sm.count("/team/") == 30 and "Sitemap: http://localhost/sitemap.xml" in c.get("/robots.txt").get_data(as_text=True)
    assert "/bvp/austin-riley-vs-yoshinobu-yamamoto-663586-808967</loc>" in sm   # 5 PA on file -> listed
    print("test_page_urls OK: /player/austin-riley-663586, /team/atlanta-braves, redirects, sitemap")


JUDGE, SNELL = 592450, 605483
VS_JUDGE_SNELL = json.loads((ROOT / "data" / "sample_vsplayer_judge_snell.json").read_text())


def test_bvp_pages():
    """Batter vs pitcher, server-rendered: MLB's real Judge vs Snell vsPlayer
    response (2-for-18, 7 BB, 11 K in 25 PA, 2016-2024)."""
    import slugs
    from api import app as app_module
    assert slugs.bvp_slug("Aaron Judge", JUDGE, "Blake Snell", SNELL) == "aaron-judge-vs-blake-snell-592450-605483"
    assert slugs.bvp_ids_from_slug("aaron-judge-vs-blake-snell-592450-605483") == (JUDGE, SNELL)
    assert slugs.bvp_ids_from_slug("592450-605483") == (JUDGE, SNELL) and slugs.bvp_ids_from_slug("judge") is None

    people = {JUDGE: {"id": JUDGE, "fullName": "Aaron Judge", "batSide": {"code": "R"},
                      "primaryPosition": {"abbreviation": "RF"}, "currentTeam": {"id": 147, "name": "New York Yankees"}},
              SNELL: {"id": SNELL, "fullName": "Blake Snell", "pitchHand": {"code": "L"},
                      "currentTeam": {"id": DODGERS, "name": "Los Angeles Dodgers"}},
              519242: {"id": 519242, "fullName": "Chris Sale", "pitchHand": {"code": "L"},
                       "currentTeam": {"id": BRAVES, "name": "Atlanta Braves"}},
              RILEY: FX["person_riley"]["people"][0], YAMAMOTO: FX["person_yamamoto"]["people"][0]}
    old_page, old_vs = mlb_api.get_person_page, mlb_api.get_vs_player
    mlb_api.get_person_page = lambda pid: {"people": [people[pid]]} if pid in people else {"people": []}
    vs = {(JUDGE, SNELL): VS_JUDGE_SNELL, (RILEY, 519242): {"stats": []}}
    mlb_api.get_vs_player = lambda b, p: vs.get((b, p), {"stats": []})
    try:
        c = app_module.app.test_client()
        r = c.get("/bvp/aaron-judge-vs-blake-snell-592450-605483")
        html = r.get_data(as_text=True)
        assert r.status_code == 200, html[:500]
        assert "<title>Aaron Judge vs Blake Snell: Batter vs Pitcher Stats &amp; History | At the Plate</title>" in html
        assert "Aaron Judge is 2-for-18 (.111), no home runs and 11 strikeouts in 25 career plate appearances " \
               "against Blake Snell (.360 OBP, .111 SLG)." in html
        assert ">Career head-to-head</h2>" in html and ">.471<" in html and ">2024<" in html and ">2016<" in html
        assert 'name="robots"' not in html, "real history -> indexable"
        assert ">Plate Projection</h2>" not in html, "18 AB -> no projection"
        assert 'href="/player/aaron-judge-592450"' in html and 'href="/player/blake-snell-605483"' in html
        # Ids alone or a misspelling -> 301 to the canonical address; junk -> 404.
        r = c.get("/bvp/592450-605483")
        assert r.status_code == 301 and r.headers["Location"].endswith("/bvp/aaron-judge-vs-blake-snell-592450-605483")
        assert c.get("/bvp/judge-vs-snell").status_code == 404
        assert c.get("/bvp/1-2").status_code == 404, "unknown players"
        assert c.get("/matchup/aaron-judge-vs-blake-snell-592450-605483").status_code == 404, "/matchup/ is for teams"

        # Never faced: the page still works but is noindex, with no career table.
        # (Riley vs Sale: teammates, so no projection either.)
        r = c.get("/bvp/austin-riley-vs-chris-sale-663586-519242")
        if r.status_code == 301:
            r = c.get(r.headers["Location"])
        html = r.get_data(as_text=True)
        assert r.status_code == 200 and '<meta name="robots" content="noindex">' in html, (r.status_code, html[-1500:])
        assert "has never faced" in html and ">Career head-to-head</h2>" not in html and ">Plate Projection</h2>" not in html
    finally:
        mlb_api.get_person_page, mlb_api.get_vs_player = old_page, old_vs
    print("test_bvp_pages OK: Judge vs Snell 2-for-18 rendered on the server; no-history page noindex")


if __name__ == "__main__":
    test_lines_match_mlb_exactly()
    _setup()
    test_player_pages()
    test_team_page()
    test_page_urls()
    test_bvp_pages()
    print("\nAll page tests passed.")
