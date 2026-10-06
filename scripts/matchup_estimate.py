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

for the probability-type rates: hits per at-bat (AVG), times on base per
plate appearance (OBP), home runs and strikeouts per plate appearance.
Slugging (total bases per at-bat) isn't a probability -- it can exceed 1
-- so it's combined the plain multiplicative way: hitter x pitcher /
league. OPS = estimated OBP + estimated SLG.

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

RATES = ("ob", "so", "hr")          # per plate appearance (odds ratio)
AB_RATES = ("avg", "slg")           # per at-bat; avg odds ratio, slg multiplicative

# Plate appearances / at-bats (hitters) and batters faced / at-bats
# (pitchers) at which each rate stabilizes -- FanGraphs Library, from
# Russell Carleton's research.
HITTER_STABILIZE = {"ob": 460, "so": 60, "hr": 170, "avg": 910, "slg": 320}
PITCHER_STABILIZE = {"ob": 540, "so": 70, "hr": 1320, "avg": 630, "slg": 550}


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
        "ab": _i(stat.get("atBats")),
        "ob": _i(stat.get("hits")) + _i(stat.get("baseOnBalls")) + _i(stat.get("hitByPitch")),
        "so": _i(stat.get("strikeOuts")),
        "hr": _i(stat.get("homeRuns")),
        "avg": _i(stat.get("hits")),
        "slg": _i(stat.get("totalBases")),
    }


def pitching_counts(stat: dict | None) -> dict | None:
    """MLB pitching stat line -> batters faced + the same three events
    (allowed), so they're on the same per-plate-appearance footing."""
    if not stat:
        return None
    return {
        "pa": _i(stat.get("battersFaced")),
        "ab": _i(stat.get("atBats")),
        "ob": _i(stat.get("hits")) + _i(stat.get("baseOnBalls")) + _i(stat.get("hitByPitch")),
        "so": _i(stat.get("strikeOuts")),
        "hr": _i(stat.get("homeRuns")),
        "avg": _i(stat.get("hits")),
        "slg": _i(stat.get("totalBases")),
    }


def league_rates(team_stat_lines: list[dict]) -> dict:
    """Sum every team's season hitting line into league rates, plus the
    league's plate appearances per team per game (for expected PA)."""
    totals = {"pa": 0, "ab": 0, "ob": 0, "so": 0, "hr": 0, "avg": 0, "slg": 0}
    games = 0
    for line in team_stat_lines:
        c = hitting_counts(line)
        for k in totals:
            totals[k] += c[k]
        games += _i(line.get("gamesPlayed"))
    if not totals["pa"] or not totals["ab"]:
        raise ValueError("no league plate appearances to average")
    out = {r: totals[r] / totals["pa"] for r in RATES}
    out.update({r: totals[r] / totals["ab"] for r in AB_RATES})
    out["pa_per_team_game"] = totals["pa"] / games if games else 38.0
    return out


