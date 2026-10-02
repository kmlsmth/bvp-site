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
    "homeRuns", "strikeOuts", "baseOnBalls", "intentionalWalks", "hitByPitch",
    "totalBases", "rbi", "leftOnBase", "sacBunts", "sacFlies",
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
    "strikeOuts": "strike_outs",
    "baseOnBalls": "base_on_balls",
    "intentionalWalks": "intentional_walks",
    "hitByPitch": "hit_by_pitch",
    "totalBases": "total_bases",
    "rbi": "rbi",
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
                row["opponent_id"] = split.get("opponent", {}).get("id")
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
            games.append({
                "game_pk": g.get("gamePk"),
                "game_date": g.get("officialDate"),
                "game_type": g.get("gameType"),
                "status": g.get("status", {}).get("detailedState"),
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
