#!/usr/bin/env python3
"""Capture NWS + ECMWF (+ NBM, AIFS) forecasts for every weatherbot city.

Writes to its own table (`provider_forecasts`) so the live trading path
(`forecasts` table, picked by max(created_at) with no source filter) is
completely untouched. This is the live-capture leg of the provider
comparison; historical model data is backfillable any time via
provider_backtest.py (Open-Meteo Previous Runs archive). NWS point
forecasts are NOT archived anywhere public — capturing them here is the
only way to build an NWS comparison record.

Run via launchd every 6h (com.weatherbot.providercapture), or manually:
    python3 capture_provider_forecasts.py
"""

import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

import psycopg2

# Mirrors src/nws.ts LOCATIONS / NWS_ENDPOINTS (station-sited lat/lon).
CITIES = {
    "nyc":           {"lat": 40.7772, "lon": -73.8726,  "tz": "America/New_York",    "grid": "OKX/37,39"},
    "chicago":       {"lat": 41.9742, "lon": -87.9073,  "tz": "America/Chicago",     "grid": "LOT/66,77"},
    "miami":         {"lat": 25.7959, "lon": -80.287,   "tz": "America/New_York",    "grid": "MFL/106,51"},
    "dallas":        {"lat": 32.8471, "lon": -96.8518,  "tz": "America/Chicago",     "grid": "FWD/87,107"},
    "seattle":       {"lat": 47.4502, "lon": -122.3088, "tz": "America/Los_Angeles", "grid": "SEW/124,61"},
    "atlanta":       {"lat": 33.6407, "lon": -84.4277,  "tz": "America/New_York",    "grid": "FFC/50,82"},
    "houston":       {"lat": 29.6375, "lon": -95.2825,  "tz": "America/Chicago",     "grid": "HGX/66,89"},   # KHOU (Hobby) — PM resolution station
    "denver":        {"lat": 39.7133, "lon": -104.7581, "tz": "America/Denver",      "grid": "BOU/71,60"},   # KBKF (Buckley SFB) — PM resolution station
    "los-angeles":   {"lat": 34.0536, "lon": -118.2456, "tz": "America/Los_Angeles", "grid": "LOX/155,45"},
    "san-francisco": {"lat": 37.7749, "lon": -122.4194, "tz": "America/Los_Angeles", "grid": "MTR/85,105"},
    "austin":        {"lat": 30.2672, "lon": -97.7431,  "tz": "America/Chicago",     "grid": "EWX/156,91"},
}

OM_MODELS = [
    "ecmwf_ifs025", "ecmwf_aifs025_single", "ncep_nbm_conus",
    "ukmo_seamless", "gem_seamless", "icon_seamless",
]
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DSN = os.getenv("DATABASE_URL", "postgresql://padraighaughey@localhost:5432/gmgn_trading")

DDL = """
CREATE TABLE IF NOT EXISTS provider_forecasts (
    id           SERIAL PRIMARY KEY,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    city         TEXT NOT NULL,
    mode         TEXT NOT NULL,          -- 'max' | 'min'
    target_date  DATE NOT NULL,          -- local calendar day the temp is for
    provider     TEXT NOT NULL,          -- 'nws' | 'ecmwf_ifs025' | ...
    forecast_temp NUMERIC(5,1) NOT NULL  -- °F
);
CREATE INDEX IF NOT EXISTS ix_provider_forecasts_lookup
    ON provider_forecasts (city, mode, target_date, provider);
"""


def http_json(url, params=None, headers=None, retries=3):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_nws(city, cfg):
    """Daily max/min per target date from the NWS gridpoint 12h-period forecast."""
    url = f"https://api.weather.gov/gridpoints/{cfg['grid']}/forecast"
    data = http_json(url, headers={"User-Agent": "weatherbot-provider-capture (tremane1977@gmail.com)"})
    rows = []
    for p in data.get("properties", {}).get("periods", []):
        t = p.get("temperature")
        start = p.get("startTime", "")
        if t is None or len(start) < 10:
            continue
        mode = "max" if p.get("isDaytime") else "min"
        # NWS overnight periods start the evening before; the low belongs to
        # the following local calendar day (matches CLI report convention).
        d = datetime.date.fromisoformat(start[:10])
        if mode == "min" and int(start[11:13]) >= 12:
            d += datetime.timedelta(days=1)
        rows.append((city, mode, d, "nws", float(t)))
    return rows


def fetch_open_meteo(city, cfg):
    data = http_json(OPEN_METEO_URL, {
        "latitude": cfg["lat"], "longitude": cfg["lon"],
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit", "timezone": cfg["tz"],
        "models": ",".join(OM_MODELS), "forecast_days": 4,
    })
    daily = data.get("daily", {})
    times = daily.get("time", [])
    rows = []
    for model in OM_MODELS:
        for mode, field in (("max", "temperature_2m_max"), ("min", "temperature_2m_min")):
            key = f"{field}_{model}" if len(OM_MODELS) > 1 else field
            for d, v in zip(times, daily.get(key, [])):
                if v is not None:
                    rows.append((city, mode, datetime.date.fromisoformat(d), model, round(float(v), 1)))
    return rows


def main():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(DDL)

    total = 0
    for city, cfg in CITIES.items():
        for fetcher, label in ((fetch_nws, "nws"), (fetch_open_meteo, "open-meteo")):
            try:
                rows = fetcher(city, cfg)
            except Exception as e:
                print(f"⚠️  {city} {label}: {e}", file=sys.stderr)
                continue
            for r in rows:
                cur.execute(
                    "INSERT INTO provider_forecasts (city, mode, target_date, provider, forecast_temp) "
                    "VALUES (%s, %s, %s, %s, %s)", r)
            total += len(rows)
        time.sleep(0.5)

    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} "
          f"captured {total} provider forecast rows for {len(CITIES)} cities")
    conn.close()


if __name__ == "__main__":
    main()
