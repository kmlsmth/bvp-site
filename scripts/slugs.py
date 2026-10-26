"""
Clean, search-friendly page URLs:
  /player/austin-riley-663586   (name for readability and search, MLB id so
                                 it's unique and survives name changes --
                                 the same pattern MLB.com uses)
  /team/atlanta-braves
  /bvp/austin-riley-vs-yoshinobu-yamamoto-663586-808967   (batter vs pitcher)
/teams lists all 30. /matchup/ is reserved for team-vs-team pages; /game/ for games.

slugify() must match the front end's slugify() in web/index.html exactly.
"""
from __future__ import annotations

import re
import unicodedata

# The 30 MLB teams (ids and names as MLB's Stats API lists them for 2026).
TEAMS = {
    108: "Los Angeles Angels", 109: "Arizona Diamondbacks", 110: "Baltimore Orioles", 111: "Boston Red Sox",
    112: "Chicago Cubs", 113: "Cincinnati Reds", 114: "Cleveland Guardians", 115: "Colorado Rockies",
    116: "Detroit Tigers", 117: "Houston Astros", 118: "Kansas City Royals", 119: "Los Angeles Dodgers",
    120: "Washington Nationals", 121: "New York Mets", 133: "Athletics", 134: "Pittsburgh Pirates",
    135: "San Diego Padres", 136: "Seattle Mariners", 137: "San Francisco Giants", 138: "St. Louis Cardinals",
    139: "Tampa Bay Rays", 140: "Texas Rangers", 141: "Toronto Blue Jays", 142: "Minnesota Twins",
    143: "Philadelphia Phillies", 144: "Atlanta Braves", 145: "Chicago White Sox", 146: "Miami Marlins",
    147: "New York Yankees", 158: "Milwaukee Brewers",
}

# MLB's own abbreviations (Stats API team.abbreviation) and divisions.
ABBR = {
    108: "LAA", 109: "AZ", 110: "BAL", 111: "BOS", 112: "CHC", 113: "CIN", 114: "CLE", 115: "COL",
    116: "DET", 117: "HOU", 118: "KC", 119: "LAD", 120: "WSH", 121: "NYM", 133: "ATH", 134: "PIT",
    135: "SD", 136: "SEA", 137: "SF", 138: "STL", 139: "TB", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}
DIVISIONS = [   # (league, division, team ids)
    ("American League", "AL East", [110, 111, 147, 139, 141]),
    ("American League", "AL Central", [145, 114, 116, 118, 142]),
    ("American League", "AL West", [133, 117, 108, 136, 140]),
    ("National League", "NL East", [144, 146, 121, 143, 120]),
    ("National League", "NL Central", [112, 113, 158, 134, 138]),
    ("National League", "NL West", [109, 115, 119, 135, 137]),
]


def slugify(name: str | None) -> str:
    """'Ronald Acuña Jr.' -> 'ronald-acuna-jr'."""
    s = unicodedata.normalize("NFD", name or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower())
    return s.strip("-")


def player_slug(name: str | None, player_id: int) -> str:
    base = slugify(name)
    return f"{base}-{player_id}" if base else str(player_id)


def player_id_from_slug(slug: str) -> int | None:
    """'austin-riley-663586' or '663586' -> 663586."""
    m = re.search(r"(\d+)$", slug or "")
    return int(m.group(1)) if m else None


TEAM_SLUGS = {slugify(n): tid for tid, n in TEAMS.items()}


def team_slug(team_id: int) -> str | None:
    return slugify(TEAMS[team_id]) if team_id in TEAMS else None


def team_id_from_slug(slug: str) -> int | None:
    """'atlanta-braves' or '144' -> 144 (None for anything else)."""
    if not slug:
        return None
    if slug.isdigit():
        return int(slug) if int(slug) in TEAMS else None
    return TEAM_SLUGS.get(slug.lower())


def bvp_slug(batter_name: str | None, batter_id: int, pitcher_name: str | None, pitcher_id: int) -> str:
    """Batter first, then pitcher, then both MLB ids (unique even when two
    players share a name, e.g. the two Will Smiths):
    'austin-riley-vs-yoshinobu-yamamoto-663586-808967'."""
    names = "-vs-".join(x for x in (slugify(batter_name), slugify(pitcher_name)) if x)
    return f"{names}-{batter_id}-{pitcher_id}" if names else f"{batter_id}-{pitcher_id}"


def bvp_ids_from_slug(slug: str) -> tuple[int, int] | None:
    m = re.search(r"(\d+)-(\d+)$", slug or "")
    return (int(m.group(1)), int(m.group(2))) if m else None
