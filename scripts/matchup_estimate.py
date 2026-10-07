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
    out["hits_per_pa"] = totals["avg"] / totals["pa"]
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


def _hits_per_pa_raw(avg: float, ob: float) -> float:
    """Hits per plate appearance from AVG (hits per at-bat) and OBP: the
    non-at-bat share of PAs (walks + HBP) is (OBP - AVG) / (1 - AVG)."""
    walks = (ob - avg) / (1 - avg)
    return avg * (1 - walks)


def hits_per_pa(est: dict, league: dict) -> float:
    """Chance of a hit in one plate appearance, from an estimate's AVG and
    OBP. Sac flies/bunts aren't in that formula, so it's scaled so a
    league-average line gives exactly the league's real hits per PA."""
    lg = _hits_per_pa_raw(league["avg"], league["ob"])
    scale = league.get("hits_per_pa", lg) / lg
    return _hits_per_pa_raw(est["avg"], est["ob"]) * scale


def hit_chance_tonight(p_vs_pitcher: float, p_vs_rest: float, total_pa: float,
                       share_vs_pitcher: float) -> float:
    """Chance of at least one hit: same split as hr_chance_tonight() --
    this pitcher for his usual share of the game, the bullpen for the rest."""
    return hr_chance_tonight(p_vs_pitcher, p_vs_rest, total_pa, share_vs_pitcher)


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


# --- v3: all at-bats + real plate-appearance counts (backtested) ---------
# Backtest on every 2026 game, June-September (27,756 starting-hitter
# games; see scripts/backtest.py): the v2 approach -- each hitter judged
# only on his at-bats vs THIS pitcher's hand, and "team PAs / 9" plate
# appearances by lineup spot -- said 64.4% "1+ hit" on average; 61.0%
# happened. These two changes bring it to 62.4% with better accuracy:
#   1. judge a player on ALL his plate appearances (both hands, 5/4/3
#      seasons), then adjust for this matchup with the league's platoon
#      split, letting his own split count PLATOON_REGRESS_PA deep;
#   2. use the real spread of plate appearances starters get by lineup spot
#      (model_constants.STARTER_PA_GAMES) instead of one average.
PLATOON_REGRESS_PA = 1000   # tested 400 / 1000 / 3000: 1000 best (barely)


def talent_rates(all_by_season: list[dict | None], hand_by_season: list[dict | None],
                 league: dict, platoon: dict, stabilize: dict,
                 k_platoon: float = PLATOON_REGRESS_PA) -> dict:
    """Rates for a player vs one hand / side.
    all_by_season: his counts vs BOTH hands, [this season, last, two ago];
    hand_by_season: the same seasons vs this hand only;
    platoon: league rate for this matchup type / overall league rate
    (model_constants.PLATOON_FACTOR["same" or "opp"], or 1s for a switch
    hitter). Overall talent is regressed toward league (stabilize), scaled
    by the platoon factor, and his own counts vs this hand are regressed
    toward that, k_platoon PA (or AB) deep."""
    o = combine_seasons(all_by_season)
    hnd = combine_seasons(hand_by_season)
    out = {}
    for r in RATES + AB_RATES:
        den = "ab" if r in AB_RATES else "pa"
        k = stabilize[r]
        overall = ((o[r] if o else 0) + league[r] * k) / ((o[den] if o else 0) + k)
        target = overall * platoon[r]
        out[r] = ((hnd[r] if hnd else 0) + target * k_platoon) / ((hnd[den] if hnd else 0) + k_platoon)
    return out


def combine_rates(hitter: dict, pitcher: dict, league: dict) -> dict:
    """Odds-ratio combination of already-regressed hitter and pitcher rates
    (SLG multiplicative), same as estimate() but without re-regressing."""
    return {r: (hitter[r] * pitcher[r] / league[r] if r == "slg" else odds_ratio(hitter[r], pitcher[r], league[r]))
            for r in RATES + AB_RATES}


def add_counts(*counts: dict | None) -> dict | None:
    cs = [c for c in counts if c]
    if not cs:
        return None
    return {k: sum(c[k] for c in cs) for k in cs[0]}


def pa_distribution(spot: int | None, home: bool | None) -> list[tuple[int, float]]:
    """[(plate appearances, probability)] for a starter in this lineup spot,
    home or away (None = unknown -> pooled)."""
    import model_constants as mc
    sides = ["home"] if home is True else ["away"] if home is False else ["home", "away"]
    spots = [spot] if spot in range(1, 10) else list(range(1, 10))
    counts: dict = {}
    for sd in sides:
        for sp in spots:
            for n, c in mc.STARTER_PA_GAMES[sd][sp].items():
                counts[n] = counts.get(n, 0) + c
    tot = sum(counts.values())
    return [(n, c / tot) for n, c in sorted(counts.items())]


def chance_over_pa_distribution(p_vs_pitcher: float, p_vs_rest: float,
                                 dist: list[tuple[int, float]], share_vs_pitcher: float) -> float:
    """Chance of at least one event, averaged over how many PAs he gets."""
    return sum(w * hr_chance_tonight(p_vs_pitcher, p_vs_rest, n, share_vs_pitcher) for n, w in dist)
