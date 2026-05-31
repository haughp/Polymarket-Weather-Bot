#!/usr/bin/env python3
"""
ECMWF IFS Forecast Pipeline for Polymarket Weather Arbitrage.

Single-source spec: one HTTP call per (city, mode) to Open-Meteo's daily
aggregate with models=ecmwf_ifs025. No GRIB2, no ensemble bands.
"""
# Observatory coordinates verified against official station metadata 2026-05-31.
# Moscow (WMO 27612) and Tokyo (WMO 47662) were patched; all others within 0.05°.
# See docs/superpowers/specs/2026-05-30-ecmwf-pipeline-rebuild-design.md

import datetime
import httpx
from zoneinfo import ZoneInfo
from database_schema import init_database, Forecast

OPEN_METEO_DAILY = "https://api.open-meteo.com/v1/forecast"

NWS_POINTS_URL = "https://api.weather.gov/points"
NWS_USER_AGENT = "polymarket-weather-bot/1.0 (contact: admin@example.com)"

# US cities served by NWS Point Forecast API — used instead of Open-Meteo ECMWF
# for better alignment with Polymarket's NWS-based resolution source.
NWS_FORECAST_US_CITIES = frozenset({'dallas', 'new_york', 'atlanta', 'austin'})

# IANA timezone per location — used to pick the right local-date index
# from the Open-Meteo daily series, and to share the same target_date
# logic as polymarket_dry_run.py (now_local + 1 day).
LOCATION_TIMEZONES = {
    'shanghai':     'Asia/Shanghai',
    'hong_kong':    'Asia/Hong_Kong',
    'qingdao':      'Asia/Shanghai',
    'beijing':      'Asia/Shanghai',
    'london':       'Europe/London',
    'dallas':       'America/Chicago',
    'paris':        'Europe/Paris',
    'taipei':       'Asia/Taipei',
    'new_york':     'America/New_York',
    'lucknow':      'Asia/Kolkata',
    'toronto':      'America/Toronto',
    'atlanta':      'America/New_York',
    'buenos_aires': 'America/Argentina/Buenos_Aires',
    'singapore':    'Asia/Singapore',
    'moscow':       'Europe/Moscow',
    'tokyo':        'Asia/Tokyo',
    'madrid':       'Europe/Madrid',
    'shenzhen':     'Asia/Shanghai',
    'chongqing':    'Asia/Shanghai',
    'austin':       'America/Chicago',
}

# Exact official observatory coordinates — top 20 Polymarket weather markets by volume
OBSERVATORIES = {
    'shanghai': {
        'name': 'Shanghai Meteorological Bureau (Xujiahui)',
        'lat': 31.1678,
        'lon': 121.4369,
        'units': 'celsius',
        'volume': 521327
    },
    'hong_kong': {
        'name': 'Hong Kong Observatory',
        'lat': 22.3027,
        'lon': 114.1772,
        'units': 'celsius',
        'volume': 503763
    },
    'qingdao': {
        'name': 'Qingdao Meteorological Observatory',
        'lat': 36.0667,
        'lon': 120.3333,
        'units': 'celsius',
        'volume': 244920
    },
    'beijing': {
        'name': 'Beijing Weather Station',
        'lat': 39.9333,
        'lon': 116.2833,
        'units': 'celsius',
        'volume': 173716
    },
    'london': {
        'name': 'Heathrow Airport',
        'lat': 51.4700,
        'lon': -0.4543,
        'units': 'celsius',
        'volume': 152494
    },
    'dallas': {
        'name': 'Dallas/Fort Worth International Airport',
        'lat': 32.8998,
        'lon': -97.0403,
        'units': 'fahrenheit',
        'volume': 124919
    },
    'paris': {
        'name': 'Paris-Montsouris Observatory',
        'lat': 48.8225,
        'lon': 2.3372,
        'units': 'celsius',
        'volume': 115804
    },
    'taipei': {
        'name': 'Taipei Weather Station (CWB)',
        'lat': 25.0378,
        'lon': 121.5644,
        'units': 'celsius',
        'volume': 109780
    },
    'new_york': {
        'name': 'Central Park ASOS',
        'lat': 40.7790,
        'lon': -73.9692,
        'units': 'fahrenheit',
        'volume': 106404
    },
    'lucknow': {
        'name': 'Lucknow Airport (Amausi)',
        'lat': 26.7606,
        'lon': 80.8893,
        'units': 'celsius',
        'volume': 98298
    },
    'toronto': {
        'name': 'Toronto Pearson International Airport',
        'lat': 43.6777,
        'lon': -79.6248,
        'units': 'celsius',
        'volume': 95461
    },
    'atlanta': {
        'name': 'Hartsfield-Jackson Atlanta International Airport',
        'lat': 33.6407,
        'lon': -84.4277,
        'units': 'fahrenheit',
        'volume': 89125
    },
    'buenos_aires': {
        'name': 'Buenos Aires Observatorio Central',
        'lat': -34.5819,
        'lon': -58.4804,
        'units': 'celsius',
        'volume': 84580
    },
    'singapore': {
        'name': 'Singapore Changi Airport',
        'lat': 1.3502,
        'lon': 103.9894,
        'units': 'celsius',
        'volume': 81021
    },
    'moscow': {
        'name': 'Moscow WMO Station',
        'lat': 55.8300,
        'lon': 37.6100,
        'units': 'celsius',
        'volume': 79755
    },
    'tokyo': {
        'name': 'Tokyo JMA Observatory',
        'lat': 35.6900,
        'lon': 139.7500,
        'units': 'celsius',
        'volume': 74820
    },
    'madrid': {
        'name': 'Madrid Retiro Observatory',
        'lat': 40.4080,
        'lon': -3.6833,
        'units': 'celsius',
        'volume': 72844
    },
    'shenzhen': {
        'name': 'Shenzhen National Reference Climate Station',
        'lat': 22.5493,
        'lon': 114.1138,
        'units': 'celsius',
        'volume': 70944
    },
    'chongqing': {
        'name': 'Chongqing Shapingba Observatory',
        'lat': 29.5925,
        'lon': 106.4691,
        'units': 'celsius',
        'volume': 70114
    },
    'austin': {
        'name': 'Austin-Bergstrom International Airport',
        'lat': 30.1975,
        'lon': -97.6664,
        'units': 'fahrenheit',
        'volume': 68735
    },
}


