"""
Pure parsing functions: raw MLB Stats API JSON -> plain dict rows ready for
the database. No network calls here, which is what makes these easy to
unit-test against saved sample files (see tests/test_parsing.py).
"""
from __future__ import annotations

# Stat fields we keep, and the row key we store them under. Shared between
# the career total row and every season row, since both come from the same
# "stat" object shape.
STAT_FIELDS = [
    "gamesPlayed", "atBats", "plateAppearances", "hits", "doubles", "triples",
    "homeRuns", "runs", "strikeOuts", "baseOnBalls", "intentionalWalks", "hitByPitch",
    "totalBases", "rbi", "stolenBases", "caughtStealing", "leftOnBase", "sacBunts", "sacFlies",
    "groundIntoDoublePlay", "numberOfPitches", "avg", "obp", "slg", "ops",
]

# camelCase (API) -> snake_case (our column names)
FIELD_TO_COLUMN = {
    "gamesPlayed": "games_played",
    "atBats": "at_bats",
    "plateAppearances": "plate_appearances",
    "hits": "hits",
    "doubles": "doubles",
    "triples": "triples",
    "homeRuns": "home_runs",
    "runs": "runs",
    "strikeOuts": "strike_outs",
    "baseOnBalls": "base_on_balls",
    "intentionalWalks": "intentional_walks",
    "hitByPitch": "hit_by_pitch",
    "totalBases": "total_bases",
    "rbi": "rbi",
    "stolenBases": "stolen_bases",
    "caughtStealing": "caught_stealing",
    "leftOnBase": "left_on_base",
    "sacBunts": "sac_bunts",
    "sacFlies": "sac_flies",
    "groundIntoDoublePlay": "ground_into_double_play",
    "numberOfPitches": "number_of_pitches",
    "avg": "avg",
    "obp": "obp",
    "slg": "slg",
    "ops": "ops",
}


def _stat_row(stat: dict) -> dict:
    return {FIELD_TO_COLUMN[f]: stat.get(f) for f in STAT_FIELDS}


def parse_vs_player(raw: dict) -> tuple[dict | None, list[dict]]:
    """
    Returns (career_row, season_rows) for one batter-vs-pitcher response.

    career_row is None if the batter has never faced the pitcher (the API
    omits the vsPlayerTotal block entirely in that case).
    """
    career_row = None
    season_rows: list[dict] = []

    for block in raw.get("stats", []):
        display_name = block.get("type", {}).get("displayName")
        splits = block.get("splits", [])

        if display_name == "vsPlayerTotal":
            if not splits:
                continue
            split = splits[0]
            row = _stat_row(split["stat"])
            row["batter_id"] = split["batter"]["id"]
            row["pitcher_id"] = split["pitcher"]["id"]
            career_row = row

        elif display_name == "vsPlayer":
            for split in splits:
                row = _stat_row(split["stat"])
                row["batter_id"] = split["batter"]["id"]
                row["pitcher_id"] = split["pitcher"]["id"]
                row["season"] = split.get("season")
                row["team_id"] = split.get("team", {}).get("id")
                row["team_name"] = split.get("team", {}).get("name")
                row["opponent_id"] = split.get("opponent", {}).get("id")
                row["opponent_name"] = split.get("opponent", {}).get("name")
                season_rows.append(row)

    return career_row, season_rows


def _lineup_rows(players: list[dict]) -> list[dict]:
    """hydrate=lineups gives each side's players as a plain list already in
    batting-order sequence (1st = leadoff) -- there's no explicit "batting
    order" number on each entry, so the array position *is* the order.
    This is MLB's confirmed starting lineup once they've submitted it
    (usually 1-3 hours before first pitch), not an early projection/guess
    -- there's no "likely lineup" data in this public API, so the front
    end should say "not announced yet" rather than imply a projection."""
    rows = []
    for i, p in enumerate(players or []):
        pos = p.get("primaryPosition") or {}
        rows.append({
            "order": i + 1,
            "id": p.get("id"),
            "name": p.get("fullName"),
            "position": pos.get("abbreviation"),
        })
    return rows