def regressed(counts: dict | None, rate: str, league_rate: float, k: int) -> float:
    events = counts[rate] if counts else 0
    denom_key = "ab" if rate in AB_RATES else "pa"
    n = counts[denom_key] if counts else 0
    return (events + league_rate * k) / (n + k)


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
    relevant handedness split (None if no data). Returns estimated rates:
    ob / so / hr per plate appearance, avg / slg per at-bat."""
    out = {}
    for r in RATES + AB_RATES:
        b = regressed(hitter, r, league[r], HITTER_STABILIZE[r])
        p = regressed(pitcher, r, league[r], PITCHER_STABILIZE[r])
        out[r] = b * p / league[r] if r == "slg" else odds_ratio(b, p, league[r])
    return out


def _odds_scale(x: float, ratio: float) -> float:
    o = x / (1 - x) * ratio
    return o / (1 + o)


def apply_mix(est: dict, mix: dict | None) -> dict:
    """Nudge an estimate by the pitch-mix ratios from pitch_mix.mix_matchup():
    AVG by the xBA ratio, OBP by the xwOBA ratio, SLG and HR by the xSLG
    ratio. No mix data -> unchanged."""
    if not mix:
        return dict(est)
    r = mix["ratio"]
    out = dict(est)
    out["avg"] = _odds_scale(est["avg"], r["xba"])
    out["ob"] = _odds_scale(est["ob"], r["xwoba"])
    out["slg"] = est["slg"] * r["xslg"]
    out["hr"] = _odds_scale(est["hr"], r["xslg"])
    return out


# Each spot lower in the batting order gets roughly 0.11 fewer plate
# appearances per game (about 18 per season per spot -- the batting-order
# chapter of "The Book"); the 5th spot gets about the league average.
PA_STEP_PER_LINEUP_SPOT = 0.11


def expected_pa(league_pa_per_team_game: float, lineup_spot: int | None) -> float:
    per_spot = league_pa_per_team_game / 9
    if not lineup_spot:
        return per_spot
    return per_spot + (5 - lineup_spot) * PA_STEP_PER_LINEUP_SPOT


def hr_chance_tonight(p_vs_pitcher: float, p_vs_rest: float, total_pa: float,
                      share_vs_pitcher: float) -> float:
    """Chance of at least one home run over his expected plate appearances:
    the share that comes against this pitcher at the matchup rate, the rest
    (bullpen) at his own rate vs league-average pitching."""
    s = max(0.0, min(1.0, share_vs_pitcher))
    n_p, n_r = total_pa * s, total_pa * (1 - s)
    return 1 - (1 - p_vs_pitcher) ** n_p * (1 - p_vs_rest) ** n_r


# --- Head-to-head at-bats ------------------------------------------------
# Hitters with only a couple of at-bats against the pitcher still get a
# projection, but those at-bats aren't thrown away: they're added on top of
# the projection as real evidence. The projection counts as the same number
# of plate appearances / at-bats used to regress the hitter's own numbers
# (HITTER_STABILIZE), so a 2-for-2 nudges it rather than swamping it.

def h2h_counts(career: dict | None) -> dict | None:
    """matchup_career or matchup_season row (this site's stored
    head-to-head totals) -> counts in the same shape as hitting_counts().
    Total bases are rebuilt from hit types when MLB left that field out (it
    sometimes does)."""
    if not career:
        return None
    pa, ab = _i(career.get("plate_appearances")), _i(career.get("at_bats"))
    if not pa and not ab:
        return None
    h, hr = _i(career.get("hits")), _i(career.get("home_runs"))
    tb = career.get("total_bases")
    if tb is None:
        tb = h + _i(career.get("doubles")) + 2 * _i(career.get("triples")) + 3 * hr
    return {
        "pa": pa or ab, "ab": ab,
        "ob": h + _i(career.get("base_on_balls")) + _i(career.get("hit_by_pitch")),
        "so": _i(career.get("strike_outs")), "hr": hr,
        "avg": h, "slg": _i(tb),
    }


def apply_h2h(est: dict, h2h: dict | None) -> dict:
    if not h2h:
        return dict(est)
    out = dict(est)
    for r in RATES + AB_RATES:
        n = h2h["ab"] if r in AB_RATES else h2h["pa"]
        if n:
            k = HITTER_STABILIZE[r]
            out[r] = (est[r] * k + h2h[r]) / (k + n)
    return out


# Older head-to-head at-bats count for less: each year back is worth 90% of
# the year after it, so a 2019 at-bat counts about half as much as one this
# season (0.9 ** 7 = 0.48). This site's own choice.
H2H_FADE_PER_YEAR = 0.9


def h2h_faded(season_rows: list[dict], current_season: int) -> dict | None:
    """matchup_season rows for one hitter vs one pitcher -> head-to-head
    counts (floats), each season weighted by H2H_FADE_PER_YEAR per year of
    age. None if there's nothing."""
    total = None
    for row in season_rows or []:
        c = h2h_counts(row)
        if not c:
            continue
        try:
            age = max(0, current_season - int(row.get("season")))
        except (TypeError, ValueError):
            age = 0
        w = H2H_FADE_PER_YEAR ** age
        total = {k: (total[k] if total else 0) + w * v for k, v in c.items()}
    return total if total and (total["pa"] or total["ab"]) else None


# --- Past seasons ("Marcel" weighting) ----------------------------------
# A hitter's 2026 split alone is often a few hundred plate appearances. The
# two seasons before it are real evidence too, just older, so they're added
# in at 5/4/3 weights -- this season counts fully, last season 80%, two
# seasons ago 60% -- the weighting Tom Tango uses in his "Marcel" projection
# system. The combined (weighted) counts are then regressed to league as
# usual, so more history means less pulling toward average.
SEASON_WEIGHTS = (1.0, 0.8, 0.6)   # this season, last season, two seasons ago


def combine_seasons(counts_by_season: list[dict | None],
                    weights: tuple | None = None) -> dict | None:
    """[this season's counts, last season's, two seasons ago] (any can be
    None) -> one weighted set of counts (floats), or None if all empty."""
    total = None
    for c, w in zip(counts_by_season, weights or SEASON_WEIGHTS):
        if not c:
            continue
        total = {k: (total[k] if total else 0) + w * v for k, v in c.items()}
    return total if total and total["pa"] else None
