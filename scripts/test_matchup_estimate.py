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

# 30 teams summed: PA 183,849; AB 163,329; H 39,849; BB 16,337; HBP 2,118;
# SO 40,700; HR 5,575; TB 65,409; 4,858 team-games
LEAGUE = m.league_rates([{"plateAppearances": 183849, "atBats": 163329, "hits": 39849,
                          "baseOnBalls": 16337, "hitByPitch": 2118, "strikeOuts": 40700,
                          "homeRuns": 5575, "totalBases": 65409, "gamesPlayed": 4858}])
# Ben Rice vs RHP and Freddy Peralta vs LHH (Rice bats left), 2026 regular season.
RICE_VS_RHP = {"plateAppearances": 446, "atBats": 374, "hits": 102, "baseOnBalls": 67,
               "hitByPitch": 2, "strikeOuts": 102, "homeRuns": 30, "totalBases": 216}
PERALTA_VS_LHH = {"battersFaced": 461, "atBats": 417, "hits": 106, "baseOnBalls": 39,
                  "hitByPitch": 1, "strikeOuts": 100, "homeRuns": 17, "totalBases": 183}
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
    assert (r(LEAGUE["avg"], 4), r(LEAGUE["slg"], 4)) == (0.244, 0.4005), LEAGUE
    assert r(LEAGUE["pa_per_team_game"], 2) == 37.84, LEAGUE
    print(f"test_league_baseline OK: {LEAGUE}")


def test_rice_vs_peralta_slash_line():
    """Hand-checked: AVG b=(102+.2440*910)/(374+910), p=(106+.2440*630)/(417+630),
    odds ratio -> .2565; SLG (216+.4005*320)/694 x (183+.4005*550)/967 / .4005 -> .5164."""
    est = m.estimate(m.hitting_counts(RICE_VS_RHP), m.pitching_counts(PERALTA_VS_LHH), LEAGUE)
    assert (r(est["avg"], 4), r(est["ob"], 4), r(est["slg"], 4)) == (0.2565, 0.3496, 0.5164), est
    # His real line vs RHP is .273/.383/.578; the estimate sits between that and league,
    # pulled down by Peralta being better than average vs lefties.
    print(f"test_rice_vs_peralta_slash_line OK: {est['avg']:.3f}/{est['ob']:.3f}/{est['slg']:.3f}")


def test_mix_adjustment_and_hr_tonight():
    est = m.estimate(m.hitting_counts(RICE_VS_RHP), m.pitching_counts(PERALTA_VS_LHH), LEAGUE)
    same = m.apply_mix(est, None)
    assert same == est
    up = m.apply_mix(est, {"ratio": {"xba": 1.05, "xslg": 1.10, "xwoba": 1.05}})
    assert up["avg"] > est["avg"] and up["ob"] > est["ob"] and up["hr"] > est["hr"], up
    assert abs(up["slg"] - est["slg"] * 1.10) < 1e-12, up
    # Expected PA: leadoff ~4.64, unknown spot = league average ~4.2.
    assert r(m.expected_pa(LEAGUE["pa_per_team_game"], 1), 2) == 4.64
    assert r(m.expected_pa(LEAGUE["pa_per_team_game"], None), 2) == 4.20
    # 1+ HR in 4 PA all at 6% = 1 - .94^4 = 21.9%; split half vs a 2% bullpen rate -> lower.
    assert r(m.hr_chance_tonight(0.06, 0.06, 4, 1.0), 3) == 0.219
    assert m.hr_chance_tonight(0.06, 0.02, 4, 0.5) < 0.219
    print("test_mix_adjustment_and_hr_tonight OK")


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
    assert all(abs(est[k] - LEAGUE[k]) < 1e-12 for k in m.RATES + m.AB_RATES), est
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


def test_h2h_at_bats_count_but_dont_swamp():
    """Kamil: 'don't fully ignore those ABs even if it's only 2 -- it's still data.'
    A 2-for-2 with a homer moves the projection up, by about what 2 AB are
    worth next to a full season of evidence -- not to .1000."""
    est = m.estimate(m.hitting_counts(RICE_VS_RHP), m.pitching_counts(PERALTA_VS_LHH), LEAGUE)
    h2h = m.h2h_counts({"plate_appearances": 2, "at_bats": 2, "hits": 2, "home_runs": 1,
                        "doubles": 1, "triples": 0, "total_bases": None})
    assert h2h["slg"] == 2 + 1 + 3 and h2h["ob"] == 2, h2h  # TB rebuilt from hit types: a double + a HR = 2 + 1 + 3 = 6
    up = m.apply_h2h(est, h2h)
    assert all(up[k] > est[k] for k in ("avg", "ob", "slg", "hr")), (up, est)
    # AVG: (est * 910 + 2) / 912 -- exactly what 2 at-bats are worth here.
    assert abs(up["avg"] - (est["avg"] * 910 + 2) / 912) < 1e-12
    assert up["avg"] - est["avg"] < 0.002, (up["avg"], est["avg"])
    # 0-for-2 moves it down by a similar small amount; no history -> unchanged.
    down = m.apply_h2h(est, m.h2h_counts({"plate_appearances": 2, "at_bats": 2, "hits": 0, "home_runs": 0}))
    assert down["avg"] < est["avg"] and m.apply_h2h(est, None) == est
    assert m.h2h_counts({"plate_appearances": 0, "at_bats": 0}) is None
    print(f"test_h2h_at_bats_count_but_dont_swamp OK: AVG {est['avg']:.4f} -> 2-for-2 {up['avg']:.4f}, 0-for-2 {down['avg']:.4f}")


if __name__ == "__main__":
    test_league_baseline()
    test_rice_vs_peralta_slash_line()
    test_mix_adjustment_and_hr_tonight()
    test_judge_vs_schlittler()
    test_contact_hitter_strikes_out_less()
    test_no_data_falls_back_to_league()
    test_switch_hitters_turn_around()
    test_traded_players_use_combined_split()
    test_h2h_at_bats_count_but_dont_swamp()
    print("\nAll matchup estimate tests passed.")
