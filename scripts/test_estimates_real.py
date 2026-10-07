"""
/api/estimates end to end on REAL data: Braves hitters vs Yoshinobu
Yamamoto, captured 2026-10-06 (data/sample_estimates_atl_vs_yamamoto.json).
MLB and Baseball Savant calls are stubbed to return those real responses,
so everything else -- parsing, the odds-ratio estimate, the pitch-mix
nudge, HR-tonight -- runs exactly as in production. Includes the 2025 and
2024 seasons (weighted 4 and 3 against this season's 5) and Yamamoto's
pitch mix by game date (last 5 starts blended with his season). v3: every
hitter's lines vs BOTH hands (talent from all at-bats, then a platoon
adjustment) and real plate-appearance counts by lineup spot.

Run: python3 scripts/test_estimates_real.py   (prints the table)
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

import mlb_api  # noqa: E402
import savant_api  # noqa: E402

FX = json.loads((ROOT / "data" / "sample_estimates_atl_vs_yamamoto.json").read_text())
YAMAMOTO, BRAVES = 808967, 144

mlb_api.get_team_roster = lambda team_id, roster_type="active": FX["roster"]
mlb_api.get_person = lambda pid: {"people": [{"id": pid, "fullName": "Yoshinobu Yamamoto",
                                              "pitchHand": {"code": "R"}}]}
HIT_KEYS = ("plateAppearances", "atBats", "hits", "baseOnBalls", "hitByPitch", "strikeOuts", "homeRuns", "totalBases")
PIT_KEYS = ("battersFaced",) + HIT_KEYS[1:]
BATS = {p["id"]: p["batSide"]["code"] for p in FX["people"]["people"]}
NAMES = {p["id"]: p["fullName"] for p in FX["people"]["people"]}


def _people(season: int) -> dict:
    """MLB-shaped /people response with BOTH hands (the endpoint is called
    with sitCodes vl,vr): 2026 vs RHP as captured; everything else rebuilt
    from the captured counting lines (null = no line that year)."""
    if season == 2026:
        vr = {p["id"]: p["stats"][0]["splits"][0]["stat"] for p in FX["people"]["people"] if p.get("stats")}
        vl = {pid: dict(zip(HIT_KEYS, line)) for pid, line in FX["hitters_vs_lhp_2026"] if line}
    else:
        past = FX["past"][str(season)]
        vr = {pid: dict(zip(HIT_KEYS, line)) for pid, line in past["hitters_vs_rhp"] if line}
        vl = {pid: dict(zip(HIT_KEYS, line)) for pid, line in past["hitters_vs_lhp"] if line}
    people = []
    for pid in BATS:
        splits = [{"split": {"code": c}, "stat": d[pid]} for c, d in (("vl", vl), ("vr", vr)) if pid in d]
        people.append({"id": pid, "fullName": NAMES[pid], "batSide": {"code": BATS[pid]},
                       "stats": [{"splits": splits}] if splits else []})
    return {"people": people}


def _pitcher_vs(season: int) -> dict:
    if season == 2026:
        return FX["pitcher_vs"]
    return {"stats": [{"splits": [{"split": {"code": code}, "stat": dict(zip(PIT_KEYS, line))}
                                  for code, line in FX["past"][str(season)]["yamamoto_vs"]]}]}


def _pitch_log(season: int) -> str:
    return "pitch_type,game_date,stand\n" + "".join(
        f"{pt},{day},{side}\n" * n for day, sides in FX["usage_by_date"].items()
        for side, counts in sides.items() for pt, n in counts.items())


def _board(kind: str, season: int) -> str:
    if kind == "pitcher":
        return FX["pitcher_csv"]
    return FX["batter_csv"] if season == 2026 else FX["past"][str(season)]["batter_csv"]


mlb_api.get_hitters_vs_hand = lambda ids, season, code: _people(season)
mlb_api.get_pitcher_vs_hand = lambda pid, season: _pitcher_vs(season)
mlb_api.get_league_team_hitting = lambda season: {"stats": [{"splits": [{"stat": FX["league"]}]}]}
mlb_api.get_pitching_stats = lambda pid, season, types, game_types="R": FX["gamelog"]
savant_api.get_arsenal_leaderboard = _board
savant_api.get_pitcher_pitches = lambda pid, season: _pitch_log(season)

from api.app import app  # noqa: E402


def main() -> None:
    resp = app.test_client().get(f"/api/estimates?pitcher={YAMAMOTO}&opponent_team={BRAVES}&date=2026-10-06")
    assert resp.status_code == 200, resp.status_code
    d = resp.get_json()
    assert d["mix_available"] and d["pitcher"]["throws"] == "R"
    # Last 5 starts: 24, 27, 24, 24, 26 batters faced -> 25.0 of ~37.8 team PA.
    assert d["pitcher"]["avg_bf"] == 25.0 and d["pitcher"]["share_of_game"] == 0.66, d["pitcher"]
    rows = d["rows"]
    assert len(rows) == 13 and all(r["est"][k] for r in rows for k in ("avg", "obp", "slg", "ops", "hr_tonight", "hit_tonight"))

    print(f"{'Batter':20} {'B':1}  est AVG/OBP/SLG  OPS  HR  | real vs RHP (PA)          | mix xwOBA vs usual")
    for r in rows:
        e, v, m = r["est"], r["vs_hand"], r["mix"]
        print(f"{r['name']:20} {r['bats']}  {e['avg']}/{e['obp']}/{e['slg']} {e['ops']} {e['hr_tonight']:>4} | "
              f"{v['avg']}/{v['obp']}/{v['slg']} ({v['pa']:3}) | "
              + (f"{m['xwoba_vs_mix']} vs {m['xwoba_usual']}" if m else "-"))

    by = {r["name"]: r for r in rows}
    names = [r["name"] for r in rows]
    # Sanity on real numbers. Baldwin (.888 OPS vs RHP this year, .808 in
    # 2025) near the top; Albies (under .700 vs RHP all three seasons, and
    # a splitter-heavy mix he grades poorly against) and Kim (.116 overall
    # this year: 15-for-129 vs both hands) at the bottom.
    assert names.index("Drake Baldwin") <= 2 and set(names[-2:]) == {"Ozzie Albies", "Ha-Seong Kim"}, names
    # All at-bats count: Kim's 81 PA vs RHP this year (.452 OPS) are backed
    # by 2024-25 and his at-bats vs lefties, so he's projected above his raw line.
    assert float(by["Ha-Seong Kim"]["est"]["ops"]) > 0.452, by["Ha-Seong Kim"]["est"]
    # Tellez: 9 PA this year (.958 OPS) don't drive it -- 663 PA from
    # 2024-25 (~.700 OPS) do, against an elite pitcher -> below league.
    assert by["Rowdy Tellez"]["est"]["ops_num"] < d["league"]["ops_num"] - 0.05, by["Rowdy Tellez"]["est"]
    # The real 2026 line shown next to it is still this season only.
    assert by["Rowdy Tellez"]["vs_hand"]["pa"] == 9 and by["Rowdy Tellez"]["vs_hand"]["ops"] == ".958"
    # Albies is a switch hitter -> bats left vs Yamamoto -> mix uses his
    # vs-LHH pitches: splitter first. Season share to lefties is 459/1521 =
    # 30.2%; his last 5 starts (8/27-9/23) 77/271 = 28.4%, blended
    # (77 + 100 x 30.2%) / (271 + 100) = 28.9%.
    alb = by["Ozzie Albies"]["mix"]["breakdown"]
    assert alb[0]["type"] == "FS" and alb[0]["usage"] == 29, alb
    assert by["Ozzie Albies"]["mix"]["diff_num"] < 0, by["Ozzie Albies"]["mix"]
    print("\nReal-data estimates test passed.")


if __name__ == "__main__":
    main()
