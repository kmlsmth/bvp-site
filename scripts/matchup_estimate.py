"""
Matchup estimate for one hitter vs. one pitcher: pure math, no network
(see test_matchup_estimate.py).

Why this exists: most hitters have little or no head-to-head history with
a given pitcher, and even where they do it's usually a handful of plate
appearances. So instead of the head-to-head line, this blends two much
bigger samples:
  - the hitter's season against pitchers of THIS pitcher's throwing hand
    (e.g. how he's hit right-handers this year), and
  - the pitcher's season against hitters of THIS hitter's side
    (e.g. how he's handled left-handed hitters this year),
relative to league average, using the odds-ratio method (Tom Tango's
generalisation of Bill James's log5, as used in "The Book"):

    odds(est) = odds(hitter) * odds(pitcher) / odds(league)

for three per-plate-appearance rates: getting on base (H+BB+HBP), striking
out, and homering.

Small samples are pulled toward league average first, by adding the
"stabilization" number of league-average plate appearances to each side.
Those numbers come from Russell Carleton's research, as published in the
FanGraphs Library (library.fangraphs.com/principles/sample-size): e.g. a
hitter's K% is meaningful after ~60 PA, his OBP only after ~460; a
pitcher's HR rate takes ~1320 batters faced, so it's pulled in hard.

It's a rough per-plate-appearance guide, not a prediction of tonight's
box score -- the front end labels it as an estimate.
"""
from __future__ import annotations

RATES = ("ob", "so", "hr")

# Plate appearances (hitters) / batters faced (pitchers) at which each rate
# stabilizes -- FanGraphs Library, from Russell Carleton's research.
HITTER_STABILIZE = {"ob": 460, "so": 60, "hr": 170}
PITCHER_STABILIZE = {"ob": 540, "so": 70, "hr": 1320}


def _i(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def hitting_counts(stat: dict | None) -> dict | None:
    """MLB hitting stat line -> plate appearances + the three event counts."""
    if not stat:
        return None
    return {
        "pa": _i(stat.get("plateAppearances")),
        "ob": _i(stat.get("hits")) + _i(stat.get("baseOnBalls")) + _i(stat.get("hitByPitch")),
        "so": _i(stat.get("strikeOuts")),
        "hr": _i(stat.get("homeRuns")),
    }


def pitching_counts(stat: dict | None) -> dict | None:
    """MLB pitching stat line -> batters faced + the same three events
    (allowed), so they're on the same per-plate-appearance footing."""
    if not stat:
        return None
    return {
        "pa": _i(stat.get("battersFaced")),
        "ob": _i(stat.get("hits")) + _i(stat.get("baseOnBalls")) + _i(stat.get("hitByPitch")),
        "so": _i(stat.get("strikeOuts")),
        "hr": _i(stat.get("homeRuns")),
    }


def league_rates(team_stat_lines: list[dict]) -> dict:
    """Sum every team's season hitting line into league per-PA rates."""
    totals = {"pa": 0, "ob": 0, "so": 0, "hr": 0}
    for line in team_stat_lines:
        c = hitting_counts(line)
        for k in totals:
            totals[k] += c[k]
    if not totals["pa"]:
        raise ValueError("no league plate appearances to average")
    return {r: totals[r] / totals["pa"] for r in RATES}


def regressed(counts: dict | None, rate: str, league_rate: float, k: int) -> float:
    events = counts[rate] if counts else 0
    pa = counts["pa"] if counts else 0
    return (events + league_rate * k) / (pa + k)


def odds_ratio(b: float, p: float, lg: float) -> float:
    def odds(x):
        return x / (1 - x)
    o = odds(b) * odds(p) / odds(lg)
    return o / (1 + o)


def facing_side(bats: str | None, throws: str) -> str | None:
    """Which side the hitter actually bats from against this pitcher:
    switch hitters ("S") turn around to the opposite side."""
    if bats == "S":
        return "L" if throws == "R" else "R"
    return bats if bats in ("L", "R") else None


def estimate(hitter: dict | None, pitcher: dict | None, league: dict) -> dict:
    """hitter / pitcher: hitting_counts() / pitching_counts() for the
    relevant handedness split (None if no data). Returns estimated per-PA
    rates: on-base, strikeout, home run."""
    out = {}
    for r in RATES:
        b = regressed(hitter, r, league[r], HITTER_STABILIZE[r])
        p = regressed(pitcher, r, league[r], PITCHER_STABILIZE[r])
        out[r] = odds_ratio(b, p, league[r])
    return out
