"""
Backtest: replay past games with the site's projection, using only data
that existed before each game, and compare to what actually happened.

Inputs (not part of the deployed site -- built offline):
  - every 2026 regular-season plate appearance from Baseball Savant
    (compact CSV: game_date, game_pk, at_bat_number, inning, inning_topbot,
    batter, pitcher, stand, p_throws, events, bb_type, home_team,
    away_team, game_type, outs_when_up), and
  - each player's 2025 / 2024 MLB season lines vs each hand (JSON).

For every game in the test window, for each team's 9 starting hitters vs
the opposing starting pitcher:
  - 2026 inputs = his plate appearances BEFORE that date (rebuilt from the
    Savant file), plus 2025 / 2024 season lines (complete, so no lookahead);
  - runs the production math in matchup_estimate.py (5/4/3 seasons,
    regression, odds ratio, 1+ hit / 1+ HR over expected PA by lineup spot,
    starter share from his last 5 starts);
  - records whether he actually got a hit / homered in that game.

Not included (no point-in-time data for them yet): the pitch-mix nudge and
head-to-head at-bats. Both move OPS by about .01 on average, so the
backtest measures the core model.

Run: python3 scripts/backtest.py PA_CSV PRIOR_JSON START END [OUT_JSON]
"""
from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import matchup_estimate as m  # noqa: E402
import model_constants as mc  # noqa: E402

HITS = {"single": 1, "double": 2, "triple": 3, "home_run": 4}
WALKS = {"walk", "intent_walk"}
HBP = {"hit_by_pitch"}
STRIKEOUTS = {"strikeout", "strikeout_double_play"}
NOT_AB = WALKS | HBP | {"sac_fly", "sac_bunt", "sac_fly_double_play", "sac_bunt_double_play", "catcher_interf"}
FIELDS = ["game_date", "game_pk", "at_bat_number", "inning", "inning_topbot", "batter", "pitcher",
          "stand", "p_throws", "events", "bb_type", "home_team", "away_team", "game_type", "outs_when_up"]
DEFAULT_STARTER_BF = 22.0   # same default as /api/estimates
PRIOR_YEARS = ("2025", "2024")   # seasons before the test season, newest first (weights: m.SEASON_WEIGHTS)


def empty():
    return {"pa": 0, "ab": 0, "ob": 0, "so": 0, "hr": 0, "avg": 0, "slg": 0}


def add_pa(c: dict, ev: str) -> None:
    c["pa"] += 1
    if ev not in NOT_AB:
        c["ab"] += 1
    if ev in HITS:
        c["avg"] += 1
        c["slg"] += HITS[ev]
        c["ob"] += 1
    if ev in WALKS or ev in HBP:
        c["ob"] += 1
    if ev in STRIKEOUTS:
        c["so"] += 1
    if ev == "home_run":
        c["hr"] += 1


def line_to_counts(line):
    """[PA or BF, AB, H, BB, HBP, SO, HR, TB] -> counts, or None."""
    if not line:
        return None
    pa, ab, h, bb, hbp, so, hr, tb = line
    return {"pa": pa, "ab": ab, "ob": h + bb + hbp, "so": so, "hr": hr, "avg": h, "slg": tb}


