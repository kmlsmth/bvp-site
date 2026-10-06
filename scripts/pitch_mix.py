"""
Pitch-mix matchup: how a hitter handles each pitch type, weighted by how
often THIS pitcher throws each one to hitters from that side. Pure
functions (no network) -- see test_pitch_mix.py, which runs on real
Baseball Savant rows.

The output is three ratios -- the hitter's expected average / slugging /
wOBA against this pitcher's mix, divided by the same stat against
everything he's actually seen this season. A ratio above 1 means this
pitcher leans on pitches the hitter handles better than his usual diet;
below 1, pitches that give him trouble. matchup_estimate.apply_mix() then
nudges the handedness-based estimate by those ratios.

Expected stats (xBA / xSLG / xwOBA, from exit velocity and launch angle)
are used rather than actual results because they settle down in fewer
plate appearances -- which matters here, since a hitter's line against
any ONE pitch type is a small sample. On top of that, each pitch type's
numbers are pulled toward the hitter's numbers on that pitch FAMILY
(fastballs / breaking / offspeed), and each family toward his overall
line, by REGRESS_PA plate appearances. That amount is this site's own
conservative choice, not a published constant: with ~60 PA on a pitch
type the hitter's own number counts for half.
"""
from __future__ import annotations

import csv
import io

REGRESS_PA = 60
RATIO_CLAMP = (0.85, 1.15)  # safety rail: the mix nudges the estimate, never dominates it
METRICS = ("xba", "xslg", "xwoba")

PITCH_NAMES = {
    "FF": "4-Seam", "SI": "Sinker", "FC": "Cutter",
    "SL": "Slider", "ST": "Sweeper", "SV": "Slurve", "CU": "Curveball",
    "KC": "Knuckle Curve", "CS": "Slow Curve",
    "CH": "Changeup", "FS": "Splitter", "FO": "Forkball", "SC": "Screwball",
    "KN": "Knuckleball", "EP": "Eephus",
}
FAMILY = {
    "FF": "fastball", "SI": "fastball", "FC": "fastball",
    "SL": "breaking", "ST": "breaking", "SV": "breaking", "CU": "breaking",
    "KC": "breaking", "CS": "breaking",
    "CH": "offspeed", "FS": "offspeed", "FO": "offspeed", "SC": "offspeed",
    "KN": "offspeed", "EP": "offspeed",
}


def _f(x):
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


def _i(x) -> int:
    try:
        return int(float(x)) if x not in (None, "") else 0
    except ValueError:
        return 0


def parse_arsenal_csv(text: str) -> dict:
    """Savant pitch-arsenal leaderboard CSV -> {player_id: {pitch_type: row}}
    where row = {pa, pitches, usage, xba, xslg, xwoba, whiff}."""
    out: dict = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        pid, pt = _i(r.get("player_id")), (r.get("pitch_type") or "").strip()
        if not pid or pt not in PITCH_NAMES:
            continue
        out.setdefault(pid, {})[pt] = {
            "pa": _i(r.get("pa")),
            "pitches": _i(r.get("pitches")),
            "usage": _f(r.get("pitch_usage")),
            "xba": _f(r.get("est_ba")),
            "xslg": _f(r.get("est_slg")),
            "xwoba": _f(r.get("est_woba")),
            "whiff": _f(r.get("whiff_percent")),
        }
    return out


def usage_by_stand(pitch_csv_text: str) -> dict:
    """Pitch-by-pitch CSV -> {"L": {type: count}, "R": {type: count}}
    (counts of real pitch types thrown to left- / right-handed hitters)."""
    counts = {"L": {}, "R": {}}
    for r in csv.DictReader(io.StringIO(pitch_csv_text.lstrip("﻿"))):
        side, pt = r.get("stand"), (r.get("pitch_type") or "").strip()
        if side in counts and pt in PITCH_NAMES:
            counts[side][pt] = counts[side].get(pt, 0) + 1
    return counts


def usage_by_date(pitch_csv_text: str) -> dict:
    """Pitch-by-pitch CSV -> {game_date: {"L": {type: count}, "R": {...}}}.
    Rows without a date go under "" (treated as old, never as recent)."""
    out: dict = {}
    for r in csv.DictReader(io.StringIO(pitch_csv_text.lstrip("\ufeff"))):
        side, pt = r.get("stand"), (r.get("pitch_type") or "").strip()
        if side in ("L", "R") and pt in PITCH_NAMES:
            day = out.setdefault((r.get("game_date") or "").strip(), {"L": {}, "R": {}})
            day[side][pt] = day[side].get(pt, 0) + 1
    return out


def total_usage(by_date: dict, before: str | None = None, dates=None) -> dict:
    """Sum usage_by_date() over game dates before `before` (all if None),
    or only over `dates` if given. -> {"L": {...}, "R": {...}}."""
    counts = {"L": {}, "R": {}}
    for d, day in by_date.items():
        if dates is not None and d not in dates:
            continue
        if dates is None and before and d and d >= before:
            continue
        for side in ("L", "R"):
            for pt, n in day[side].items():
                counts[side][pt] = counts[side].get(pt, 0) + n
    return counts


