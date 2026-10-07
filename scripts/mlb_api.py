"""
Thin wrapper around the public MLB Stats API (statsapi.mlb.com).

No API key is required. These are plain HTTPS GET calls — the functions
here just build the right URL and return the parsed JSON. All the
batter-vs-pitcher specific parsing lives in parsing.py, kept separate so it
can be unit-tested against saved sample responses without any network
access.
"""
from __future__ import annotations

import requests

BASE_URL = "https://statsapi.mlb.com/api/v1"
TIMEOUT = 15


def get_schedule(date: str, sport_id: int = 1) -> dict:
    """Games on a given date (YYYY-MM-DD), with probable pitchers, venue,
    and (once MLB posts them -- usually 1-3 hours before first pitch) each
    team's starting lineup attached. Since this one call already runs both
    once a day and every hour (see ingest_daily.refresh_probable_pitchers),
    adding "lineups" here means the lineup gets picked up on that same
    existing cadence for free, the same way a late-announced probable
    pitcher does -- no separate polling needed."""
    url = f"{BASE_URL}/schedule"
    params = {
        "sportId": sport_id,
        "date": date,
        "hydrate": "probablePitcher,team,venue,lineups",
    }
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_team_roster(team_id: int, roster_type: str = "active") -> dict:
    """Current roster for a team (used to find the opposing lineup)."""
    url = f"{BASE_URL}/teams/{team_id}/roster"
    params = {"rosterType": roster_type}
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_venue(venue_id: int) -> dict:
    """Location, field dimensions, and orientation for one ballpark.

    hydrate=location is what gives us lat/long and azimuthAngle (the
    compass bearing from home plate to straightaway center field) --
    everything the weather card needs, from the same free API we're
    already using for every other part of this site.
    """
    url = f"{BASE_URL}/venues/{venue_id}"
    params = {"hydrate": "location,fieldInfo"}
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_vs_player(batter_id: int, pitcher_id: int) -> dict:
    """Career + season-by-season hitting stats for batter_id vs pitcher_id."""
    url = f"{BASE_URL}/people/{batter_id}/stats"
    params = {
        "stats": "vsPlayer",
        "opposingPlayerId": pitcher_id,
        "group": "hitting",
    }
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_team_schedule_range(team_id: int, start_date: str, end_date: str) -> dict:
    """A team's games (any status) between two dates (both YYYY-MM-DD,
    inclusive) -- used to find which recent games need a box score pulled
    for bullpen-fatigue/starter-form tracking, without one API call per day.
    """
    url = f"{BASE_URL}/schedule"
    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": start_date,
        "endDate": end_date,
    }
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_person(person_id: int) -> dict:
    """One player's bio record -- used for a pitcher's throwing hand
    (people[0].pitchHand.code, "L"/"R")."""
    url = f"{BASE_URL}/people/{person_id}"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# MLB game-type codes: R = regular season; F/D/L/W = the four postseason
# rounds (Wild Card, Division Series, LCS, World Series). Verified live:
# passing them comma-separated returns one combined, per-game log.
REGULAR_SEASON = "R"
REGULAR_AND_POSTSEASON = "R,F,D,L,W"


def get_pitching_stats(pitcher_id: int, season: int, stat_types: str,
                       game_types: str = REGULAR_SEASON) -> dict:
    """A pitcher's own pitching stats. stat_types is MLB's comma list, e.g.
    "season,career" (one totals line each) or "gameLog" (one line per
    game, with gamesStarted/outs/earnedRuns/hits/baseOnBalls/strikeOuts)."""
    url = f"{BASE_URL}/people/{pitcher_id}/stats"
    params = {
        "stats": stat_types,
        "group": "pitching",
        "season": season,
        "gameType": game_types,
    }
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_hitters_vs_hand(person_ids: list[int], season: int, sit_code: str) -> dict:
    """Many hitters in ONE call: each one's bio (incl. batSide) plus his
    regular-season line against one pitcher hand -- sit_code "vr" = vs
    right-handed pitchers, "vl" = vs left-handed. Verified live: the
    people endpoint accepts a comma list of ids with this stats hydrate."""
    url = f"{BASE_URL}/people"
    params = {
        "personIds": ",".join(str(i) for i in person_ids),
        "hydrate": (f"stats(group=[hitting],type=[statSplits],sitCodes=[{sit_code}],"
                    f"season={season},gameType=[R])"),
    }
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_pitcher_vs_hand(pitcher_id: int, season: int) -> dict:
    """A pitcher's regular-season line against left-handed ("vl") and
    right-handed ("vr") hitters."""
    url = f"{BASE_URL}/people/{pitcher_id}/stats"
    params = {"stats": "statSplits", "group": "pitching", "season": season,
              "sitCodes": "vl,vr", "gameType": REGULAR_SEASON}
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_league_team_hitting(season: int) -> dict:
    """All 30 teams' regular-season hitting totals (summed into league
    averages by the caller)."""
    url = f"{BASE_URL}/teams/stats"
    params = {"season": season, "group": "hitting", "stats": "season",
              "sportIds": 1, "gameType": REGULAR_SEASON}
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_boxscore(game_pk: int) -> dict:
    """Full box score for one game: every pitcher's line (innings, pitches,
    runs, walks, strikeouts, hits) for both teams. This is the one new data
    source bullpen fatigue and starter recent-form both read from."""
    url = f"{BASE_URL}/game/{game_pk}/boxscore"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ---- Player / team pages and the game summary ---------------------------
# All verified live 2026-10-06 against statsapi.mlb.com.

def _get(path: str, params: dict) -> dict:
    resp = requests.get(f"{BASE_URL}/{path}", params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_person_page(person_id: int) -> dict:
    """Bio for a player page: name, number, age, position, bats/throws, current team."""
    return _get(f"people/{person_id}", {"hydrate": "currentTeam"})


def get_year_by_year(person_id: int, group: str) -> dict:
    """Every regular season of his career ("hitting" or "pitching"); a
    traded player has one row per team that season."""
    return _get(f"people/{person_id}/stats", {"stats": "yearByYear", "group": group,
                                              "gameType": REGULAR_SEASON})


def get_hitting_gamelog(person_id: int, season: int) -> dict:
    """Game-by-game hitting lines, regular season + postseason."""
    return _get(f"people/{person_id}/stats", {"stats": "gameLog", "group": "hitting", "season": season,
                                              "gameType": REGULAR_AND_POSTSEASON})


def get_team_hitting(team_id: int, season: int) -> dict:
    """One team's hitting: season line plus vs LHP ("vl") / vs RHP ("vr")."""
    return _get(f"teams/{team_id}/stats", {"stats": "statSplits,season", "group": "hitting",
                                           "sitCodes": "vl,vr", "season": season,
                                           "gameType": REGULAR_SEASON})


def get_league_team_splits(season: int) -> dict:
    """Every team's hitting vs LHP and vs RHP (60 rows; default page size is
    50, hence limit)."""
    return _get("teams/stats", {"stats": "statSplits", "group": "hitting", "sitCodes": "vl,vr",
                                "season": season, "gameType": REGULAR_SEASON, "sportIds": 1,
                                "limit": 100})