def target_date_for(location_id: str) -> datetime.date:
    """Tomorrow in the location's local timezone — matches polymarket_dry_run.py."""
    tz = ZoneInfo(LOCATION_TIMEZONES[location_id])
    now_local = datetime.datetime.now(datetime.timezone.utc).astimezone(tz)
    return (now_local + datetime.timedelta(days=1)).date()


def fetch_ecmwf_daily_and_peak(
    lat: float,
    lon: float,
    target_date: datetime.date,
    timezone: str,
    units: str,
    mode: str = 'max',
) -> tuple[float, datetime.datetime] | None:
    """
    Fetch Open-Meteo daily aggregate + hourly series (ECMWF IFS 0.25°) for one
    (lat, lon). Returns (forecast_value, peak_time_utc) where peak_time is the
    UTC instant of the daily extremum on target_date (selected from the hourly
    series). Returns None on failure.
    """
    temp_unit = 'fahrenheit' if units == 'fahrenheit' else 'celsius'
    daily_field = 'temperature_2m_max' if mode == 'max' else 'temperature_2m_min'

    days_ahead = (target_date - datetime.date.today()).days + 2

    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(OPEN_METEO_DAILY, params={
                'latitude':         lat,
                'longitude':        lon,
                'daily':            daily_field,
                'hourly':           'temperature_2m',
                'temperature_unit': temp_unit,
                'timezone':         timezone,
                'models':           'ecmwf_ifs025',
                'forecast_days':    min(max(days_ahead, 3), 14),
            })
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        print(f"   ⚠️  Open-Meteo error for ({lat},{lon}) {mode}: {e}")
        return None

    target_str = target_date.isoformat()

    daily = data.get('daily', {})
    daily_times = daily.get('time', [])
    daily_vals = daily.get(daily_field, [])
    try:
        d_idx = daily_times.index(target_str)
    except ValueError:
        print(f"   ⚠️  Target date {target_str} not in daily response for ({lat},{lon})")
        return None
    if d_idx >= len(daily_vals) or daily_vals[d_idx] is None:
        return None
    forecast_value = float(daily_vals[d_idx])

    hourly = data.get('hourly', {})
    h_times = hourly.get('time', [])
    h_vals  = hourly.get('temperature_2m', [])
    target_hours = [
        (t, v) for t, v in zip(h_times, h_vals)
        if t.startswith(target_str) and v is not None
    ]
    if not target_hours:
        print(f"   ⚠️  No hourly values for {target_str} at ({lat},{lon})")
        return None

    if mode == 'max':
        peak_local_iso, _ = max(target_hours, key=lambda x: x[1])
    else:
        peak_local_iso, _ = min(target_hours, key=lambda x: x[1])

    peak_local_naive = datetime.datetime.fromisoformat(peak_local_iso)
    peak_utc = peak_local_naive.replace(tzinfo=ZoneInfo(timezone)).astimezone(datetime.timezone.utc)

    return forecast_value, peak_utc.replace(tzinfo=None)


