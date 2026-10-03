"""
Keeps the venues table filled in, and pulls a wind/weather snapshot for a
game's ballpark and start time.

Venue details (location, orientation, field dimensions) are looked up
once per park and kept forever -- stadiums don't move. Weather is
re-fetched every ingestion run, since a forecast changes.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db
import mlb_api
import parsing
import weather_api


def ensure_venue(conn, venue_id: int) -> None:
    """Fetch and store this ballpark's details, if we don't have them yet."""
    if venue_id is None:
        return
    row = conn.execute("SELECT id FROM venues WHERE id = ?", (venue_id,)).fetchone()
    if row is not None:
        return
    try:
        raw = mlb_api.get_venue(venue_id)
    except Exception as exc:
        print(f"    venue {venue_id}: couldn't fetch details: {exc}")
        return
    venue = parsing.parse_venue(raw)
    if venue is None or venue.get("id") is None:
        return
    db.upsert_venue(conn, venue)
    conn.commit()


def refresh_weather(conn, game: dict) -> None:
    """Pull a wind/temperature snapshot for one game's ballpark and time."""
    venue_id = game.get("venue_id")
    if venue_id is None:
        return

    venue_row = conn.execute(
        "SELECT lat, lon, nws_forecast_hourly_url FROM venues WHERE id = ?",
        (venue_id,),
    ).fetchone()
    if venue_row is None or venue_row[0] is None or venue_row[1] is None:
        return  # no coordinates on file (e.g. azimuth/location wasn't published) -- nothing to look up
    lat, lon, forecast_url = venue_row

    if not forecast_url:
        try:
            forecast_url = weather_api.get_forecast_hourly_url(lat, lon)
        except Exception as exc:
            print(f"    weather: couldn't resolve forecast grid for venue {venue_id}: {exc}")
            return
        if not forecast_url:
            return
        conn.execute(
            "UPDATE venues SET nws_forecast_hourly_url = ? WHERE id = ?",
            (forecast_url, venue_id),
        )
        conn.commit()

    try:
        periods = weather_api.get_hourly_forecast(forecast_url)
    except Exception as exc:
        print(f"    weather: couldn't fetch forecast for venue {venue_id}: {exc}")
        return

    period = _closest_period(periods, game.get("game_date_time"))
    if period is None:
        return

    compass = period.get("windDirection")
    db.upsert_game_weather(conn, {
        "game_pk": game["game_pk"],
        "wind_speed_mph": weather_api.parse_wind_speed(period.get("windSpeed")),
        "wind_dir_deg": weather_api.COMPASS_TO_DEGREES.get(compass),
        "wind_dir_compass": compass,
        "temp_f": period.get("temperature"),
        "sky": period.get("shortForecast"),
        "forecast_time": period.get("startTime"),
    })
    conn.commit()


def _closest_period(periods: list[dict], target_iso: str | None) -> dict | None:
    if not periods:
        return None
    if not target_iso:
        return periods[0]
    try:
        target = datetime.fromisoformat(target_iso.replace("Z", "+00:00"))
    except ValueError:
        return periods[0]

    def _diff(p: dict) -> float:
        try:
            start = datetime.fromisoformat(p["startTime"])
        except (KeyError, ValueError):
            return float("inf")
        return abs((start - target).total_seconds())

    return min(periods, key=_diff)
