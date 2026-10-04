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
import ingest_appearances  # noqa: E402 (patched below, but must be imported first)
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

# Shape returned by weather_api.get_hourly_forecast() (one period; the real
# function normalizes Open-Meteo's raw response into exactly this shape).
FAKE_HOURLY_PERIODS = [{
    "time": "2026-09-25T23:00",
    "temp_f": 68,
    "wind_speed_mph": 9,
    "wind_dir_deg": 225.0,
    "wind_dir_compass": "SW",
    "sky": "Partly cloudy",
}]

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

# Both teams' trailing-lookback schedule-range call returns the same prior
# game (147 @ 139 is a division matchup here -- they played each other the
# day before too), so one box score fixture covers both teams' appearances
# ingestion in one go.
FAKE_SCHEDULE_RANGE = {
    "dates": [{"games": [{
        "gamePk": 777000,
        "officialDate": "2026-09-24",
        "gameDate": "2026-09-24T23:00:00Z",
        "gameType": "R",
        "status": {"detailedState": "Final"},
        "venue": {"id": 3, "name": "Fenway Park"},
        "teams": {
            "home": {"team": {"id": 147, "name": "New York Yankees"}},
            "away": {"team": {"id": 139, "name": "Tampa Bay Rays"}},
        },
    }]}],
}

FAKE_BOXSCORE_777000 = {
    "teams": {
        "home": {
            "team": {"id": 147},
            "pitchers": [605483],
            "players": {
                "ID605483": {
                    "person": {"id": 605483, "fullName": "Blake Snell"},
                    "stats": {"pitching": {
                        "gamesStarted": 1, "inningsPitched": "6.0",
                        "numberOfPitches": 94, "battersFaced": 23,
                        "earnedRuns": 2, "baseOnBalls": 1, "strikeOuts": 7, "hits": 4,
                    }},
                },
            },
        },
        "away": {
            "team": {"id": 139},
            "pitchers": [999111, 888222],
            "players": {
                "ID999111": {
                    "person": {"id": 999111, "fullName": "Fake Opposing Pitcher"},
                    "stats": {"pitching": {
                        "gamesStarted": 1, "inningsPitched": "5.0",
                        "numberOfPitches": 88, "battersFaced": 22,
                        "earnedRuns": 3, "baseOnBalls": 2, "strikeOuts": 5, "hits": 5,
                    }},
                },
                "ID888222": {
                    "person": {"id": 888222, "fullName": "Fake Reliever"},
                    "stats": {"pitching": {
                        "gamesStarted": 0, "inningsPitched": "1.0",
                        "numberOfPitches": 15, "battersFaced": 4,
                        "earnedRuns": 0, "baseOnBalls": 0, "strikeOuts": 1, "hits": 1,
                    }},
                },
            },
        },
    },
}


def fake_get_team_schedule_range(team_id: int, start_date: str, end_date: str) -> dict:
    return FAKE_SCHEDULE_RANGE  # same prior game for both teams in this fixture


def fake_get_boxscore(game_pk: int) -> dict:
    assert game_pk == 777000, game_pk
    return FAKE_BOXSCORE_777000


def fake_get_vs_player(batter_id: int, pitcher_id: int) -> dict:
    if batter_id == 592450 and pitcher_id == 605483:
        return JUDGE_VS_SNELL
    # everyone else: no recorded history, matching what the real API
    # returns for a pair that has never faced each other.
    return {"copyright": "x", "stats": []}


def fake_get_team_roster(team_id: int) -> dict:
    return {139: FAKE_ROSTER_139, 147: FAKE_ROSTER_147}[team_id]


# --- Fixtures for refresh_probable_pitchers(): a game where the away
# team's starter isn't announced yet on the first schedule pull (V1), then
# shows up on a later pull the same day (V2) -- exactly the "Dylan Dodd
# announced as an opener hours before first pitch" scenario this feature
# exists to catch. Reuses pitcher/roster fixtures from above so this
# doesn't need its own full set.
FAKE_SCHEDULE_V1 = {
    "dates": [{"games": [{
        "gamePk": 849823,
        "officialDate": "2026-10-04",
        "gameDate": "2026-10-04T23:10:00Z",
        "gameType": "D",
        "status": {"detailedState": "Scheduled"},
        "venue": {"id": 3, "name": "Fenway Park"},
        "teams": {
            "home": {"team": {"id": 147, "name": "New York Yankees"},
                      "probablePitcher": {"id": 605483, "fullName": "Blake Snell"}},
            "away": {"team": {"id": 139, "name": "Tampa Bay Rays"}},  # no starter yet
        },
    }]}],
}
FAKE_SCHEDULE_V2 = json.loads(json.dumps(FAKE_SCHEDULE_V1))
FAKE_SCHEDULE_V2["dates"][0]["games"][0]["teams"]["away"]["probablePitcher"] = {
    "id": 999111, "fullName": "Fake Opposing Pitcher",
}


