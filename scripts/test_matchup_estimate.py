"""
Matchup estimate math, on REAL MLB numbers captured live 2026-10-06:
Aaron Judge and Yandy Díaz vs right-handed pitchers (2026 regular season),
Cam Schlittler vs right-handed hitters, and the 30 teams' summed 2026
hitting totals for the league baseline.

The Judge expectation was also worked by hand: regressed hitter OB rate
(69 + .3171*460)/(204+460) = .3236; regressed pitcher OB rate
(70 + .3171*540)/(303+540) = .2861; odds ratio -> .292.

Run: python3 scripts/test_matchup_estimate.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import matchup_estimate as m

# 30 teams summed: PA 183,849; H 39,849; BB 16,337; HBP 2,118; SO 40,700; HR 5,575
LEAGUE = m.league_rates([{"plateAppearances": 183849, "hits": 39849, "baseOnBalls": 16337,
                          "hitByPitch": 2118, "strikeOuts": 40700, "homeRuns": 5575}])
JUDGE_VS_RHP = {"plateAppearances": 204, "hits": 42, "baseOnBalls": 25, "hitByPitch": 2,
                "strikeOuts": 63, "homeRuns": 12}
DIAZ_VS_RHP = {"plateAppearances": 484, "hits": 133, "baseOnBalls": 36, "hitByPitch": 8,
               "strikeOuts": 74, "homeRuns": 14}
SCHLITTLER_VS_RHH = {"battersFaced": 303, "hits": 51, "baseOnBalls": 16, "hitByPitch": 3,
                     "strikeOuts": 80, "homeRuns": 7}


def r(x, n=3):
    return round(x, n)


def test_league_baseline():
    assert (r(LEAGUE["ob"], 4), r(LEAGUE["so"], 4), r(LEAGUE["hr"], 4)) == (0.3171, 0.2214, 0.0303), LEAGUE
    print(f"test_league_baseline OK: {LEAGUE}")


def test_judge_vs_schlittler():
    est = m.estimate(m.hitting_counts(JUDGE_VS_RHP), m.pitching_counts(SCHLITTLER_VS_RHH), LEAGUE)
    assert (r(est["ob"]), r(est["so"]), r(est["hr"])) == (0.292, 0.330, 0.044), est
    print(f"test_judge_vs_schlittler OK: {est}")


def test_contact_hitter_strikes_out_less():
    judge = m.estimate(m.hitting_counts(JUDGE_VS_RHP), m.pitching_counts(SCHLITTLER_VS_RHH), LEAGUE)
    diaz = m.estimate(m.hitting_counts(DIAZ_VS_RHP), m.pitching_counts(SCHLITTLER_VS_RHH), LEAGUE)
    assert diaz["so"] < judge["so"] and diaz["hr"] < judge["hr"], (diaz, judge)
    assert r(diaz["ob"]) == 0.310, diaz
    print(f"test_contact_hitter_strikes_out_less OK: Díaz K {diaz['so']:.3f} < Judge K {judge['so']:.3f}")


def test_no_data_falls_back_to_league():
    est = m.estimate(None, None, LEAGUE)
    assert all(abs(est[k] - LEAGUE[k]) < 1e-12 for k in m.RATES), est
    # A tiny sample barely moves the estimate (2-for-2 is not a 1.000 hitter).
    tiny = m.estimate(m.hitting_counts({"plateAppearances": 2, "hits": 2, "homeRuns": 2}), None, LEAGUE)
    assert tiny["ob"] < 0.325 and tiny["hr"] < 0.045, tiny  # league: .317 / .030
    print(f"test_no_data_falls_back_to_league OK (2-for-2 with 2 HR -> est OB {tiny['ob']:.3f}, HR {tiny['hr']:.3f})")


def test_switch_hitters_turn_around():
    assert m.facing_side("S", "R") == "L" and m.facing_side("S", "L") == "R"
    assert m.facing_side("L", "R") == "L" and m.facing_side(None, "R") is None
    print("test_switch_hitters_turn_around OK")


def test_traded_players_use_combined_split():
    """Real batched response: García and Ramos (traded) each have three
    'vs RHP' splits -- by team, then combined -- and so does Peralta vs
    each side. The parser must take the combined (largest) one."""
    import json
    import parsing
    fx = json.loads((Path(__file__).resolve().parent.parent / "data" /
                     "sample_estimates_nyy_vs_peralta.json").read_text())
    hitters = parsing.parse_hitters_vs_hand(fx["people"], "vr")
    assert hitters[671277]["stat"]["plateAppearances"] == 456, hitters[671277]   # García: 317 + 139
    assert hitters[671218]["stat"]["plateAppearances"] == 327, hitters[671218]   # Ramos: 236 + 91
    assert hitters[700250]["bats"] == "L" and hitters[700250]["stat"]["ops"] == ".961"
    pv = parsing.parse_pitcher_vs_hand(fx["peralta_vs"])
    assert pv["L"]["battersFaced"] == 461 and pv["R"]["battersFaced"] == 269, pv
    print("test_traded_players_use_combined_split OK: García 456 PA, Ramos 327 PA, Peralta 461/269 BF")


if __name__ == "__main__":
    test_league_baseline()
    test_judge_vs_schlittler()
    test_contact_hitter_strikes_out_less()
    test_no_data_falls_back_to_league()
    test_switch_hitters_turn_around()
    test_traded_players_use_combined_split()
    print("\nAll matchup estimate tests passed.")