def parse_schedule(raw: dict) -> list[dict]:
    """Flatten the schedule response into one row per game."""
    games = []
    for date_block in raw.get("dates", []):
        for g in date_block.get("games", []):
            teams = g.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            venue = g.get("venue", {})
            lineups = g.get("lineups") or {}
            games.append({
                "game_pk": g.get("gamePk"),
                "game_date": g.get("officialDate"),
                "game_date_time": g.get("gameDate"),  # full first-pitch timestamp, for matching a weather forecast hour
                "game_type": g.get("gameType"),
                "status": g.get("status", {}).get("detailedState"),
                "venue_id": venue.get("id"),
                "venue_name": venue.get("name"),
                "home_team_id": home.get("team", {}).get("id"),
                "home_team_name": home.get("team", {}).get("name"),
                "away_team_id": away.get("team", {}).get("id"),
                "away_team_name": away.get("team", {}).get("name"),
                "home_probable_pitcher_id": home.get("probablePitcher", {}).get("id"),
                "home_probable_pitcher_name": home.get("probablePitcher", {}).get("fullName"),
                "away_probable_pitcher_id": away.get("probablePitcher", {}).get("id"),
                "away_probable_pitcher_name": away.get("probablePitcher", {}).get("fullName"),
                "home_lineup": _lineup_rows(lineups.get("homePlayers")),
                "away_lineup": _lineup_rows(lineups.get("awayPlayers")),
            })
    return games


def parse_venue(raw: dict) -> dict | None:
    """One ballpark's location, orientation, and field dimensions.

    Returns None if the API didn't return a venue at all (bad id). A
    venue missing azimuth_angle or lat/lon (MLB doesn't publish location
    data for every park) still parses fine -- those fields just come
    back None, and callers (scripts/venues.py) handle that gracefully.
    """
    venues = raw.get("venues") or []
    if not venues:
        return None
    v = venues[0]
    location = v.get("location", {}) or {}
    coords = location.get("defaultCoordinates", {}) or {}
    field = v.get("fieldInfo", {}) or {}
    return {
        "id": v.get("id"),
        "name": v.get("name"),
        "city": location.get("city"),
        "state": location.get("stateAbbrev") or location.get("state"),
        "lat": coords.get("latitude"),
        "lon": coords.get("longitude"),
        "azimuth_angle": location.get("azimuthAngle"),
        "elevation": location.get("elevation"),
        "roof_type": field.get("roofType"),
        "capacity": field.get("capacity"),
        "left_line": field.get("leftLine"),
        "left_center": field.get("leftCenter"),
        "center": field.get("center"),
        "right_center": field.get("rightCenter"),
        "right_line": field.get("rightLine"),
    }


def _innings_pitched_to_outs(ip_str: str | None) -> int | None:
    """MLB reports innings pitched as e.g. "6.0", "5.1", "5.2" -- the digit
    after the decimal is OUTS (1 or 2), not a tenth of an inning. Converts
    to a plain outs-recorded integer (18, 16, 17) so appearances can be
    summed correctly; "5.1" + "0.2" has to equal "6.0", which only works
    in outs, not in floating-point innings."""
    if not ip_str:
        return None
    whole_str, _, frac_str = ip_str.partition(".")
    try:
        whole = int(whole_str)
        frac = int(frac_str) if frac_str else 0
    except ValueError:
        return None
    if frac not in (0, 1, 2):
        frac = 0  # defensive -- MLB shouldn't send anything else here
    return whole * 3 + frac


def parse_boxscore(raw: dict, game_pk: int, game_date: str) -> list[dict]:
    """Every pitcher's line from one game's box score, for both teams.

    Starter vs. reliever is read two ways and cross-checked: the
    "gamesStarted" flag on the player's own pitching stat line, and
    whether they're first in their team's "pitchers" list (the order
    pitchers entered the game) -- belt and suspenders, since this feeds a
    public-facing fatigue grade and getting the one starter wrong per team
    would throw it off. A player with no "pitching" stats block didn't
    pitch in this game and is skipped.
    """
    appearances: list[dict] = []
    teams = raw.get("teams", {}) or {}
    for side in ("home", "away"):
        team_block = teams.get(side, {}) or {}
        team_id = (team_block.get("team") or {}).get("id")
        first_pitcher_id = (team_block.get("pitchers") or [None])[0]
        players = team_block.get("players", {}) or {}

        for p in players.values():
            pitching = ((p.get("stats") or {}).get("pitching")) or {}
            if not pitching:
                continue
            person = p.get("person") or {}
            pitcher_id = person.get("id")
            if pitcher_id is None:
                continue
            outs = _innings_pitched_to_outs(pitching.get("inningsPitched"))
            if outs is None:
                continue

            is_starter = bool(pitching.get("gamesStarted")) or pitcher_id == first_pitcher_id
            appearances.append({
                "game_pk": game_pk,
                "pitcher_id": pitcher_id,
                "pitcher_name": person.get("fullName"),
                "team_id": team_id,
                "game_date": game_date,
                "role": "starter" if is_starter else "reliever",
                "outs": outs,
                "pitches": pitching.get("numberOfPitches"),
                "batters_faced": pitching.get("battersFaced"),
                "earned_runs": pitching.get("earnedRuns"),
                "base_on_balls": pitching.get("baseOnBalls"),
                "strike_outs": pitching.get("strikeOuts"),
                "hits": pitching.get("hits"),
            })
    return appearances


