"""
Pure stat-math: turns raw counting stats (as stored in matchup_career /
matchup_season) into the rate stats the front end displays -- AVG, OBP,
SLG, OPS, and wOBA. Kept separate from api/app.py (no Flask, no database)
so it's easy to unit-test on its own, same reasoning as parsing.py.

wOBA isn't one of MLB's vsPlayer API fields, so it's derived here from the
raw counts using standard linear weights. These are fixed weights, not
the official year-specific FanGraphs constants (which aren't freely
published) -- a reasonable approximation, not an exact match to
FanGraphs' own site.

A rate stat (AVG/OBP/SLG/OPS/wOBA) is always recomputed from raw counts
here, including for a team-totals row, rather than averaging individual
batters' rates -- you can't average rates across different numbers of at
bats and get the right answer, you have to add up the raw counts first.
"""
from __future__ import annotations

import math

# The raw counting-stat columns (as named in matchup_career /
# matchup_season), in the order the front end's table displays them.
# short key -> database column name.
RAW_COUNT_FIELDS = [
    ("np", "number_of_pitches"),
    ("pa", "plate_appearances"),
    ("ab", "at_bats"),
    ("h", "hits"),
    ("d2", "doubles"),
    ("d3", "triples"),
    ("hr", "home_runs"),
    ("r", "runs"),
    ("rbi", "rbi"),
    ("bb", "base_on_balls"),
    ("so", "strike_outs"),
    ("sb", "stolen_bases"),
    ("cs", "caught_stealing"),
]

# Extra columns the rate-stat math needs but that aren't their own table
# column (hit-by-pitch, intentional walks, sac flies feed OBP/wOBA only).
_EXTRA_FIELDS = ["hit_by_pitch", "intentional_walks", "sac_flies"]


def _n(x) -> float:
    """None (MLB's API sometimes omits a field for a given pair) counts as
    zero for arithmetic. This never hides missing data from the person
    looking at the table -- the raw value shown in compute_batting_stats()
    stays None/blank -- it just keeps the math from crashing on it."""
    return x if x is not None else 0


def _fmt3(x: float) -> str:
    """.xxx formatting, matching how batting averages are conventionally
    written (no leading zero below 1.000)."""
    if not math.isfinite(x):
        return ".000"
    s = f"{x:.3f}"
    return s[1:] if 0 <= x < 1 else s


def compute_batting_stats(raw: dict) -> dict:
    """raw: a dict with matchup_career/matchup_season column names (a
    sqlite3.Row-turned-dict works directly).

    Returns a display row: the original raw counting stats (None stays
    None -- the front end shows that as a blank, not a zero) plus computed
    avg/obp/slg/ops/woba strings and a numeric ops_num for color coding.
    """
    ab = _n(raw.get("at_bats"))
    h = _n(raw.get("hits"))
    d2 = _n(raw.get("doubles"))
    d3 = _n(raw.get("triples"))
    hr = _n(raw.get("home_runs"))
    bb = _n(raw.get("base_on_balls"))
    ibb = _n(raw.get("intentional_walks"))
    hbp = _n(raw.get("hit_by_pitch"))
    sf = _n(raw.get("sac_flies"))

    singles = h - d2 - d3 - hr
    total_bases = singles + 2 * d2 + 3 * d3 + 4 * hr

    avg = h / ab if ab else 0.0
    obp_denom = ab + bb + hbp + sf
    obp = (h + bb + hbp) / obp_denom if obp_denom else 0.0
    slg = total_bases / ab if ab else 0.0
    ops = obp + slg
    woba_denom = ab + bb - ibb + sf + hbp
    woba = (
        (0.69 * (bb - ibb) + 0.72 * hbp + 0.888 * singles + 1.271 * d2 + 1.616 * d3 + 2.101 * hr)
        / woba_denom
    ) if woba_denom else 0.0

    row = {key: raw.get(col) for key, col in RAW_COUNT_FIELDS}
    row.update({
        "avg": _fmt3(avg),
        "obp": _fmt3(obp),
        "slg": _fmt3(slg),
        "ops": _fmt3(ops),
        "woba": _fmt3(woba),
        "ops_num": ops,
    })
    return row


def sum_raw_counts(rows: list[dict]) -> dict:
    """Add up every raw counting column (including the OBP/wOBA-only ones
    that aren't their own table column) across a list of raw stat dicts,
    treating a missing value as zero. Feed the result straight into
    compute_batting_stats() to get a team-totals row."""
    cols = [col for _, col in RAW_COUNT_FIELDS] + _EXTRA_FIELDS
    totals = {c: 0 for c in cols}
    for r in rows:
        for c in cols:
            totals[c] += _n(r.get(c))
    return totals