def load_pas(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for r in csv.reader(f):
            if len(r) != len(FIELDS) or not r[1].isdigit():
                continue
            d = dict(zip(FIELDS, r))
            if d["game_type"] != "R" or not d["events"]:
                continue
            for k in ("game_pk", "at_bat_number", "inning", "batter", "pitcher"):
                d[k] = int(d[k])
            rows.append(d)
    rows.sort(key=lambda d: (d["game_date"], d["game_pk"], d["at_bat_number"]))
    return rows


def games_by_date(rows):
    out = defaultdict(lambda: defaultdict(list))
    for d in rows:
        out[d["game_date"]][d["game_pk"]].append(d)
    return out


def game_structure(pas: list[dict]) -> dict | None:
    """Starting pitchers, batting orders (first 9 distinct batters per
    team, in order of their first PA) and each batter's game outcome."""
    side = {"Top": "away", "Bot": "home"}   # top of the inning = away team batting
    order = {"away": [], "home": []}
    starter = {}
    outcome = defaultdict(lambda: {"h": 0, "hr": 0, "pa": 0})
    stand = {}
    for d in pas:
        bat_team = side.get(d["inning_topbot"])
        if bat_team is None:
            return None
        if bat_team not in starter:
            starter[bat_team] = (d["pitcher"], d["p_throws"])   # first pitcher they faced
        if d["batter"] not in order[bat_team] and len(order[bat_team]) < 9:
            order[bat_team].append(d["batter"])
        if d["pitcher"] == starter[bat_team][0]:
            if not stand.get(d["batter"], (None, False))[1]:
                stand[d["batter"]] = (d["stand"], True)          # side he actually took vs the starter
        elif d["p_throws"] == starter[bat_team][1]:
            stand.setdefault(d["batter"], (d["stand"], False))   # same hand as the starter: same side
        o = outcome[d["batter"]]
        o["pa"] += 1
        o["h"] += d["events"] in HITS
        o["hr"] += d["events"] == "home_run"
    if len(order["away"]) < 9 or len(order["home"]) < 9 or len(starter) < 2:
        return None
    # A pitcher listed first for both sides would mean a bad sort; skip.
    if starter["away"][0] == starter["home"][0]:
        return None
    return {"order": order, "starter": starter, "outcome": outcome,
            "stand": {b: v[0] for b, v in stand.items()}}


def _add(a, b):
    return {k: a[k] + b[k] for k in a}


def _sum_lines(*lines):
    cs = [line_to_counts(l) for l in lines if l]
    if not cs:
        return None
    out = cs[0]
    for c in cs[1:]:
        out = _add(out, c)
    return out


def _rates(c):
    return {r: c[r] / c["ab" if r in m.AB_RATES else "pa"] for r in m.RATES + m.AB_RATES}


league_keys = m.RATES + m.AB_RATES


def run(pa_csv: str, prior_json: str, start: str, end: str, pa_model: str = "spot",
        talent: str = "hand", k_platoon: float = 1000.0, platoon_src: str = "pointintime") -> dict:
    rows = load_pas(pa_csv)
    prior = json.loads(Path(prior_json).read_text())
    by_date = games_by_date(rows)

    hit_cum = defaultdict(lambda: {"L": empty(), "R": empty()})     # batter -> vs pitcher hand
    pit_cum = defaultdict(lambda: {"L": empty(), "R": empty()})     # pitcher -> vs batter side
    lg = empty()
    team_games = 0
    starts = defaultdict(list)   # pitcher -> [(date, batters faced)]
    spot_pa = defaultdict(lambda: [0, 0])   # (home/away, spot) -> [starter PAs, starter games]
    spot_hist = defaultdict(lambda: defaultdict(int))   # (home/away, spot) -> {PA count: games}
    lg_type = {"same": empty(), "opp": empty()}   # batter side vs pitcher hand
    stands = defaultdict(set)

    preds = []
    for date in sorted(by_date):
        games = by_date[date]
        if start <= date <= end and lg["pa"]:
            league = m.league_rates([{"plateAppearances": lg["pa"], "atBats": lg["ab"], "hits": lg["avg"],
                                      "baseOnBalls": lg["ob"] - lg["avg"], "hitByPitch": 0,
                                      "strikeOuts": lg["so"], "homeRuns": lg["hr"],
                                      "totalBases": lg["slg"], "gamesPlayed": team_games}])
            lg_type_rates = {k: _rates(v) for k, v in lg_type.items()}
            switch_hitters = {b for b, st in stands.items() if len(st) > 1}
            for gpk, pas in games.items():
                g = game_structure(pas)
                if not g:
                    continue
                for bat_team, pit_team in (("away", "home"), ("home", "away")):
                    sp, throws = g["starter"][bat_team]
                    hist = [bf for _, bf in starts[sp][-5:]]
                    avg_bf = sum(hist) / len(hist) if hist else DEFAULT_STARTER_BF
                    share = min(1.0, avg_bf / league["pa_per_team_game"])
                    for spot, bid in enumerate(g["order"][bat_team], start=1):
                        side = g["stand"].get(bid)
                        if talent == "all" and side:
                            pr_h = [prior["hit"].get(y, {}).get(str(bid)) or {} for y in PRIOR_YEARS]
                            pr_p = [prior["pit"].get(y, {}).get(str(sp)) or {} for y in PRIOR_YEARS]
                            switch = bid in switch_hitters
                            mt = "same" if side == throws else "opp"
                            pf = {r: lg_type_rates[mt][r] / league[r] for r in league_keys}
                            if platoon_src == "constants":
                                pf = dict(mc.PLATOON_FACTOR[mt])
                            ones = {r: 1.0 for r in league_keys}
                            b = m.talent_rates(
                                [_add(hit_cum[bid]["L"], hit_cum[bid]["R"]) if hit_cum[bid]["L"]["pa"] + hit_cum[bid]["R"]["pa"] else None] +
                                [_sum_lines(x.get("vl"), x.get("vr")) for x in pr_h],
                                [hit_cum[bid][throws] if hit_cum[bid][throws]["pa"] else None] +
                                [line_to_counts(x.get("v" + throws.lower())) for x in pr_h],
                                league, ones if switch else pf, m.HITTER_STABILIZE, k_platoon)
                            pt = m.talent_rates(
                                [_add(pit_cum[sp]["L"], pit_cum[sp]["R"]) if pit_cum[sp]["L"]["pa"] + pit_cum[sp]["R"]["pa"] else None] +
                                [_sum_lines(x.get("vl"), x.get("vr")) for x in pr_p],
                                [pit_cum[sp][side] if pit_cum[sp][side]["pa"] else None] +
                                [line_to_counts(x.get("v" + side.lower())) for x in pr_p],
                                league, pf, m.PITCHER_STABILIZE, k_platoon)
                            est = m.combine_rates(b, pt, league)
                            pen = m.combine_rates(b, {r: league[r] * (1.0 if switch else pf[r]) for r in league_keys}, league)
                            h_counts = {"pa": 1}
                        else:
                            h_counts = m.combine_seasons(
                                [hit_cum[bid][throws] if hit_cum[bid][throws]["pa"] else None] +
                                [line_to_counts((prior["hit"].get(y, {}).get(str(bid)) or {}).get("v" + throws.lower()))
                                 for y in PRIOR_YEARS])
                            p_counts = m.combine_seasons(
                                [pit_cum[sp][side] if side and pit_cum[sp][side]["pa"] else None] +
                                [line_to_counts((prior["pit"].get(y, {}).get(str(sp)) or {}).get("v" + side.lower()))
                                 if side else None for y in PRIOR_YEARS]) if side else None
                            est = m.estimate(h_counts, p_counts, league)
                            pen = m.estimate(h_counts, None, league)
                        if pa_model == "empirical" and spot_pa[(bat_team, spot)][1]:
                            tot, n = spot_pa[(bat_team, spot)]
                            pa_exp = tot / n
                        else:
                            pa_exp = m.expected_pa(league["pa_per_team_game"], spot)
                        hp, hpen = m.hits_per_pa(est, league), m.hits_per_pa(pen, league)
                        if pa_model == "constants":
                            dist = m.pa_distribution(spot, bat_team == "home")
                            pa_exp = sum(n * w for n, w in dist)
                            p_hit = m.chance_over_pa_distribution(hp, hpen, dist, share)
                            p_hr = m.chance_over_pa_distribution(est["hr"], pen["hr"], dist, share)
                            base_hit = m.chance_over_pa_distribution(league["hits_per_pa"], league["hits_per_pa"], dist, share)
                        elif pa_model == "distribution" and spot_hist[(bat_team, spot)]:
                            hist = spot_hist[(bat_team, spot)]
                            tot = sum(hist.values())
                            pa_exp = sum(n * c for n, c in hist.items()) / tot
                            p_hit = sum(c / tot * m.hit_chance_tonight(hp, hpen, n, share) for n, c in hist.items())
                            p_hr = sum(c / tot * m.hr_chance_tonight(est["hr"], pen["hr"], n, share) for n, c in hist.items())
                            base_hit = sum(c / tot * m.hit_chance_tonight(league["hits_per_pa"], league["hits_per_pa"], n, share)
                                           for n, c in hist.items())
                        else:
                            p_hit = m.hit_chance_tonight(hp, hpen, pa_exp, share)
                            p_hr = m.hr_chance_tonight(est["hr"], pen["hr"], pa_exp, share)
                            base_hit = m.hit_chance_tonight(league["hits_per_pa"], league["hits_per_pa"], pa_exp, share)
                        o = g["outcome"][bid]
                        preds.append({
                            "date": date, "game": gpk, "batter": bid, "spot": spot, "throws": throws, "home": bat_team == "home",
                            "side": side, "pa_exp": round(pa_exp, 2), "pa_actual": o["pa"],
                            "p_hit": p_hit, "p_hr": p_hr, "p_hit_league": base_hit,
                            "hit": int(o["h"] > 0), "hr": int(o["hr"] > 0),
                            "h_pa": round(h_counts["pa"], 1) if h_counts else 0,
                            "est_avg": est["avg"], "est_ob": est["ob"],
                        })
        # Fold this day's plate appearances into the running totals AFTER
        # predicting it -- no game sees its own results.
        for gpk, pas in games.items():
            team_games += 2
            g = game_structure(pas)
            for d in pas:
                add_pa(hit_cum[d["batter"]][d["p_throws"]], d["events"])
                add_pa(pit_cum[d["pitcher"]][d["stand"]], d["events"])
                add_pa(lg, d["events"])
                add_pa(lg_type["same" if d["stand"] == d["p_throws"] else "opp"], d["events"])
                stands[d["batter"]].add(d["stand"])
            if g:
                for bat_team in ("away", "home"):
                    for spot, bid in enumerate(g["order"][bat_team], start=1):
                        spot_pa[(bat_team, spot)][0] += g["outcome"][bid]["pa"]
                        spot_pa[(bat_team, spot)][1] += 1
                        spot_hist[(bat_team, spot)][g["outcome"][bid]["pa"]] += 1
                    sp = g["starter"][bat_team][0]
                    starts[sp].append((date, sum(1 for d in pas if d["pitcher"] == sp)))
    return {"preds": preds, "league_pa": lg["pa"]}


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def logloss(ps, ys):
    e = 1e-9
    return -sum(y * math.log(max(p, e)) + (1 - y) * math.log(max(1 - p, e)) for p, y in zip(ps, ys)) / len(ps)


def calibration(preds, key, outcome, edges):
    out = []
    for lo, hi in zip(edges, edges[1:]):
        b = [p for p in preds if lo <= p[key] < hi]
        if b:
            out.append({"bucket": f"{lo:.0%}-{hi:.0%}", "n": len(b),
                        "predicted": sum(p[key] for p in b) / len(b),
                        "actual": sum(p[outcome] for p in b) / len(b)})
    return out


def report(preds) -> dict:
    ys = [p["hit"] for p in preds]
    hr = [p["hr"] for p in preds]
    by_spot = []
    for s in range(1, 10):
        b = [p for p in preds if p["spot"] == s]
        if b:
            by_spot.append({"spot": s, "n": len(b), "pa_exp": b[0]["pa_exp"],
                            "pa_actual": sum(p["pa_actual"] for p in b) / len(b),
                            "pred_hit": sum(p["p_hit"] for p in b) / len(b),
                            "actual_hit": sum(p["hit"] for p in b) / len(b)})
    return {
        "n": len(preds),
        "hit": {"actual_rate": sum(ys) / len(ys), "predicted_rate": sum(p["p_hit"] for p in preds) / len(preds),
                "brier_model": brier([p["p_hit"] for p in preds], ys),
                "brier_league_only": brier([p["p_hit_league"] for p in preds], ys),
                "logloss_model": logloss([p["p_hit"] for p in preds], ys),
                "logloss_league_only": logloss([p["p_hit_league"] for p in preds], ys),
                "calibration": calibration(preds, "p_hit", "hit", [0, .45, .5, .55, .6, .65, .7, .75, 1.01])},
        "hr": {"actual_rate": sum(hr) / len(hr), "predicted_rate": sum(p["p_hr"] for p in preds) / len(preds),
               "brier_model": brier([p["p_hr"] for p in preds], hr),
               "calibration": calibration(preds, "p_hr", "hr", [0, .05, .08, .11, .14, .17, .20, 1.01])},
        "by_spot": by_spot,
    }


if __name__ == "__main__":
    pa_csv, prior_json, start, end = sys.argv[1:5]
    res = run(pa_csv, prior_json, start, end, pa_model=(sys.argv[6] if len(sys.argv) > 6 else "spot"),
              talent=(sys.argv[7] if len(sys.argv) > 7 else "hand"),
              k_platoon=float(sys.argv[8]) if len(sys.argv) > 8 else 1000.0,
              platoon_src=(sys.argv[9] if len(sys.argv) > 9 else "pointintime"))
    rep = report(res["preds"])
    print(json.dumps(rep, indent=1))
    if len(sys.argv) > 5:
        Path(sys.argv[5]).write_text(json.dumps(res["preds"]))
