"""
Hand-seeds venues.hr_factor / venues.hit_factor -- see the comment on
those columns in data/schema.sql for why this is NOT part of daily
ingestion (park factors don't change game to game).

PROVENANCE / CONFIDENCE -- read before trusting these numbers:
  hr_factor below is FanGraphs' Guts! tool park-factor table (HR column,
  https://www.fangraphs.com/guts.aspx?type=pf), fetched by an automated
  web-fetch tool rather than typed in from memory or invented. hit_factor
  is a composite we computed ourselves from that same page's 1B/2B/3B
  columns, weighted 70/20/10 (singles/doubles/triples -- roughly their
  real-world share of all hits) to turn three split factors into the one
  "hits overall" number this schema's hit_factor column expects.

  We could not independently re-verify the exact scraped values against
  a second byte-for-byte source, and the fetch tool summarizes a
  dynamically-rendered page rather than parsing its raw table markup, so
  treat every number in PARK_FACTORS as a reasonable starting point, NOT
  a verified ground truth -- exactly like the box-score parser added
  alongside bullpen fatigue, this deserves a spot-check against the live
  FanGraphs page (a handful of teams is enough to sanity-check) before
  anyone relies on it for a real bet. Both factors use FanGraphs' scale:
  100 = league average, so a hr_factor of 110 means ~10% more home runs
  than a neutral park.

  Keyed by venue name, matching venues.name as MLB's own API reports it
  (not team name) -- a few parks have been renamed or relocated recently
  (Daikin Park/Astros, Rate Field/White Sox, Sutter Health Park/A's), so
  a couple of aliases are included for the names most likely to still be
  in flux. Only venues already in the table (i.e. a game has referenced
  them at least once) get updated; nothing is inserted standalone, since
  a bare row would be missing the lat/lon/azimuth fields the rest of the
  app expects.

Run: python3 scripts/seed_park_factors.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db

# name -> (hr_factor, hit_factor). See the module docstring for sourcing
# and the weighting used to build hit_factor from FanGraphs' 1B/2B/3B.
PARK_FACTORS: dict[str, tuple[float, float]] = {
    "Angel Stadium": (105, 99.5),
    "Oriole Park at Camden Yards": (99, 101.4),
    "Fenway Park": (98, 106.1),
    "Rate Field": (105, 97.9),
    "Guaranteed Rate Field": (105, 97.9),  # pre-2025 name, kept as an alias
    "Progressive Field": (98, 98.7),
    "Comerica Park": (96, 101.8),
    "Kauffman Stadium": (95, 106.0),
    "Target Field": (99, 101.0),
    "Yankee Stadium": (104, 95.4),
    "Sutter Health Park": (103, 102.4),
    "Oakland Coliseum": (103, 102.4),  # pre-2025 A's home, kept as an alias
    "T-Mobile Park": (96, 93.0),
    "Tropicana Field": (104, 100.1),
    "George M. Steinbrenner Field": (104, 100.1),  # Rays' temporary home, kept as an alias
    "Globe Life Field": (102, 97.9),
    "Rogers Centre": (103, 98.0),
    "Chase Field": (91, 105.1),
    "Truist Park": (99, 100.5),
    "Wrigley Field": (98, 99.7),
    "Great American Ball Park": (114, 99.3),
    "Coors Field": (107, 111.3),
    "loanDepot park": (97, 102.5),
    "Minute Maid Park": (102, 100.3),
    "Daikin Park": (102, 100.3),  # Astros' park, renamed in 2025
    "Dodger Stadium": (110, 95.2),
    "UNIQLO Field at Dodger Stadium": (110, 95.2),  # sponsorship rename, kept as an alias
    "American Family Field": (104, 97.1),
    "Nationals Park": (100, 99.4),
    "Citi Field": (99, 95.4),
    "Citizens Bank Park": (105, 98.9),
    "PNC Park": (93, 102.9),
    "Busch Stadium": (94, 99.4),
    "Petco Park": (101, 95.5),
    "Oracle Park": (91, 102.8),
}


def seed(conn) -> tuple[int, int]:
    """Fill in hr_factor/hit_factor for any already-known venue that
    doesn't have them yet. Returns (matched, skipped_already_set)."""
    matched = 0
    skipped = 0
    for name, (hr_factor, hit_factor) in PARK_FACTORS.items():
        row = conn.execute(
            "SELECT hr_factor FROM venues WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            continue  # venue not ingested yet -- nothing to update
        if row[0] is not None:
            skipped += 1
            continue
        conn.execute(
            "UPDATE venues SET hr_factor = ?, hit_factor = ? WHERE name = ?",
            (hr_factor, hit_factor, name),
        )
        matched += 1
    conn.commit()
    return matched, skipped


if __name__ == "__main__":
    conn = db.connect()
    try:
        matched, skipped = seed(conn)
        print(f"Park factors: set on {matched} venue(s), {skipped} already had values.")
    finally:
        conn.close()
