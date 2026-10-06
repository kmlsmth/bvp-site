"""
Pre-game refresh (ingest_daily.pregame_refresh): only games starting within
~3 hours get re-checked, weather is re-pulled once the stored forecast is
20+ minutes old, and a late-announced / swapped starter gets picked up
with his matchup history -- all with canned MLB/weather responses (reused
from test_ingest_integration.py) and a fixed "now", so it's deterministic.

Run: python3 scripts/test_pregame_refresh.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Importing the fixtures module also points the database at a throwaway
# temp dir (it sets BVP_DATA_DIR before importing db) and creates the schema.
import test_ingest_integration as fx  # noqa: E402
import db  # noqa: E402
import ingest_daily  # noqa: E402

FIRST_PITCH = "2026-10-04T23:10:00Z"  # FAKE_SCHEDULE_V1's game 849823


def at(hh_mm: str):
    h, m = hh_mm.split(":")
    return datetime(2026, 10, 4, int(h), int(m), tzinfo=timezone.utc)


def set_weather_fetched(when: str) -> None:
    conn = db.connect()
    conn.execute("UPDATE game_weather SET fetched_at = ? WHERE game_pk = 849823", (when,))
    conn.commit()
    conn.close()


def run(schedule, now):
    with mock.patch("mlb_api.get_schedule", return_value=schedule), \
         mock.patch("mlb_api.get_team_roster", side_effect=fx.fake_get_team_roster), \
         mock.patch("mlb_api.get_vs_player", side_effect=fx.fake_get_vs_player), \
         mock.patch("mlb_api.get_venue", return_value=fx.FAKE_VENUE_RAW), \
         mock.patch("weather_api.get_hourly_forecast", return_value=fx.FAKE_HOURLY_PERIODS) as wx, \
         mock.patch("ingest_daily.REQUEST_PAUSE_SECONDS", 0):
        counts = ingest_daily.pregame_refresh("2026-10-04", now_utc=now)
    return counts, wx.call_count


def main() -> None:
    v1, v2 = fx.FAKE_SCHEDULE_V1, fx.FAKE_SCHEDULE_V2

    # 4+ hours out: nothing touched (not even a weather call).
    counts, wx_calls = run(v1, at("19:00"))
    assert counts["games_checked"] == 0 and wx_calls == 0, counts
    print("too early (4h10m out): skipped OK")

    # 2h40m out: game stored, weather pulled for the first time.
    counts, wx_calls = run(v1, at("20:30"))
    assert counts == {"games_checked": 1, "weather_refreshed": 1, "new_pitchers": 1}, counts
    assert wx_calls == 1
    print(f"inside window: {counts}")

    # Forecast 10 min old -> not re-pulled; 21 min old -> re-pulled.
    set_weather_fetched("2026-10-04 20:30:00")
    counts, wx_calls = run(v1, at("20:40"))
    assert counts["weather_refreshed"] == 0 and wx_calls == 0, counts
    counts, wx_calls = run(v1, at("20:51"))
    assert counts["weather_refreshed"] == 1 and wx_calls == 1, counts
    print("weather re-pulled only once 20+ min old OK")

    # The away starter gets announced 40 min before first pitch: picked up
    # on the next pass, game row updated, his matchup pull runs.
    set_weather_fetched("2026-10-04 22:20:00")
    counts, _ = run(v2, at("22:30"))
    assert counts["new_pitchers"] == 1, counts
    conn = db.connect()
    away = conn.execute("SELECT away_probable_pitcher_id FROM games WHERE game_pk = 849823").fetchone()[0]
    conn.close()
    assert away == 999111, away
    print("late-announced starter picked up 40 min before first pitch OK")

    # Late scratch: a different pitcher replaces him -> treated as new.
    v3 = json.loads(json.dumps(v2))
    v3["dates"][0]["games"][0]["teams"]["away"]["probablePitcher"] = {"id": 888222, "fullName": "Fake Reliever"}
    counts, _ = run(v3, at("22:50"))
    assert counts["new_pitchers"] == 1, counts
    print("late scratch / replacement starter picked up OK")

    # Finished game: left alone.
    v4 = json.loads(json.dumps(v3))
    v4["dates"][0]["games"][0]["status"] = {"detailedState": "Final"}
    counts, wx_calls = run(v4, at("23:20"))
    assert counts["games_checked"] == 0 and wx_calls == 0, counts
    print("finished game skipped OK")

    print("\nAll pre-game refresh tests passed.")


if __name__ == "__main__":
    main()
