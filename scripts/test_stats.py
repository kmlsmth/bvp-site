"""
Unit tests for the pure stat math in stats.py -- no network, no database.

Run: python3 scripts/test_stats.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stats  # noqa: E402


def test_basic_line():
    # 4 AB, 2 H (1 double, 1 single), 1 BB, 0 HBP/SF/IBB -> easy numbers to
    # hand-check: AVG .500, OBP (2+1)/5=.600, SLG (1+2)/4=.750, OPS 1.350
    raw = {
        "at_bats": 4, "hits": 2, "doubles": 1, "triples": 0, "home_runs": 0,
        "base_on_balls": 1, "intentional_walks": 0, "hit_by_pitch": 0, "sac_flies": 0,
        "plate_appearances": 5, "runs": 1, "rbi": 1, "strike_outs": 1,
        "stolen_bases": 0, "caught_stealing": 0, "number_of_pitches": 20,
    }
    row = stats.compute_batting_stats(raw)
    assert row["avg"] == ".500", row["avg"]
    assert row["obp"] == ".600", row["obp"]
    assert row["slg"] == ".750", row["slg"]
    assert row["ops"] == "1.350", row["ops"]
    # raw counting stats pass through unchanged
    assert row["ab"] == 4 and row["h"] == 2 and row["pa"] == 5
    print("test_basic_line OK:", row["avg"], row["obp"], row["slg"], row["ops"])


def test_zero_at_bats_no_crash():
    # A batter who's only ever walked against this pitcher: 0 AB, 1 BB.
    raw = {"at_bats": 0, "hits": 0, "doubles": 0, "triples": 0, "home_runs": 0,
           "base_on_balls": 1, "intentional_walks": 0, "hit_by_pitch": 0, "sac_flies": 0}
    row = stats.compute_batting_stats(raw)
    assert row["avg"] == ".000", row["avg"]  # AB=0 -> AVG defined as .000, not a crash
    assert row["obp"] == "1.000", row["obp"]  # 1 BB / 1 PA-ish denom = 1.000
    print("test_zero_at_bats_no_crash OK")


def test_missing_fields_become_blank_not_zero():
    # Real API responses sometimes omit runs/stolen_bases/caught_stealing
    # for a given pair -- those should stay None (front end shows blank),
    # not silently become 0, even though the rate-stat math treats a
    # missing value as 0 internally so it doesn't crash.
    raw = {"at_bats": 3, "hits": 1, "doubles": 0, "triples": 0, "home_runs": 0,
           "base_on_balls": 0, "runs": None, "stolen_bases": None, "caught_stealing": None}
    row = stats.compute_batting_stats(raw)
    assert row["r"] is None and row["sb"] is None and row["cs"] is None
    assert row["avg"] == ".333", row["avg"]
    print("test_missing_fields_become_blank_not_zero OK")


def test_sum_raw_counts_treats_none_as_zero():
    rows = [
        {"at_bats": 4, "hits": 2, "base_on_balls": 1, "runs": None},
        {"at_bats": 3, "hits": 0, "base_on_balls": 0, "runs": 2},
    ]
    totals = stats.sum_raw_counts(rows)
    assert totals["at_bats"] == 7
    assert totals["hits"] == 2
    assert totals["runs"] == 2  # None treated as 0, not skipped/crashed
    print("test_sum_raw_counts_treats_none_as_zero OK:", totals)


def test_team_totals_recomputed_not_averaged():
    # Two batters with very different AB counts -- the team AVG must come
    # from combined H/AB, not from averaging their individual .500 and
    # .250 averages (which would wrongly give .375).
    rows = [
        {"at_bats": 4, "hits": 2, "doubles": 0, "triples": 0, "home_runs": 0,
         "base_on_balls": 0, "intentional_walks": 0, "hit_by_pitch": 0, "sac_flies": 0},
        {"at_bats": 8, "hits": 2, "doubles": 0, "triples": 0, "home_runs": 0,
         "base_on_balls": 0, "intentional_walks": 0, "hit_by_pitch": 0, "sac_flies": 0},
    ]
    totals = stats.sum_raw_counts(rows)
    team = stats.compute_batting_stats(totals)
    assert team["avg"] == ".333", team["avg"]  # 4 H / 12 AB, not (.500+.250)/2
    print("test_team_totals_recomputed_not_averaged OK:", team["avg"])


if __name__ == "__main__":
    test_basic_line()
    test_zero_at_bats_no_crash()
    test_missing_fields_become_blank_not_zero()
    test_sum_raw_counts_treats_none_as_zero()
    test_team_totals_recomputed_not_averaged()
    print("\nAll stats tests passed.")
