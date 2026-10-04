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


def parse_schedule(raw: dict) -> list[dict]:
    """Flatten the schedule response into one row per game."""
    games = []
    for date_block in raw.get("dates", []):
        for g in date_block.get("games", []):
            teams = g.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            venue = g.get("venue", {})
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
        })
    return players
