"""
Player / team pages and the game summary: pure functions over MLB Stats
API responses (no network -- see test_pages.py, which runs on real
responses). Rate stats are always recomputed from counting stats, so a
traded player's two team rows add up correctly.
"""
from __future__ import annotations

HIT_FIELDS = {"pa": "plateAppearances", "ab": "atBats", "h": "hits", "d2": "doubles", "d3": "triples",
              "hr": "homeRuns", "bb": "baseOnBalls", "ibb": "intentionalWalks", "hbp": "hitByPitch",
              "so": "strikeOuts", "tb": "totalBases", "r": "runs", "rbi": "rbi", "sb": "stolenBases",
              "sf": "sacFlies", "g": "gamesPlayed"}
PIT_FIELDS = {"g": "gamesPlayed", "gs": "gamesStarted", "outs": "outs", "bf": "battersFaced",
              "ab": "atBats", "h": "hits", "hr": "homeRuns", "bb": "baseOnBalls", "hbp": "hitByPitch",
              "so": "strikeOuts", "er": "earnedRuns", "pitches": "numberOfPitches", "tb": "totalBases"}


def _n(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _outs(stat: dict) -> int:
    if stat.get("outs") is not None:
        return _n(stat.get("outs"))
    ip = str(stat.get("inningsPitched") or "0")
    whole, _, frac = ip.partition(".")
    return _n(whole) * 3 + _n(frac or 0)


def hit_counts(stat: dict | None) -> dict:
    stat = stat or {}
    return {k: _n(stat.get(v)) for k, v in HIT_FIELDS.items()}


def pit_counts(stat: dict | None) -> dict:
    stat = stat or {}
    c = {k: _n(stat.get(v)) for k, v in PIT_FIELDS.items() if k != "outs"}
    c["outs"] = _outs(stat)
    return c


def add(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}


def _round3(x: float) -> float:
    """Round half up, like MLB (.3125 -> .313; Python's round() gives .312)."""
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(repr(x)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _r3(x: float | None) -> str | None:
    if x is None:
        return None
    s = f"{_round3(x):.3f}"
    return s[1:] if s.startswith("0") else s


def _ops(obp: float, slg: float) -> str:
    """MLB adds the ROUNDED OBP and SLG for its displayed OPS (.321 + .342 =
    .663 for Riley vs LHP 2026, where the unrounded sum is .6623) -- match it."""
    return _r3(_round3(obp) + _round3(slg))


def hit_line(c: dict) -> dict | None:
    """Counting stats -> AVG / OBP / SLG / OPS (MLB definitions), K% and BB%."""
    if not c or not c.get("pa"):
        return None
    ab, pa = c["ab"], c["pa"]
    avg = c["h"] / ab if ab else 0.0
    obp_den = ab + c["bb"] + c["hbp"] + c["sf"]
    obp = (c["h"] + c["bb"] + c["hbp"]) / obp_den if obp_den else 0.0
    slg = c["tb"] / ab if ab else 0.0
    return {"g": c.get("g"), "pa": pa, "ab": ab, "h": c["h"], "hr": c["hr"], "rbi": c.get("rbi"),
            "bb": c["bb"], "so": c["so"], "sb": c.get("sb"),
            "avg": _r3(avg), "obp": _r3(obp), "slg": _r3(slg), "ops": _ops(obp, slg), "ops_num": obp + slg,
            "k_pct": f"{c['so'] / pa * 100:.1f}%", "bb_pct": f"{c['bb'] / pa * 100:.1f}%"}


def pit_line(c: dict) -> dict | None:
    """Counting stats -> ERA, WHIP, K/9, BB/9, HR/9, IP, and what hitters did
    against him (AVG / OBP / SLG / OPS, K%, BB%)."""
    if not c or not (c.get("outs") or c.get("bf")):
        return None
    ip = c["outs"] / 3
    bf = c["bf"]
    ab = c["ab"]
    out = {"g": c.get("g"), "gs": c.get("gs"), "ip": f"{c['outs'] // 3}.{c['outs'] % 3}", "bf": bf,
           "so": c["so"], "bb": c["bb"], "hr": c["hr"],
           "era": f"{c['er'] * 9 / ip:.2f}" if ip else None,
           "whip": f"{(c['bb'] + c['h']) / ip:.2f}" if ip else None,
           "k9": f"{c['so'] * 9 / ip:.1f}" if ip else None,
           "bb9": f"{c['bb'] * 9 / ip:.1f}" if ip else None,
           "hr9": f"{c['hr'] * 9 / ip:.1f}" if ip else None}
    if bf:
        out["k_pct"] = f"{c['so'] / bf * 100:.1f}%"
        out["bb_pct"] = f"{c['bb'] / bf * 100:.1f}%"
    if ab:
        avg = c["h"] / ab
        obp = (c["h"] + c["bb"] + c["hbp"]) / bf if bf else 0.0   # SF not on pitching lines; BF is the fair denominator
        slg = c["tb"] / ab if c.get("tb") else None
        out.update({"avg": _r3(avg), "obp": _r3(obp)})
        if slg is not None:
            out.update({"slg": _r3(slg), "ops": _ops(obp, slg)})
    return out


def _splits(raw: dict) -> list:
    return [sp for block in (raw or {}).get("stats") or [] for sp in block.get("splits") or []]


def by_season(raw: dict, group: str, last_n: int = 3) -> list[dict]:
    """yearByYear -> [{season, team(s), line}] newest first, one row per
    season (a traded player's team rows are added up)."""
    seasons: dict = {}
    teams: dict = {}
    for sp in _splits(raw):
        s = sp.get("season")
        c = hit_counts(sp.get("stat")) if group == "hitting" else pit_counts(sp.get("stat"))
        seasons[s] = add(seasons[s], c) if s in seasons else c
        t = (sp.get("team") or {}).get("name")
        if t:
            teams.setdefault(s, [])
            if t not in teams[s]:
                teams[s].append(t)
    out = []
    for s in sorted(seasons, reverse=True)[:last_n]:
        line = hit_line(seasons[s]) if group == "hitting" else pit_line(seasons[s])
        if line:
            out.append({"season": s, "teams": teams.get(s, []), "line": line})
    return out


def vs_hand(raw: dict, group: str) -> dict:
    """statSplits vl/vr -> {"L": line, "R": line}. Traded players get one row
    per team plus a combined one; the combined (largest) is used."""
    best: dict = {}
    for sp in _splits(raw):
        code = (sp.get("split") or {}).get("code")
        if code not in ("vl", "vr"):
            continue
        st = sp.get("stat") or {}
        n = _n(st.get("plateAppearances") or st.get("battersFaced"))
        if code not in best or n > best[code][0]:
            best[code] = (n, st)
    f = (lambda st: hit_line(hit_counts(st))) if group == "hitting" else (lambda st: pit_line(pit_counts(st)))
    return {"L": f(best["vl"][1]) if "vl" in best else None, "R": f(best["vr"][1]) if "vr" in best else None}


def hitting_games(raw: dict) -> list[dict]:
    """gameLog -> one row per game, oldest first: date, opponent, home/away,
    game type, counts."""
    rows = []
    for sp in _splits(raw):
        if not sp.get("date"):
            continue
        rows.append({"date": sp["date"], "game_type": sp.get("gameType"), "is_home": sp.get("isHome"),
                     "opponent": (sp.get("opponent") or {}).get("name"),
                     "opponent_id": (sp.get("opponent") or {}).get("id"),
                     "game_pk": (sp.get("game") or {}).get("gamePk"),
                     "c": hit_counts(sp.get("stat"))})
    rows.sort(key=lambda r: r["date"])
    return rows


def recent_hitting(games: list[dict], windows=(7, 15, 30)) -> list[dict]:
    """His last N games (regular season + postseason), added up."""
    out = []
    for n in windows:
        g = games[-n:]
        if not g:
            continue
        tot = {}
        for row in g:
            tot = add(tot, row["c"]) if tot else dict(row["c"])
        line = hit_line(tot)
        if line:
            out.append({"games": len(g), "from": g[0]["date"], "to": g[-1]["date"], "line": line})
    return out


def game_log_rows(games: list[dict], n: int = 10) -> list[dict]:
    """The last n games, newest first, for the game-log table."""
    out = []
    for row in reversed(games[-n:]):
        c = row["c"]
        out.append({"date": row["date"], "opponent": row["opponent"], "is_home": row["is_home"],
                    "postseason": row["game_type"] not in (None, "R"),
                    "ab": c["ab"], "h": c["h"], "d2": c["d2"], "d3": c["d3"], "hr": c["hr"],
                    "rbi": c["rbi"], "bb": c["bb"], "so": c["so"], "r": c["r"]})
    return out


def team_lines(raw: dict) -> dict:
    """get_team_hitting -> {"season": line, "L": line vs LHP, "R": line vs RHP}."""
    out = {"season": None, "L": None, "R": None}
    for block in (raw or {}).get("stats") or []:
        kind = (block.get("type") or {}).get("displayName")
        for sp in block.get("splits") or []:
            code = (sp.get("split") or {}).get("code")
            line = hit_line(hit_counts(sp.get("stat")))
            if kind == "season" or (kind is None and code is None):
                out["season"] = line
            elif code == "vl":
                out["L"] = line
            elif code == "vr":
                out["R"] = line
    return out


def league_ranks(raw: dict) -> dict:
    """get_league_team_splits -> {"L": {team_id: rank by OPS}, "R": {...},
    "league": {"L": line, "R": line}} (1 = best of 30)."""
    by = {"vl": [], "vr": []}
    tot = {"vl": {}, "vr": {}}
    for sp in _splits(raw):
        code = (sp.get("split") or {}).get("code")
        tid = (sp.get("team") or {}).get("id")
        if code not in by or tid is None:
            continue
        c = hit_counts(sp.get("stat"))
        line = hit_line(c)
        if line:
            by[code].append((line["ops_num"], tid))
            tot[code] = add(tot[code], c) if tot[code] else dict(c)
    ranks = {}
    for code, side in (("vl", "L"), ("vr", "R")):
        ordered = sorted(by[code], reverse=True)
        ranks[side] = {tid: i + 1 for i, (_, tid) in enumerate(ordered)}
    return {"L": ranks["L"], "R": ranks["R"], "teams": {"L": len(by["vl"]), "R": len(by["vr"])},
            "league": {"L": hit_line(tot["vl"]), "R": hit_line(tot["vr"])}}