def fetch_nws_forecast(
    lat: float,
    lon: float,
    target_date: datetime.date,
    timezone: str,
    units: str,
    mode: str = 'max',
) -> tuple[float, datetime.datetime] | None:
    """
    NWS Point Forecast API for US cities. No API key required.
    Returns (forecast_temp, peak_time_utc) or None on failure.
    NWS always returns °F; converted to celsius if units='celsius'.
    """
    try:
        with httpx.Client(
            timeout=20,
            headers={'User-Agent': NWS_USER_AGENT},
        ) as client:
            r = client.get(f"{NWS_POINTS_URL}/{lat:.4f},{lon:.4f}")
            r.raise_for_status()
            forecast_url = r.json()['properties']['forecast']

            r2 = client.get(forecast_url)
            r2.raise_for_status()
            periods = r2.json()['properties']['periods']
    except Exception as e:
        print(f"   ⚠️  NWS error for ({lat},{lon}): {e}")
        return None

    target_str = target_date.isoformat()
    want_daytime = (mode == 'max')

    for period in periods:
        if period['startTime'][:10] == target_str and period['isDaytime'] == want_daytime:
            temp_f = float(period['temperature'])
            if units == 'celsius':
                forecast_temp = round((temp_f - 32.0) * 5.0 / 9.0, 1)
            else:
                forecast_temp = round(temp_f, 1)
            peak_utc = (
                datetime.datetime.fromisoformat(period['startTime'])
                .astimezone(datetime.timezone.utc)
                .replace(tzinfo=None)
            )
            return forecast_temp, peak_utc

    label = 'daytime' if want_daytime else 'nighttime'
    print(f"   ⚠️  NWS: no {label} period for {target_str} at ({lat},{lon})")
    return None


def extract_forecast(location_id: str, mode: str = 'max') -> dict | None:
    """Build a forecast row for a single (location_id, mode). Returns None on failure."""
    obs = OBSERVATORIES[location_id]
    target_date = target_date_for(location_id)

    if location_id in NWS_FORECAST_US_CITIES:
        result = fetch_nws_forecast(
            obs['lat'], obs['lon'], target_date,
            LOCATION_TIMEZONES[location_id], obs['units'], mode,
        )
    else:
        result = fetch_ecmwf_daily_and_peak(
            obs['lat'], obs['lon'], target_date,
            LOCATION_TIMEZONES[location_id], obs['units'], mode,
        )

    if result is None:
        return None
    temp, peak_utc = result

    return {
        'location_id':   location_id,
        'mode':          mode,
        'name':          obs['name'],
        'forecast_temp': round(temp, 1),
        'target_date':   target_date,
        'peak_time':     peak_utc,
        'units':         obs['units'],
        'confidence':    0.962,
    }


def main():
    print("=" * 70)
    print("ECMWF Forecast Pipeline for Polymarket Weather Arbitrage")
    print("=" * 70)
    print("Source: Open-Meteo daily aggregate (models=ecmwf_ifs025)")
    print("-" * 70)

    results = []
    skipped = []
    for loc_id in OBSERVATORIES.keys():
        for mode in ['max', 'min']:
            fc = extract_forecast(loc_id, mode)
            if fc is None:
                skipped.append((loc_id, mode))
                continue
            results.append(fc)

            units_label = '°F' if fc['units'] == 'fahrenheit' else '°C'
            tz = ZoneInfo(LOCATION_TIMEZONES[loc_id])
            peak_local = fc['peak_time'].replace(tzinfo=datetime.timezone.utc).astimezone(tz)
            print(f"\n📍 {fc['name']} ({mode.upper()}):")
            print(f"   Forecast: {fc['forecast_temp']} {units_label}  "
                  f"peak {peak_local.strftime('%Y-%m-%d %H:%M %Z')}  (target {fc['target_date']})")

    if skipped:
        print(f"\n⚠️  Skipped {len(skipped)} (city, mode) pairs with no daily aggregate: "
              f"{', '.join(f'{c}/{m}' for c, m in skipped)}")

    if not results:
        print("\n❌ No forecasts produced — aborting DB write")
        return

    print(f"\n✅ Forecast run produced {len(results)} rows")

    # Initialize database connection
    session = init_database(auto_migrate=True)

    # Bucket the run time to the hour for de-dup grouping
    ecmwf_run_time = datetime.datetime.now(datetime.timezone.utc).replace(
        tzinfo=None, minute=0, second=0, microsecond=0
    )

    for r in results:
        forecast = Forecast(
            location_id   = r['location_id'],
            mode          = r['mode'],
            location_name = r['name'],
            forecast_temp = r['forecast_temp'],
            lower_band    = None,
            upper_band    = None,
            horizon_hours = None,
            ecmwf_run     = ecmwf_run_time,
            units         = r['units'],
            confidence    = r['confidence'],
            peak_time     = r['peak_time'],
        )
        session.add(forecast)

    session.commit()
    session.close()

    print(f"📝 {len(results)} forecasts saved to PostgreSQL database")


if __name__ == "__main__":
    main()