def test_refresh_probable_pitchers() -> None:
    schedule_calls = {"n": 0}

    def fake_get_schedule_versioned(date_str):
        schedule_calls["n"] += 1
        return FAKE_SCHEDULE_V1 if schedule_calls["n"] == 1 else FAKE_SCHEDULE_V2

    with mock.patch("mlb_api.get_schedule", side_effect=fake_get_schedule_versioned), \
         mock.patch("mlb_api.get_team_roster", side_effect=fake_get_team_roster), \
         mock.patch("mlb_api.get_vs_player", side_effect=fake_get_vs_player), \
         mock.patch("mlb_api.get_venue", return_value=FAKE_VENUE_RAW), \
         mock.patch("mlb_api.get_team_schedule_range", side_effect=fake_get_team_schedule_range), \
         mock.patch("mlb_api.get_boxscore", side_effect=fake_get_boxscore), \
         mock.patch("weather_api.get_hourly_forecast", return_value=FAKE_HOURLY_PERIODS), \
         mock.patch("ingest_daily.REQUEST_PAUSE_SECONDS", 0), \
         mock.patch("ingest_appearances.REQUEST_PAUSE_SECONDS", 0):

        count = ingest_daily.ingest_date("2026-10-04")
        assert count == 1, count

        conn = db.connect()
        row = conn.execute(
            "SELECT home_probable_pitcher_id, away_probable_pitcher_id "
            "FROM games WHERE game_pk = 849823"
        ).fetchone()
        assert row == (605483, None), row
        conn.close()

        # The bug this guards against: the full ingest only runs once per
        # date, so without a separate lightweight pass, an away starter
        # announced later today would never get picked up until tomorrow.
        second = ingest_daily.ingest_date("2026-10-04")
        assert second == 0, second

        found = ingest_daily.refresh_probable_pitchers("2026-10-04")
        assert found == 1, found

        conn = db.connect()
        row = conn.execute(
            "SELECT home_probable_pitcher_id, away_probable_pitcher_id "
            "FROM games WHERE game_pk = 849823"
        ).fetchone()
        assert row == (605483, 999111), row
        print(f"refresh_probable_pitchers picked up the newly announced starter: {row}")
        conn.close()

        # Nothing changed since the last refresh -- should find nothing new.
        again = ingest_daily.refresh_probable_pitchers("2026-10-04")
        assert again == 0, again
        print("refresh_probable_pitchers is a no-op once nothing has changed")


def main() -> None:
    with mock.patch("mlb_api.get_schedule", return_value=FAKE_SCHEDULE), \
         mock.patch("mlb_api.get_team_roster", side_effect=fake_get_team_roster), \
         mock.patch("mlb_api.get_vs_player", side_effect=fake_get_vs_player), \
         mock.patch("mlb_api.get_venue", return_value=FAKE_VENUE_RAW), \
         mock.patch("mlb_api.get_team_schedule_range", side_effect=fake_get_team_schedule_range), \
         mock.patch("mlb_api.get_boxscore", side_effect=fake_get_boxscore), \
         mock.patch("weather_api.get_hourly_forecast", return_value=FAKE_HOURLY_PERIODS), \
         mock.patch("ingest_daily.REQUEST_PAUSE_SECONDS", 0), \
         mock.patch("ingest_appearances.REQUEST_PAUSE_SECONDS", 0):
        games_count = ingest_daily.ingest_date("2026-09-25")
        # Calling the backfill again directly, still inside the mocked
        # network, as every future day's ingestion run will for as long as
        # 777000 stays inside the trailing lookback window -- must not
        # re-fetch the box score or duplicate the row (tests the
        # appearance_exists() skip, not a network failure).
        again = ingest_appearances.backfill_team_appearances(db.connect(), 147, "2026-09-25")
        assert again == 0, "already-stored game should not be re-fetched"

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
        assert weather_row == (9, 225.0, "SW", 68, "Partly cloudy"), weather_row
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

        # Trailing-window box score backfill, triggered for both teams
        # while processing today's game: one Final game (777000) the day
        # before, with one starter + one reliever on the away side.
        appearance_rows = conn.execute(
            "SELECT pitcher_id, team_id, role, outs, pitches "
            "FROM pitcher_appearances ORDER BY pitcher_id"
        ).fetchall()
        assert appearance_rows == [
            (605483, 147, "starter", 18, 94),
            (888222, 139, "reliever", 3, 15),
            (999111, 139, "starter", 15, 88),
        ], appearance_rows
        print(f"pitcher_appearances rows OK: {appearance_rows}")

        count_after = conn.execute("SELECT COUNT(*) FROM pitcher_appearances").fetchone()[0]
        assert count_after == 3, count_after
        print("re-running appearances backfill is idempotent (no duplicate rows)")
    finally:
        conn.close()

    test_refresh_probable_pitchers()

    print("\nAll integration tests passed.")


if __name__ == "__main__":
    main()
