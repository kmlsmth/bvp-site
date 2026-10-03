"""
End-to-end integration test for ingest_date(), using canned API responses
(built from real shapes captured earlier) instead of live network calls --
this sandbox can't reach statsapi.mlb.com, but the orchestration logic
(ordering of upserts, foreign-key dependencies between games/players/teams)
can and should be exercised without the network.

This is exactly the kind of bug a per-function unit test misses: each
piece (parsing, a single upsert) worked fine in isolation, but the real
multi-step flow in ingest_date() had an ordering bug that only showed up
when everything ran together against realistic data.

Run: python3 scripts/test_ingest_integration.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Point the database at a throwaway temp dir BEFORE importing db/ingest_daily,
# since DATA_DIR is resolved at import time.
_tmpdir = tempfile.mkdtemp()
os.environ["BVP_DATA_DIR"] = _tmpdir

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import init_db  # noqa: E402
import ingest_daily  # noqa: E402
import weather_api  # noqa: E402 (patched below, but must be imported first)

ROOT = Path(__file__).resolve().parent.parent
init_db.init_db(db.DB_PATH)

# Real fixture: Aaron Judge vs Blake Snell. Its season rows include a 2024
# row where the opponent was the Giants (team 137) -- a team that is NOT
# one of today's two game teams below. That's exactly what exposed the
# second foreign-key bug (historical opponent teams never getting
# upserted), so keeping this fixture in the test is deliberate.
JUDGE_VS_SNELL = json.loads(
    (ROOT / "data" / "sample_vsplayer_judge_snell.json").read_text()
)

FAKE_SCHEDULE = {
    "dates": [{
        "games": [{
            "gamePk": 777001,
            "officialDate": "2026-09-25",
            "gameDate": "2026-09-25T23:10:00Z",
            "gameType": "R",
            "status": {"detailedState": "Final"},
            "venue": {"id": 3, "name": "Fenway Park"},
            "teams": {
                "home": {
                    "team": {"id": 147, "name": "New York Yankees"},
                    "probablePitcher": {"id": 605483, "fullName": "Blake Snell"},
                },
                "away": {
                    "team": {"id": 139, "name": "Tampa Bay Rays"},
                    "probablePitcher": {"id": 999111, "fullName": "Fake Opposing Pitcher"},
                },
            },
        }],
    }],
}

# Real response, captured live from statsapi.mlb.com/api/v1/venues/3
# (?hydrate=location,fieldInfo) -- same shape scripts/venues.py expects.
FAKE_VENUE_RAW = {
    "copyright": "x",
    "venues": [{
        "id": 3, "name": "Fenway Park", "link": "/api/v1/venues/3",
        "location": {
            "address1": "4 Yawkey Way", "city": "Boston", "state": "Massachusetts",
            "stateAbbrev": "MA", "postalCode": "02215",
            "defaultCoordinates": {"latitude": 42.346456, "longitude": -71.097441},
            "azimuthAngle": 45.0, "elevation": 21, "country": "USA",
        },
        "fieldInfo": {
            "capacity": 37755, "turfType": "Grass", "roofType": "Open",
            "leftLine": 310, "left": 379, "leftCenter": 390, "center": 420,
            "rightCenter": 380, "rightLine": 302,
        },
        "active": True, "season": "2026",
    }],
}

# Realistic shape of an NWS hourly forecast response (trimmed to one period).
FAKE_HOURLY_FORECAST = {
    "properties": {
        "periods": [{
            "startTime": "2026-09-25T19:00:00-04:00",
            "temperature": 68,
            "windSpeed": "9 mph",
            "windDirection": "SW",
            "shortForecast": "Partly Cloudy",
        }],
    },
}

# Roster for team 139 (away, opposing the home probable pitcher Snell) --
# just enough to exercise the batter-pairing loop.
FAKE_ROSTER_139 = {
    "roster": [
        {"person": {"id": 592450, "fullName": "Aaron Judge"},
         "position": {"code": "9", "type": "Outfielder"}},
        {"person": {"id": 605483, "fullName": "Blake Snell"},
         "position": {"code": "1", "type": "Pitcher"}},  # should be skipped (pitcher)
    ],
}

# Roster for team 147 (home, opposing the away probable pitcher) -- empty
# is fine, just checks the other branch doesn't blow up.
FAKE_ROSTER_147 = {"roster": []}


def fake_get_vs_player(batter_id: int, pitcher_id: int) -> dict:
    if batter_id == 592450 and pitcher_id == 605483:
        return JUDGE_VS_SNELL
    # everyone else: no recorded history, matching what the real API
    # returns for a pair that has never faced each other.
    return {"copyright": "x", "stats": []}


def fake_get_team_roster(team_id: int) -> dict:
    return {139: FAKE_ROSTER_139, 147: FAKE_ROSTER_147}[team_id]


def main() -> None:
    with mock.patch("mlb_api.get_schedule", return_value=FAKE_SCHEDULE), \
         mock.patch("mlb_api.get_team_roster", side_effect=fake_get_team_roster), \
         mock.patch("mlb_api.get_vs_player", side_effect=fake_get_vs_player), \
         mock.patch("mlb_api.get_venue", return_value=FAKE_VENUE_RAW), \
         mock.patch("weather_api.get_forecast_hourly_url", return_value="https://api.weather.gov/fake-grid/hourly"), \
         mock.patch("weather_api.get_hourly_forecast", return_value=FAKE_HOURLY_FORECAST["properties"]["periods"]), \
         mock.patch("ingest_daily.REQUEST_PAUSE_SECONDS", 0):
        games_count = ingest_daily.ingest_date("2026-09-25")

    assert games_count == 1, games_count
    print(f"ingest_date() completed without error, {games_count} game(s) processed")

    conn = db.connect()
    try:
        game_row = conn.execute(
            "SELECT home_team_id, away_team_id, home_probable_pitcher_id, away_probable_pitcher_id, venue_id "
            "FROM games WHERE game_pk = 777001"
        ).fetchone()
        assert game_row == (147, 139, 605483, 999111, 3), game_row
        print(f"games row OK: {game_row}")

        venue_row = conn.execute(
            "SELECT name, azimuth_angle, lat, lon, roof_type FROM venues WHERE id = 3"
        ).fetchone()
        assert venue_row == ("Fenway Park", 45.0, 42.346456, -71.097441, "Open"), venue_row
        print(f"venues row OK: {venue_row}")

        weather_row = conn.execute(
            "SELECT wind_speed_mph, wind_dir_deg, wind_dir_compass, temp_f, sky "
            "FROM game_weather WHERE game_pk = 777001"
        ).fetchone()
        assert weather_row == (9, 225.0, "SW", 68, "Partly Cloudy"), weather_row
        print(f"game_weather row OK: {weather_row}")

        career_row = conn.execute(
            "SELECT hits, avg FROM matchup_career WHERE batter_id=592450 AND pitcher_id=605483"
        ).fetchone()
        assert career_row == (2, ".111"), career_row
        print(f"matchup_career row OK: {career_row}")

        season_count = conn.execute(
            "SELECT COUNT(*) FROM matchup_season WHERE batter_id=592450 AND pitcher_id=605483"
        ).fetchone()[0]
        assert season_count == 6, season_count
        print(f"matchup_season rows OK: {season_count}")

        # The historical-team fix: team 137 (Giants) was an opponent in a
        # past season, never one of today's game teams, and must have
        # been upserted on the fly.
        giants = conn.execute("SELECT name FROM teams WHERE id = 137").fetchone()
        assert giants == ("San Francisco Giants",), giants
        print(f"historical opponent team (137) upserted OK: {giants}")

        run_row = conn.execute(
            "SELECT status, games_count FROM ingestion_runs WHERE run_date = '2026-09-25'"
        ).fetchone()
        assert run_row == ("done", 1), run_row
        print(f"ingestion_runs row OK: {run_row}")

        # Re-running the same date should skip (already done), not error
        # or duplicate rows -- confirms the idempotency guard still works
        # with the reordered code.
        second_count = ingest_daily.ingest_date("2026-09-25")
        assert second_count == 0, second_count
        print("re-running same date skips as expected (idempotent)")
    finally:
        conn.close()

    print("\nAll integration tests passed.")


if __name__ == "__main__":
    main()
