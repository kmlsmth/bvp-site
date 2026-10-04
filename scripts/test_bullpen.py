"""
Unit tests for the pure stat math in bullpen.py -- no network, no database.

Run: python3 scripts/test_bullpen.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bullpen  # noqa: E402


def test_innings_pitched_display() -> None:
    assert bullpen.innings_pitched_display(18) == "6.0"
    assert bullpen.innings_pitched_display(16) == "5.1"
    assert bullpen.innings_pitched_display(17) == "5.2"
    assert bullpen.innings_pitched_display(0) == "0.0"
    assert bullpen.innings_pitched_display(None) == "0.0"
    print("test_innings_pitched_display OK")


def test_compute_pitching_rate_stats_basic() -> None:
    # 6 IP (18 outs), 2 ER, 1 BB, 7 K, 4 H -> easy numbers to hand-check:
    # ERA = 2*9/6 = 3.00, WHIP = (1+4)/6 = 0.833, K/9 = 7*9/6 = 10.5, BB/9 = 1*9/6 = 1.5
    raw = {"outs": 18, "earned_runs": 2, "base_on_balls": 1, "strike_outs": 7, "hits": 4}
    row = bullpen.compute_pitching_rate_stats(raw)
    assert row["ip_display"] == "6.0", row["ip_display"]
    assert row["era"] == "3.00", row["era"]
    assert row["whip"] == "0.83", row["whip"]
    assert row["k9"] == "10.5", row["k9"]
    assert row["bb9"] == "1.5", row["bb9"]
    print("test_compute_pitching_rate_stats_basic OK:", row["era"], row["whip"], row["k9"], row["bb9"])


def test_compute_pitching_rate_stats_zero_outs_no_crash() -> None:
    # Pulled before recording an out -- 0 IP, 3 ER. Should return zeroed
    # rate stats, not divide-by-zero.
    raw = {"outs": 0, "earned_runs": 3, "base_on_balls": 2, "strike_outs": 0, "hits": 3}
    row = bullpen.compute_pitching_rate_stats(raw)
    assert row["era"] == "0.00", row["era"]
    assert row["ip_display"] == "0.0", row["ip_display"]
    assert row["er"] == 3, "raw counts still pass through even when rate stats are zeroed"
    print("test_compute_pitching_rate_stats_zero_outs_no_crash OK")


def test_sum_pitching_counts_treats_none_as_zero() -> None:
    rows = [
        {"outs": 18, "earned_runs": 2, "pitches": 94, "hits": None},
        {"outs": 4, "earned_runs": 0, "pitches": 19, "hits": 1},
    ]
    totals = bullpen.sum_pitching_counts(rows)
    assert totals["outs"] == 22
    assert totals["pitches"] == 113
    assert totals["hits"] == 1  # None treated as 0, not skipped
    print("test_sum_pitching_counts_treats_none_as_zero OK:", totals)


def test_pitcher_recent_form_takes_last_n_by_date() -> None:
    rows = [
        {"game_date": "2026-09-01", "outs": 18, "earned_runs": 5, "base_on_balls": 0, "strike_outs": 4, "hits": 7},
        {"game_date": "2026-09-20", "outs": 18, "earned_runs": 1, "base_on_balls": 1, "strike_outs": 8, "hits": 3},
        {"game_date": "2026-09-25", "outs": 15, "earned_runs": 2, "base_on_balls": 2, "strike_outs": 6, "hits": 4},
    ]
    form = bullpen.pitcher_recent_form(rows, n=2)
    assert form["starts_counted"] == 2, "should only use the 2 most recent starts, not all 3"
    # 2026-09-20 + 2026-09-25: outs 33 (11.0 IP), ER 3 -> ERA = 3*9/11 = 2.4545
    assert form["era"] == "2.45", form["era"]
    print("test_pitcher_recent_form_takes_last_n_by_date OK:", form["starts_counted"], form["era"])


def test_fatigue_label_boundaries() -> None:
    assert bullpen.fatigue_label(80) == "Exhausted"
    assert bullpen.fatigue_label(75) == "Exhausted"
    assert bullpen.fatigue_label(74.9) == "Taxed"
    assert bullpen.fatigue_label(50) == "Taxed"
    assert bullpen.fatigue_label(25) == "Moderate"
    assert bullpen.fatigue_label(10) == "Fresh"
    print("test_fatigue_label_boundaries OK")


def test_compute_bullpen_fatigue_empty_bullpen() -> None:
    result = bullpen.compute_bullpen_fatigue([], "2026-10-03", league_avg_weekly_pitches=300)
    assert result["score"] == 0.0
    assert result["label"] == "Fresh"
    assert result["relievers_used"] == 0
    print("test_compute_bullpen_fatigue_empty_bullpen OK")


def test_compute_bullpen_fatigue_exactly_average_and_fresh() -> None:
    # One reliever, exactly league-average weekly volume, last pitched
    # 7+ days ago -> volume component = 50 (exactly average), recency
    # component = 0 (fully rested) -> score = 50*0.7 + 0*0.3 = 35.0
    rows = [{"pitcher_id": 1, "game_date": "2026-09-25", "pitches": 300}]
    result = bullpen.compute_bullpen_fatigue(rows, "2026-10-03", league_avg_weekly_pitches=300)
    assert result["score"] == 35.0, result["score"]
    assert result["label"] == "Moderate", result["label"]  # 25 <= 35 < 50
    print("test_compute_bullpen_fatigue_exactly_average_and_fresh OK:", result)


def test_compute_bullpen_fatigue_heavy_and_recent() -> None:
    # Two relievers, double the league-average combined volume (capped at
    # 100 for the volume component), both pitched today (0 days ago, fully
    # fatigued on the recency axis) -> score = 100*0.7 + 100*0.3 = 100.0
    rows = [
        {"pitcher_id": 1, "game_date": "2026-10-03", "pitches": 300},
        {"pitcher_id": 2, "game_date": "2026-10-03", "pitches": 300},
    ]
    result = bullpen.compute_bullpen_fatigue(rows, "2026-10-03", league_avg_weekly_pitches=300)
    assert result["score"] == 100.0, result["score"]
    assert result["label"] == "Exhausted"
    assert result["relievers_used"] == 2
    print("test_compute_bullpen_fatigue_heavy_and_recent OK:", result)


def test_compute_bullpen_fatigue_no_league_baseline_treated_as_average() -> None:
    rows = [{"pitcher_id": 1, "game_date": "2026-09-25", "pitches": 50}]
    result = bullpen.compute_bullpen_fatigue(rows, "2026-10-03", league_avg_weekly_pitches=0)
    # volume_component falls back to 50 (treated as average) when there's
    # no league baseline yet; recency is 0 (8 days ago, clamped to 7+)
    assert result["score"] == 35.0, result["score"]
    print("test_compute_bullpen_fatigue_no_league_baseline_treated_as_average OK")


if __name__ == "__main__":
    test_innings_pitched_display()
    test_compute_pitching_rate_stats_basic()
    test_compute_pitching_rate_stats_zero_outs_no_crash()
    test_sum_pitching_counts_treats_none_as_zero()
    test_pitcher_recent_form_takes_last_n_by_date()
    test_fatigue_label_boundaries()
    test_compute_bullpen_fatigue_empty_bullpen()
    test_compute_bullpen_fatigue_exactly_average_and_fresh()
    test_compute_bullpen_fatigue_heavy_and_recent()
    test_compute_bullpen_fatigue_no_league_baseline_treated_as_average()
    print("\nAll bullpen tests passed.")
