"""
Baseball Savant (MLB's Statcast site) downloads -- the source for
pitch-type data, which the MLB Stats API doesn't break out:

  - pitch-arsenal leaderboards (one CSV for every batter, one for every
    pitcher): per player per pitch type -- pitches seen/thrown, PA, usage,
    and expected stats (xBA / xSLG / xwOBA) on plate appearances that
    ended on that pitch type.
  - one pitcher's pitch-by-pitch log for the season, used only to count
    his pitch mix separately vs left- and right-handed hitters (the
    leaderboard doesn't split by batter hand, and mixes really differ:
    e.g. Freddy Peralta 2026 throws his sweeper 12.5% of the time to
    righties but 1.8% to lefties).

All three verified live (2026-10-06). Plain CSV text is returned; parsing
lives in pitch_mix.py so it can be tested without the network.
"""
from __future__ import annotations

import requests

BASE_URL = "https://baseballsavant.mlb.com"
TIMEOUT = 45  # the pitch-by-pitch download is ~2 MB and takes a few seconds
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AtThePlate/1.0)"}


def _get_text(url: str, params: dict) -> str:
    resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "utf-8-sig"
    return resp.text


def get_arsenal_leaderboard(kind: str, season: int) -> str:
    """kind: "batter" or "pitcher". min=1 includes every player/pitch type
    with at least one plate appearance (small samples are regressed later,
    not dropped)."""
    assert kind in ("batter", "pitcher"), kind
    return _get_text(f"{BASE_URL}/leaderboard/pitch-arsenal-stats", {
        "type": kind, "pitchType": "", "year": season, "team": "", "min": 1, "csv": "true",
    })


def get_pitcher_pitches(pitcher_id: int, season: int) -> str:
    """Every regular-season pitch he's thrown this season (one row each)."""
    return _get_text(f"{BASE_URL}/statcast_search/csv", {
        "all": "true", "hfGT": "R|", "hfSea": f"{season}|", "player_type": "pitcher",
        "pitchers_lookup[]": pitcher_id, "type": "details",
    })