def parse_roster(raw: dict) -> list[dict]:
    """One row per player on a team's roster."""
    players = []
    for entry in raw.get("roster", []):
        person = entry.get("person", {})
        position = entry.get("position", {})
        players.append({
            "id": person.get("id"),
            "full_name": person.get("fullName"),
            "position_code": position.get("code"),
            "position_type": position.get("type"),  # e.g. "Pitcher", "Outfielder"
            "position_abbr": position.get("abbreviation"),  # e.g. "SS"
        })
    return players


# --- A pitcher's own pitching stats (starter overview card) ----------------

def _pitching_count_row(stat: dict) -> dict:
    """One pitching stat line -> the same raw column names pitcher_appearances
    uses, so the existing rate-stat math in bullpen.py works on it
    unchanged. MLB includes a plain "outs" count on these lines (verified
    live); inningsPitched ("5.2" = 5 innings + 2 outs) is the fallback."""
    outs = stat.get("outs")
    if outs is None:
        outs = _innings_pitched_to_outs(stat.get("inningsPitched"))
    return {
        "outs": outs,
        "pitches": stat.get("numberOfPitches"),
        "batters_faced": stat.get("battersFaced"),
        "earned_runs": stat.get("earnedRuns"),
        "base_on_balls": stat.get("baseOnBalls"),
        "strike_outs": stat.get("strikeOuts"),
        "hits": stat.get("hits"),
        "games_started": stat.get("gamesStarted"),
    }


def parse_pitching_totals(raw: dict) -> dict:
    """Response from mlb_api.get_pitching_stats(..., "season,career") ->
    {"season": row or None, "career": row or None}. A pitcher with no
    appearances this season simply has no "season" split.

    A pitcher traded mid-season gets SEVERAL season splits: one per team
    plus a combined total (seen live: Freddy Peralta 2026 -> 32 GS total,
    22 + 10 by team). The combined line is the one with the most outs --
    picked that way rather than by position in the list, which MLB
    doesn't document."""
    out = {"season": None, "career": None}
    for block in raw.get("stats", []):
        kind = (block.get("type") or {}).get("displayName")
        splits = block.get("splits") or []
        if kind in out and splits:
            rows = [_pitching_count_row(sp.get("stat") or {}) for sp in splits]
            out[kind] = max(rows, key=lambda r: r["outs"] or 0)
    return out


def parse_pitching_gamelog(raw: dict) -> list[dict]:
    """Response from mlb_api.get_pitching_stats(..., "gameLog") -> one row
    per game pitched: date, game type (R, or F/D/L/W for postseason),
    whether he started, and that game's raw counts."""
    rows = []
    for block in raw.get("stats", []):
        if (block.get("type") or {}).get("displayName") != "gameLog":
            continue
        for split in block.get("splits") or []:
            row = _pitching_count_row(split.get("stat") or {})
            row["game_date"] = split.get("date")
            row["game_type"] = split.get("gameType")
            row["game_pk"] = (split.get("game") or {}).get("gamePk")
            row["started"] = bool(row["games_started"])
            row["opponent"] = (split.get("opponent") or {}).get("name")
            row["is_home"] = split.get("isHome")
            rows.append(row)
    return rows


def parse_person_throws(raw: dict) -> dict:
    """Response from mlb_api.get_person() -> name + throwing hand ("L"/"R",
    or None if MLB doesn't list one)."""
    people = raw.get("people") or [{}]
    person = people[0] or {}
    return {
        "full_name": person.get("fullName"),
        "throws": (person.get("pitchHand") or {}).get("code"),
    }


# --- Handedness splits (matchup estimate) ---------------------------------

def _first_split(stat_blocks: list, code: str | None = None) -> dict | None:
    """The split matching `code`. A player traded mid-season gets SEVERAL
    matching splits -- one per team plus a combined line, in no reliable
    order (seen live 2026-10-06: Luis García Jr. vs RHP = 317 + 139 PA by
    team, then 456 combined; Freddy Peralta's season line had the combined
    one FIRST). The combined line is always the biggest, so take that."""
    best, best_n = None, -1
    for block in stat_blocks or []:
        for sp in block.get("splits") or []:
            if code is not None and (sp.get("split") or {}).get("code") != code:
                continue
            st = sp.get("stat") or {}
            n = st.get("plateAppearances") or st.get("battersFaced") or 0
            if n > best_n:
                best, best_n = sp, n
    return best


