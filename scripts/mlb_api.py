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
