"""
Pure stat math for pitcher_appearances rows: pitching rate stats (for
starter recent-form) and the bullpen fatigue score. No network, no
database -- same reasoning as stats.py, kept separate so it's easy to
unit-test on its own (see test_bullpen.py).

The fatigue formula's weights (pitch volume vs. league average weighted
70%, days since each reliever's last appearance weighted 30%, flagged
"Exhausted" above 75) are borrowed from an existing open-source bullpen
tracker (github.com/mikeharman89/bullpen-report) rather than invented --
see the roadmap doc for that research. The volume-scoring curve and the
Fresh/Moderate/Taxed/Exhausted label boundaries below are this site's own
choices layered on top of that borrowed weighting, not published anywhere,
and worth revisiting once real data suggests better cutoffs.
"""
from __future__ import annotations

import math
from datetime import date

OUTS_PER_INNING = 3


def innings_pitched_display(outs: int | None) -> str:
    """Outs recorded -> MLB's conventional "6.0" / "5.1" / "5.2" display
    string (the inverse of parsing._innings_pitched_to_outs)."""
    if outs is None:
        return "0.0"
    whole, frac = divmod(outs, OUTS_PER_INNING)
    return f"{whole}.{frac}"


def _fmt2(x: float) -> str:
    return f"{x:.2f}" if math.isfinite(x) else "0.00"


def _fmt1(x: float) -> str:
    return f"{x:.1f}" if math.isfinite(x) else "0.0"


# raw pitcher_appearances column -> short display key, used by both the
# per-appearance rows and the summed totals this module works with.
RAW_COUNT_FIELDS = [
    ("outs", "outs"),
    ("pitches", "pitches"),
    ("bf", "batters_faced"),
    ("er", "earned_runs"),
    ("bb", "base_on_balls"),
    ("k", "strike_outs"),
    ("h", "hits"),
]


def _n(x) -> float:
    return x if x is not None else 0


def sum_pitching_counts(rows: list[dict]) -> dict:
    """Add up every raw counting column across a list of pitcher_appearances
    rows (dicts with those column names), treating a missing value as
    zero. Feed the result into compute_pitching_rate_stats()."""
    cols = [col for _, col in RAW_COUNT_FIELDS]
    totals = {c: 0 for c in cols}
    for r in rows:
        for c in cols:
            totals[c] += _n(r.get(c))
    return totals


def compute_pitching_rate_stats(raw: dict) -> dict:
    """raw: a dict with pitcher_appearances column names (a single
    appearance, or summed totals from sum_pitching_counts()).

    Returns the raw counts passthrough plus ERA/WHIP/K9/BB9 as display
    strings, an innings-pitched display string, and era_num/whip_num for
    sorting or color coding. Innings pitched of 0 returns zeroed rate
    stats rather than crashing on a divide-by-zero (a pitcher who was
    pulled before recording an out)."""
    outs = _n(raw.get("outs"))
    er = _n(raw.get("earned_runs"))
    bb = _n(raw.get("base_on_balls"))
    k = _n(raw.get("strike_outs"))
    h = _n(raw.get("hits"))
    innings = outs / OUTS_PER_INNING

    era = (er * 9 / innings) if innings else 0.0
    whip = ((bb + h) / innings) if innings else 0.0
    k9 = (k * 9 / innings) if innings else 0.0
    bb9 = (bb * 9 / innings) if innings else 0.0

    row = {key: raw.get(col) for key, col in RAW_COUNT_FIELDS}
    row.update({
        "ip_display": innings_pitched_display(int(outs)),
        "era": _fmt2(era),
        "whip": _fmt2(whip),
        "k9": _fmt1(k9),
        "bb9": _fmt1(bb9),
        "era_num": era,
        "whip_num": whip,
    })
    return row


def pitcher_recent_form(starter_rows: list[dict], n: int = 5) -> dict:
    """A starter's rolling recent-form line: the last n starts (by
    game_date, most recent first), summed and run through the same rate
    math as a season total. `starter_rows` should already be filtered to
    one pitcher's role='starter' appearances; any order in, sorted here.

    Returns compute_pitching_rate_stats()'s dict plus "starts_counted" (how
    many starts actually went into it -- 0 to n, since a pitcher new to
    the league or just activated may not have n yet)."""
    recent = sorted(starter_rows, key=lambda r: r["game_date"], reverse=True)[:n]
    totals = sum_pitching_counts(recent)
    result = compute_pitching_rate_stats(totals)
    result["starts_counted"] = len(recent)
    return result


# --- Bullpen fatigue -------------------------------------------------

_LABEL_THRESHOLDS = [
    (75, "Exhausted"),
    (50, "Taxed"),
    (25, "Moderate"),
]


def fatigue_label(score: float) -> str:
    for threshold, label in _LABEL_THRESHOLDS:
        if score >= threshold:
            return label
    return "Fresh"


