"""
Pitch-mix matchup, on REAL Baseball Savant data captured 2026-10-06:
Aaron Judge's and Ben Rice's 2026 rows by pitch type (batter arsenal
leaderboard), Freddy Peralta's and Cam Schlittler's rows (pitcher
leaderboard), Peralta's real pitch counts vs lefties / righties (counted
from his full 2,960-pitch regular-season log), and two raw rows of that
log (which contain commas inside quoted play descriptions).

Run: python3 scripts/test_pitch_mix.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pitch_mix as pm

DATA = Path(__file__).resolve().parent.parent / "data"
BATTERS = pm.parse_arsenal_csv((DATA / "sample_savant_batters.csv").read_text())
PITCHERS = pm.parse_arsenal_csv((DATA / "sample_savant_pitchers.csv").read_text())
# Counted from Peralta's real 2026 pitch log (regular season).
PERALTA_VS_L = {"FF": 964, "CH": 500, "CU": 313, "ST": 33, "CS": 19, "SL": 46}
PERALTA_VS_R = {"FF": 604, "CH": 193, "ST": 136, "CU": 70, "SL": 72, "CS": 9}


def test_parse_leaderboards():
    rice = BATTERS[700250]
    assert set(rice) == {"FF", "SI", "SL", "CH", "FC", "CU", "ST", "FS", "SV"}, rice
    assert rice["FF"] == {"pa": 143, "pitches": 571, "usage": 21.4, "xba": 0.304,
                          "xslg": 0.76, "xwoba": 0.473, "whiff": 20.9}, rice["FF"]
    assert BATTERS[592450]["SV"]["xba"] is None  # blank expected stats in the real CSV
    assert PITCHERS[642547]["FF"]["usage"] == 53.0 and PITCHERS[693645]["FC"]["xwoba"] == 0.289
    print("test_parse_leaderboards OK")


def test_usage_by_stand_parses_real_pitch_log():
    counts = pm.usage_by_stand((DATA / "sample_savant_pitches_peralta.csv").read_text())
    assert counts == {"L": {}, "R": {"FF": 2}}, counts
    by_date = pm.usage_by_date((DATA / "sample_savant_pitches_peralta.csv").read_text())
    assert by_date == {"2026-09-25": {"L": {}, "R": {"FF": 2}}}, by_date
    assert pm.total_usage(by_date) == counts
    print("test_usage_by_stand_parses_real_pitch_log OK")


def test_handedness_mix_really_differs():
    l, r = pm.shares(PERALTA_VS_L), pm.shares(PERALTA_VS_R)
    assert round(l["ST"], 3) == 0.018 and round(r["ST"], 3) == 0.125, (l["ST"], r["ST"])
    assert round(l["CU"], 3) == 0.167 and round(r["CU"], 3) == 0.065
    print(f"test_handedness_mix_really_differs OK: sweeper {l['ST']:.1%} vs LHH, {r['ST']:.1%} vs RHH")


def test_rice_vs_peralta_mix():
    """Rice (bats L) crushes fastballs (.473 xwOBA on 143 PA) and Peralta
    throws lefties 51% fastballs -- so the mix should grade ABOVE Rice's
    own baseline, but only modestly after regression."""
    res = pm.mix_matchup(BATTERS[700250], pm.shares(PERALTA_VS_L))
    assert res["ratio"]["xwoba"] > 1.0 and res["ratio"]["xslg"] > 1.0, res
    assert res["ratio"]["xwoba"] < 1.10, res  # regression keeps it a nudge
    print(f"test_rice_vs_peralta_mix OK: xwOBA mix {res['mix']['xwoba']:.3f} vs "
          f"baseline {res['baseline']['xwoba']:.3f} (ratio {res['ratio']['xwoba']:.3f})")


def test_small_samples_barely_move_it():
    """Judge's 1-PA slurve with a 0 xwOBA must not drag anything around."""
    only_slurves = pm.mix_matchup(BATTERS[592450], {"SV": 1.0})
    big = pm.mix_matchup(BATTERS[592450], {"SL": 1.0})  # 44 PA, .248 xwOBA -- a real weakness
    assert only_slurves["ratio"]["xwoba"] > big["ratio"]["xwoba"], (only_slurves, big)
    print(f"test_small_samples_barely_move_it OK: all-slurve ratio {only_slurves['ratio']['xwoba']:.3f}, "
          f"all-slider {big['ratio']['xwoba']:.3f}")