def parse_hitters_vs_hand(raw: dict, sit_code: str) -> dict:
    """mlb_api.get_hitters_vs_hand() -> {player_id: {"name", "bats",
    "stat" (MLB's raw hitting line vs that hand, or None if he hasn't
    faced that hand this season)}}."""
    out = {}
    for p in raw.get("people") or []:
        sp = _first_split(p.get("stats"), sit_code)
        out[p.get("id")] = {
            "name": p.get("fullName"),
            "bats": (p.get("batSide") or {}).get("code"),
            "stat": (sp or {}).get("stat"),
        }
    return out


def parse_pitcher_vs_hand(raw: dict) -> dict:
    """mlb_api.get_pitcher_vs_hand() -> {"L": raw line vs lefty hitters,
    "R": raw line vs righty hitters} (None where he has no data)."""
    blocks = raw.get("stats") or []
    vl, vr = _first_split(blocks, "vl"), _first_split(blocks, "vr")
    return {"L": (vl or {}).get("stat"), "R": (vr or {}).get("stat")}


def parse_team_hitting_lines(raw: dict) -> list[dict]:
    """mlb_api.get_league_team_hitting() -> one raw hitting line per team."""
    blocks = raw.get("stats") or []
    return [sp.get("stat") or {} for b in blocks for sp in (b.get("splits") or [])]


# ---- Daily archive: hitters' lines and final score from a box score ------

def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def parse_boxscore_batting(raw: dict, game_pk: int, game_date: str) -> list[dict]:
    """Every hitter who batted in this game, both teams. MLB's per-player
    "battingOrder" code gives the lineup spot and whether he started: "300"
    = started in the 3 hole, "301" = the first substitute in that spot
    (verified on the 2026-10-06 Dodgers @ Braves box score). Players with no
    batting line (pitchers in a DH game, unused bench) are skipped."""
    rows = []
    teams = raw.get("teams") or {}
    for side in ("away", "home"):
        t = teams.get(side) or {}
        team_id = (t.get("team") or {}).get("id")
        for p in (t.get("players") or {}).values():
            bat = ((p.get("stats") or {}).get("batting")) or {}
            pa = _int(bat.get("plateAppearances"))
            code = p.get("battingOrder")
            if not pa or not code:
                continue
            code_i = _int(code)
            pid = (p.get("person") or {}).get("id")
            if code_i is None or pid is None:
                continue
            rows.append({
                "game_pk": game_pk, "batter_id": pid, "team_id": team_id, "game_date": game_date,
                "is_home": 1 if side == "home" else 0,
                "batting_order": code_i // 100, "started": 1 if code_i % 100 == 0 else 0,
                "position": (p.get("position") or {}).get("abbreviation"),
                "pa": pa, "ab": _int(bat.get("atBats")), "h": _int(bat.get("hits")),
                "d2": _int(bat.get("doubles")), "d3": _int(bat.get("triples")),
                "hr": _int(bat.get("homeRuns")), "bb": _int(bat.get("baseOnBalls")),
                "ibb": _int(bat.get("intentionalWalks")), "hbp": _int(bat.get("hitByPitch")),
                "so": _int(bat.get("strikeOuts")), "tb": _int(bat.get("totalBases")),
                "r": _int(bat.get("runs")), "rbi": _int(bat.get("rbi")),
                "sb": _int(bat.get("stolenBases")), "sf": _int(bat.get("sacFlies")),
            })
    return rows


def parse_game_result(raw: dict, game_pk: int, game_date: str, game_type: str | None) -> dict:
    """Final score (runs, hits) from a box score's team batting totals."""
    teams = raw.get("teams") or {}

    def side(s):
        t = teams.get(s) or {}
        bat = ((t.get("teamStats") or {}).get("batting")) or {}
        return (t.get("team") or {}).get("id"), _int(bat.get("runs")), _int(bat.get("hits"))
    a_id, a_r, a_h = side("away")
    h_id, h_r, h_h = side("home")
    return {"game_pk": game_pk, "game_date": game_date, "game_type": game_type,
            "away_team_id": a_id, "home_team_id": h_id, "away_runs": a_r, "home_runs": h_r,
            "away_hits": a_h, "home_hits": h_h}


def parse_schedule_finals(raw: dict) -> list[dict]:
    """Final games from a schedule response: [{game_pk, game_date, game_type}].
    Uses abstractGameState == "Final" (covers "Final", "Game Over",
    "Completed Early"); postponed / suspended games aren't Final."""
    out = []
    for d in raw.get("dates") or []:
        for g in d.get("games") or []:
            st = g.get("status") or {}
            detailed = (st.get("detailedState") or "").lower()
            if (st.get("abstractGameState") == "Final" and st.get("codedGameState") not in ("D", "C")
                    and not any(w in detailed for w in ("postponed", "cancelled", "suspended"))):
                out.append({"game_pk": g.get("gamePk"),
                            "game_date": g.get("officialDate") or d.get("date"),
                            "game_type": g.get("gameType")})
    return out