def compute_bullpen_fatigue(reliever_rows: list[dict], as_of_date: str,
                             league_avg_weekly_pitches: float) -> dict:
    """reliever_rows: one team's role='reliever' pitcher_appearances rows
    from the trailing 7 days (the caller does that date filtering -- this
    function just scores whatever rows it's handed). as_of_date: the date
    to measure "days since last appearance" from (YYYY-MM-DD), normally
    today. league_avg_weekly_pitches: the 30-team average weekly bullpen
    pitch count, so one team's volume is judged against its peers rather
    than an arbitrary fixed number.

    Returns {"score": 0-100, "label": ..., "weekly_pitches": ...,
    "relievers_used": ...} -- never raises on an empty bullpen (an
    unusual data gap, not a crash).
    """
    if not reliever_rows:
        return {"score": 0.0, "label": fatigue_label(0.0),
                "weekly_pitches": 0, "relievers_used": 0}

    weekly_pitches = sum(_n(r.get("pitches")) for r in reliever_rows)

    # Volume component: 50 points at exactly league-average volume, scaling
    # linearly, capped at 100 for a bullpen that's thrown double the
    # league average or more.
    if league_avg_weekly_pitches:
        volume_component = min(100.0, (weekly_pitches / league_avg_weekly_pitches) * 50.0)
    else:
        volume_component = 50.0  # no league baseline yet (early season) -- treat as average

    # Recency component: for each reliever who appeared, how many of the
    # last 7 days ago they last pitched (0 days ago = fully fatigued on
    # this axis, 7+ days ago = fully fresh), averaged across the bullpen.
    as_of = date.fromisoformat(as_of_date)
    last_appearance: dict[int, str] = {}
    for r in reliever_rows:
        pid = r.get("pitcher_id")
        gd = r.get("game_date")
        if pid is None or gd is None:
            continue
        if pid not in last_appearance or gd > last_appearance[pid]:
            last_appearance[pid] = gd

    if last_appearance:
        recency_scores = []
        for gd in last_appearance.values():
            days_ago = (as_of - date.fromisoformat(gd)).days
            days_ago = max(0, min(7, days_ago))
            recency_scores.append((7 - days_ago) / 7 * 100.0)
        recency_component = sum(recency_scores) / len(recency_scores)
    else:
        recency_component = 0.0

    score = volume_component * 0.7 + recency_component * 0.3
    score = max(0.0, min(100.0, score))

    return {
        "score": round(score, 1),
        "label": fatigue_label(score),
        "weekly_pitches": int(weekly_pitches),
        "relievers_used": len(last_appearance),
    }


# --- Starter overview card ---------------------------------------------

POSTSEASON_GAME_TYPES = {"F", "D", "L", "W"}


def _rate_line(raw: dict | None) -> dict | None:
    """Rate stats for one totals row, or None if there's nothing to show
    (no row at all, or zero outs recorded -- a 0.00 ERA over 0 innings
    would read as a real, great number when it's really "no data")."""
    if not raw or not _n(raw.get("outs")):
        return None
    return compute_pitching_rate_stats(raw)


def pitcher_overview(totals: dict, gamelog: list[dict], n: int = 5,
                     before_date: str | None = None) -> dict:
    """The starter overview card's numbers.

    totals: parsing.parse_pitching_totals() output (regular-season
      "season" line and "career" line).
    gamelog: parsing.parse_pitching_gamelog() rows, regular season AND
      postseason -- so "last 5 starts" means his actual last 5 times on
      the mound to start a game, playoffs included.

    Last-n-starts is summed from raw counts and run through the same rate
    math as everything else (never an average of per-game ERAs), plus an
    innings-per-start figure: total outs / starts, rounded to the nearest
    out and shown in baseball notation ("5.1" = 5 1/3 innings)."""
    season = _rate_line(totals.get("season"))
    if season is not None:
        season["games_started"] = (totals.get("season") or {}).get("games_started")

    career = _rate_line(totals.get("career"))

    # before_date: the game being previewed. Its own start shows up in
    # MLB's game log as soon as it begins (even mid-game), and the card is
    # about how he's pitched coming INTO this game -- so leave it out.
    starts = sorted(
        (g for g in gamelog if g.get("started") and g.get("game_date")
         and (before_date is None or g["game_date"] < before_date)),
        key=lambda g: g["game_date"], reverse=True,
    )[:n]
    last = None
    if starts:
        summed = sum_pitching_counts(starts)
        last = compute_pitching_rate_stats(summed)
        last["starts_counted"] = len(starts)
        last["postseason_starts"] = sum(
            1 for g in starts if g.get("game_type") in POSTSEASON_GAME_TYPES)
        last["ip_per_start"] = innings_pitched_display(
            int(round(_n(summed.get("outs")) / len(starts))))
        last["from_date"] = starts[-1]["game_date"]
        last["to_date"] = starts[0]["game_date"]

    return {"season": season, "last_starts": last, "career": career}
