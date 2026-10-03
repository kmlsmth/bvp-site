"""
Thin wrapper around Open-Meteo (api.open-meteo.com) -- a free, no-API-key
weather service with worldwide coverage, including the one MLB ballpark
the U.S. National Weather Service can't reach: Rogers Centre in Toronto.

Switched here from the U.S. National Weather Service (which only covers
the U.S.) specifically to cover that one park. For the 30 U.S. ballparks,
Open-Meteo's default "best match" setting blends the same NOAA models
(GFS, HRRR, the NOAA Blend of Models) the old setup was already built on,
so this isn't a trade-down in accuracy -- it's the same underlying
government weather data, just from a single service that also happens to
cover Canada (via Environment Canada's own GEM model for Toronto).

One call per lookup -- no separate "which forecast grid covers this
point" step like NWS needed, since Open-Meteo takes coordinates directly.
We don't pass a timezone parameter, so timestamps come back in GMT/UTC
(Open-Meteo's default when none is given), matching how game times are
already stored.
"""
from __future__ import annotations

import requests

BASE_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 15

# WMO Weather interpretation codes, as used by Open-Meteo's weather_code
# field -- https://open-meteo.com/en/docs ("WMO Weather interpretation
# codes (WW)" table).
WMO_CODE_TO_TEXT = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snowfall", 73: "Moderate snowfall", 75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 97: "Heavy thunderstorm",
    99: "Thunderstorm with heavy hail",
}

# Open-Meteo gives wind direction in degrees, not a compass label -- this
# is the standard 16-point mapping back the other way, so the stored
# wind_dir_compass column (e.g. "ENE") still reads the same as it always
# has on the front end.
_COMPASS_POINTS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def degrees_to_compass(deg: float | None) -> str | None:
    if deg is None:
        return None
    return _COMPASS_POINTS[round(deg / 22.5) % 16]


def get_hourly_forecast(lat: float, lon: float) -> list[dict]:
    """Hour-by-hour forecast for one lat/long, several days out.

    Each returned dict has: time (UTC, no offset suffix), temp_f,
    wind_speed_mph, wind_dir_deg, wind_dir_compass, sky.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,weather_code",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
    }
    resp = requests.get(BASE_URL, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    hourly = resp.json().get("hourly", {})

    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    speeds = hourly.get("wind_speed_10m", [])
    dirs = hourly.get("wind_direction_10m", [])
    codes = hourly.get("weather_code", [])

    periods = []
    for i, t in enumerate(times):
        deg = dirs[i] if i < len(dirs) else None
        code = codes[i] if i < len(codes) else None
        periods.append({
            "time": t,
            "temp_f": temps[i] if i < len(temps) else None,
            "wind_speed_mph": speeds[i] if i < len(speeds) else None,
            "wind_dir_deg": deg,
            "wind_dir_compass": degrees_to_compass(deg),
            "sky": WMO_CODE_TO_TEXT.get(code) if code is not None else None,
        })
    return periods