# Recent pitch mix: his last RECENT_GAMES outings (postseason included),
# blended with his season mix. The season mix counts as RECENT_PRIOR_PITCHES
# pitches -- about one start's worth to one side -- so a real change in how
# he's pitching shows up quickly, but one odd outing doesn't take over.
# Both numbers are this site's own choice.
RECENT_GAMES = 5
RECENT_PRIOR_PITCHES = 100


def recent_dates(by_date: dict, before: str | None, n: int | None = None) -> list[str]:
    days = sorted((d for d in by_date if d and (not before or d < before)), reverse=True)
    return days[:RECENT_GAMES if n is None else n]


def blended_shares(by_date: dict, side: str, before: str | None) -> dict:
    """His mix to hitters from `side`: recent outings blended with the
    season (see RECENT_PRIOR_PITCHES). -> {type: share}."""
    season = shares(total_usage(by_date, before)[side])
    recent_counts = total_usage(by_date, dates=set(recent_dates(by_date, before)))[side]
    n = sum(recent_counts.values())
    if not n or not season:
        return season or shares(recent_counts)
    recent = shares(recent_counts)
    k = RECENT_PRIOR_PITCHES
    return {pt: (n * recent.get(pt, 0) + k * season.get(pt, 0)) / (n + k)
            for pt in set(season) | set(recent)}


def combine_hitter_rows(rows_by_season: list[tuple[dict, float]]) -> dict:
    """One hitter's pitch-type rows from several seasons, [(rows, weight)],
    -> one set of rows: plate appearances weighted, expected stats averaged
    by weighted plate appearances (see matchup_estimate.SEASON_WEIGHTS)."""
    out: dict = {}
    for rows, w in rows_by_season:
        for pt, r in (rows or {}).items():
            acc = out.setdefault(pt, {"pa": 0.0, "pitches": 0.0, "_w": {m: 0.0 for m in METRICS},
                                      "_s": {m: 0.0 for m in METRICS}})
            acc["pa"] += w * r["pa"]
            acc["pitches"] += w * (r.get("pitches") or 0)
            for m in METRICS:
                if r.get(m) is not None:
                    acc["_w"][m] += w * r["pa"]
                    acc["_s"][m] += w * r["pa"] * r[m]
    for pt, acc in out.items():
        for m in METRICS:
            acc[m] = acc["_s"][m] / acc["_w"][m] if acc["_w"][m] else None
        del acc["_w"], acc["_s"]
    return out


def shares(counts: dict) -> dict:
    total = sum(counts.values())
    return {pt: n / total for pt, n in counts.items()} if total else {}


def _weighted(rows, metric):
    pa = sum(r["pa"] for r in rows if r.get(metric) is not None)
    if not pa:
        return None, 0
    return sum(r[metric] * r["pa"] for r in rows if r.get(metric) is not None) / pa, pa


def hitter_by_pitch(hitter_rows: dict) -> dict | None:
    """Regressed per-pitch-type expected stats for one hitter.
    Returns {"baseline": {metric: v}, "type": {pt: {metric: v}},
    "family": {fam: {metric: v}}} or None if he has no rows at all."""
    if not hitter_rows:
        return None
    baseline = {}
    for m in METRICS:
        v, _ = _weighted(hitter_rows.values(), m)
        if v is None:
            return None
        baseline[m] = v

    fam_vals = {}
    for fam in ("fastball", "breaking", "offspeed"):
        rows = [r for pt, r in hitter_rows.items() if FAMILY[pt] == fam]
        fam_vals[fam] = {}
        for m in METRICS:
            v, pa = _weighted(rows, m)
            fam_vals[fam][m] = baseline[m] if v is None else \
                (v * pa + baseline[m] * REGRESS_PA) / (pa + REGRESS_PA)

    type_vals = {}
    for pt, r in hitter_rows.items():
        type_vals[pt] = {}
        for m in METRICS:
            prior = fam_vals[FAMILY[pt]][m]
            v, pa = r.get(m), r["pa"]
            type_vals[pt][m] = prior if v is None else (v * pa + prior * REGRESS_PA) / (pa + REGRESS_PA)
    return {"baseline": baseline, "type": type_vals, "family": fam_vals}


def mix_matchup(hitter_rows: dict, usage: dict) -> dict | None:
    """hitter_rows: parse_arsenal_csv()[hitter_id]; usage: shares() of the
    pitcher's mix to this hitter's side. Returns {"ratio": {metric: r},
    "mix": {metric: v}, "baseline": {metric: v}} or None (no data)."""
    prof = hitter_by_pitch(hitter_rows)
    if prof is None or not usage:
        return None
    mix = {}
    for m in METRICS:
        mix[m] = sum(
            share * (prof["type"][pt][m] if pt in prof["type"] else prof["family"][FAMILY[pt]][m])
            for pt, share in usage.items()
        )
    lo, hi = RATIO_CLAMP
    ratio = {m: min(hi, max(lo, mix[m] / prof["baseline"][m])) if prof["baseline"][m] else 1.0
             for m in METRICS}
    return {"ratio": ratio, "mix": mix, "baseline": prof["baseline"]}
