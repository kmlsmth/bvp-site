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
    """Games on a given date (YYYY-MM-DD), with probable pitchers attached."""
    url = f"{BASE_URL}/schedule"
    params = {
        "sportId": sport_id,
        "date": date,
        "hydrate": "probablePitcher,team,venue",
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


def get_boxscore(game_pk: int) -> dict:
    """Full box score for one game: every pitcher's line (innings, pitches,
    runs, walks, strikeouts, hits) for both teams. This is the one new data
    source bullpen fatigue and starter recent-form both read from."""
    url = f"{BASE_URL}/game/{game_pk}/boxscore"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()
