"""
/api/estimates end to end on REAL data: Braves hitters vs Yoshinobu
Yamamoto, captured 2026-10-06 (data/sample_estimates_atl_vs_yamamoto.json).
MLB and Baseball Savant calls are stubbed to return those real responses,
so everything else -- parsing, the odds-ratio estimate, the pitch-mix
nudge, HR-tonight -- runs exactly as in production.

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
mlb_api.get_hitters_vs_hand = lambda ids, season, code: FX["people"]
mlb_api.get_pitcher_vs_hand = lambda pid, season: FX["pitcher_vs"]
mlb_api.get_league_team_hitting = lambda season: {"stats": [{"splits": [{"stat": FX["league"]}]}]}
mlb_api.get_pitching_stats = lambda pid, season, types, game_types="R": FX["gamelog"]
savant_api.get_arsenal_leaderboard = lambda kind, season: FX["batter_csv"] if kind == "batter" else FX["pitcher_csv"]
savant_api.get_pitcher_pitches = lambda pid, season: "pitch_type,stand\n" + "".join(
    f"{pt},{side}\n" * n for side, counts in FX["usage_counts"].items() for pt, n in counts.items())

from api.app import app  # noqa: E402


def main() -> None:
    resp = app.test_client().get(f"/api/estimates?pitcher={YAMAMOTO}&opponent_team={BRAVES}&date=2026-10-06")
    assert resp.status_code == 200, resp.status_code
    d = resp.get_json()
    assert d["mix_available"] and d["pitcher"]["throws"] == "R"
    # Last 5 starts: 24, 27, 24, 24, 26 batters faced -> 25.0 of ~37.8 team PA.
    assert d["pitcher"]["avg_bf"] == 25.0 and d["pitcher"]["share_of_game"] == 0.66, d["pitcher"]
    rows = d["rows"]
    assert len(rows) == 13 and all(r["est"][k] for r in rows for k in ("avg", "obp", "slg", "ops", "hr_tonight"))

    print(f"{'Batter':20} {'B':1}  est AVG/OBP/SLG  OPS  HR  | real vs RHP (PA)          | mix xwOBA vs usual")
    for r in rows:
        e, v, m = r["est"], r["vs_hand"], r["mix"]
        print(f"{r['name']:20} {r['bats']}  {e['avg']}/{e['obp']}/{e['slg']} {e['ops']} {e['hr_tonight']:>4} | "
              f"{v['avg']}/{v['obp']}/{v['slg']} ({v['pa']:3}) | "
              + (f"{m['xwoba_vs_mix']} vs {m['xwoba_usual']}" if m else "-"))

    by = {r["name"]: r for r in rows}
    # Sanity on real numbers: Baldwin (.888 OPS vs RHP, 391 PA) should rank
    # near the top; Kim (.452 OPS in 81 PA) near the bottom but regressed
    # well above his raw line; Tellez (9 PA) close to league average.
    names = [r["name"] for r in rows]
    assert names.index("Drake Baldwin") <= 2, names
    assert names.index("Ha-Seong Kim") >= len(names) - 3, names
    assert float(by["Ha-Seong Kim"]["est"]["ops"]) > 0.452
    assert abs(by["Rowdy Tellez"]["est"]["ops_num"] - d["league"]["ops_num"]) < 0.08, by["Rowdy Tellez"]["est"]
    # Albies is a switch hitter -> bats left vs Yamamoto -> mix uses his vs-LHH pitches.
    alb = by["Ozzie Albies"]["mix"]["breakdown"]
    assert alb[0]["type"] == "FS" and alb[0]["usage"] == 30, alb  # 459 of 1521 to lefties
    # Splitter-heavy mix vs a hitter with a .181 xwOBA on splitters grades below his usual.
    assert by["Ozzie Albies"]["mix"]["diff_num"] < 0, by["Ozzie Albies"]["mix"]
    print("\nReal-data estimates test passed.")


if __name__ == "__main__":
    main()