def test_no_data():
    assert pm.mix_matchup({}, pm.shares(PERALTA_VS_L)) is None
    assert pm.mix_matchup(BATTERS[700250], {}) is None
    # Pitch type the hitter has never seen falls back to his family number.
    res = pm.mix_matchup(BATTERS[700250], {"KC": 1.0})
    assert res is not None and 0.85 <= res["ratio"]["xwoba"] <= 1.15
    print("test_no_data OK")


def test_recent_mix_includes_postseason_and_blends():
    """Real data: Chris Sale's 2026 pitch counts by game date. Before his
    10/6 start, his last 5 outings include two Wild Card games."""
    import json
    fx = json.loads((DATA / "sample_usage_by_date_sale.json").read_text())
    by = {d: day for d, day in fx["usage_by_date"].items()}
    recent = pm.recent_dates(by, "2026-10-06")
    assert recent == ["2026-10-01", "2026-09-29", "2026-09-23", "2026-09-11", "2026-09-04"], recent
    assert [fx["game_types"][d] for d in recent[:2]] == ["F", "F"]
    # Nothing on or after the game date counts (no peeking at tonight).
    assert pm.recent_dates(by, "2026-09-29")[0] == "2026-09-23"
    # Sliders to lefties: 267/677 = 39.4% on the season, 59/134 = 44.0% in
    # the last 5; blended (59 + 100 x .394) / (134 + 100) = 42.1%.
    season = pm.shares(pm.total_usage(by, "2026-10-06")["L"])
    blend = pm.blended_shares(by, "L", "2026-10-06")
    assert round(season["SL"], 3) == 0.394 and round(blend["SL"], 3) == 0.421, (season, blend)
    assert abs(sum(blend.values()) - 1) < 1e-9
    # No dates at all (old cache / missing column) -> plain season mix.
    undated = {"": by["2026-09-23"]}
    assert pm.blended_shares(undated, "R", "2026-10-06") == pm.shares(by["2026-09-23"]["R"])
    print(f"test_recent_mix_includes_postseason_and_blends OK: Sale SL to LHH {season['SL']:.1%} season -> {blend['SL']:.1%}")


def test_hitter_rows_combine_across_seasons():
    a = {"FF": {"pa": 100, "pitches": 400, "xba": 0.300, "xslg": 0.500, "xwoba": 0.400}}
    b = {"FF": {"pa": 100, "pitches": 400, "xba": 0.200, "xslg": 0.300, "xwoba": 0.300},
         "SL": {"pa": 10, "pitches": 40, "xba": None, "xslg": None, "xwoba": 0.250}}
    c = pm.combine_hitter_rows([(a, 1.0), (b, 0.6)])
    assert c["FF"]["pa"] == 160 and abs(c["FF"]["xba"] - (30 + 12) / 160) < 1e-12, c
    assert c["SL"]["pa"] == 6 and c["SL"]["xba"] is None and c["SL"]["xwoba"] == 0.25, c
    assert pm.combine_hitter_rows([({}, 1.0)]) == {}
    print("test_hitter_rows_combine_across_seasons OK")


if __name__ == "__main__":
    test_parse_leaderboards()
    test_usage_by_stand_parses_real_pitch_log()
    test_handedness_mix_really_differs()
    test_rice_vs_peralta_mix()
    test_small_samples_barely_move_it()
    test_no_data()
    test_recent_mix_includes_postseason_and_blends()
    test_hitter_rows_combine_across_seasons()
    print("\nAll pitch-mix tests passed.")
