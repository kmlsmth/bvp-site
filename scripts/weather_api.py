"""
Thin wrapper around the National Weather Service API (api.weather.gov).

Free, no API key -- but NWS asks every caller to identify itself with a
real User-Agent string (not a browser-style one), so requests that look
anonymous don't get rate-limited or blocked. We identify the site itself,
not a person.

Two-step lookup, same as NWS's own docs describe:
  1. /points/{lat},{lon}   -> which forecast "grid" covers this location,
                              plus a ready-made URL for its hourly forecast.
  2. that hourly forecast URL -> a list of hour-by-hour periods, each with
                              wind speed/direction, temperature, and sky.
Step 1 only needs to happen once per ballpark (stadiums don't move), so
its result gets cached on the venue row -- see scripts/venues.py.
"""
from __future__ import annotations

import requests

BASE_URL = "https://api.weather.gov"
TIMEOUT = 15
HEADERS = {
    "User-Agent": "bvp-site.up.railway.app (batter-vs-pitcher matchup tool)",
    "Accept": "application/geo+json",
}


def get_forecast_hourly_url(lat: float, lon: float) -> str | None:
    """Resolve a lat/long into its NWS hourly-forecast URL.

    Returns None if NWS doesn't cover this location (it's a US-only
    service, but every MLB stadium is in the US, so this should always
    succeed in practice).
    """
    url = f"{BASE_URL}/points/{lat:.4f},{lon:.4f}"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("properties", {}).get("forecastHourly")


def get_hourly_forecast(forecast_hourly_url: str) -> list[dict]:
    """The hour-by-hour forecast periods for one grid point."""
    resp = requests.get(forecast_hourly_url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("properties", {}).get("periods", [])


# NWS gives wind direction as a 16-point compass label (e.g. "ENE"), not
# degrees -- this is the standard mapping back to degrees.
COMPASS_TO_DEGREES = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5,
    "SE": 135, "SSE": 157.5, "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}


def parse_wind_speed(raw: str | None) -> int | None:
    """NWS gives wind speed as a string like '10 mph' (sometimes a range
    like '10 to 15 mph' -- we take the first number)."""
    if not raw:
        return None
    digits = "".join(c if c.isdigit() else " " for c in raw).split()
    return int(digits[0]) if digits else None
