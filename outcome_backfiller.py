#!/usr/bin/env python3
"""
Official Observatory Outcome Backfiller
Uses Open-Meteo Archive API for all locations — free, no key, global coverage.
Replaces per-observatory government APIs (NWS UK bug, HK daily-max bug).
"""

import sys
import datetime
import calendar
import httpx
from database_schema import init_database, Outcome

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

NOAA_CDO_URL = "https://www.ncdc.noaa.gov/cdo-web/api/v2/data"

# GHCND station IDs for US cities — actual ASOS station readings,
# same source Polymarket uses to resolve US temperature markets.
US_GHCND_STATIONS = {
    'new_york': 'USW00094728',   # Central Park ASOS
    'atlanta':  'USW00013874',   # Hartsfield-Jackson Intl
    'dallas':   'USW00003927',   # Dallas/Fort Worth Intl
    'chicago':  'USW00094846',   # O'Hare Intl
    'miami':    'USW00012839',   # Miami Intl
    'seattle':  'USW00024233',   # Seattle-Tacoma Intl
    'austin':   'USW00013958',   # Austin-Bergstrom Intl
}

OBSERVATORIES = {
    'shanghai': {
        'lat': 31.1678, 'lon': 121.4369,
        'timezone': 'Asia/Shanghai', 'units': 'celsius',
        'source_name': 'Shanghai Meteorological Bureau (Xujiahui)',
    },
    'hong_kong': {
        'lat': 22.3027, 'lon': 114.1772,
        'timezone': 'Asia/Hong_Kong', 'units': 'celsius',
        'source_name': 'Hong Kong Observatory',
    },
    'qingdao': {
        'lat': 36.0667, 'lon': 120.3333,
        'timezone': 'Asia/Shanghai', 'units': 'celsius',
        'source_name': 'Qingdao Meteorological Observatory',
    },
    'beijing': {
        'lat': 39.9333, 'lon': 116.2833,
        'timezone': 'Asia/Shanghai', 'units': 'celsius',
        'source_name': 'Beijing Weather Station',
    },
    'london': {
        'lat': 51.4700, 'lon': -0.4543,
        'timezone': 'Europe/London', 'units': 'celsius',
        'source_name': 'Heathrow Airport',
    },
    'dallas': {
        'lat': 32.8998, 'lon': -97.0403,
        'timezone': 'America/Chicago', 'units': 'fahrenheit',
        'source_name': 'Dallas/Fort Worth International Airport',
    },
    'paris': {
        'lat': 48.8225, 'lon': 2.3372,
        'timezone': 'Europe/Paris', 'units': 'celsius',
        'source_name': 'Paris-Montsouris Observatory',
    },
    'taipei': {
        'lat': 25.0378, 'lon': 121.5644,
        'timezone': 'Asia/Taipei', 'units': 'celsius',
        'source_name': 'Taipei Weather Station (CWB)',
    },
    'new_york': {
        'lat': 40.7790, 'lon': -73.9692,
        'timezone': 'America/New_York', 'units': 'fahrenheit',
        'source_name': 'Central Park ASOS',
    },
    'lucknow': {
        'lat': 26.7606, 'lon': 80.8893,
        'timezone': 'Asia/Kolkata', 'units': 'celsius',
        'source_name': 'Lucknow Airport (Amausi)',
    },
    'toronto': {
        'lat': 43.6777, 'lon': -79.6248,
        'timezone': 'America/Toronto', 'units': 'celsius',
        'source_name': 'Toronto Pearson International Airport',
    },
    'atlanta': {
        'lat': 33.6407, 'lon': -84.4277,
        'timezone': 'America/New_York', 'units': 'fahrenheit',
        'source_name': 'Hartsfield-Jackson Atlanta International Airport',
    },
    'chicago': {
        'lat': 41.9802, 'lon': -87.9090,
        'timezone': 'America/Chicago', 'units': 'fahrenheit',
        'source_name': "O'Hare International Airport",
    },
    'miami': {
        'lat': 25.7959, 'lon': -80.2870,
        'timezone': 'America/New_York', 'units': 'fahrenheit',
        'source_name': 'Miami International Airport',
    },
    'seattle': {
        'lat': 47.4502, 'lon': -122.3088,
        'timezone': 'America/Los_Angeles', 'units': 'fahrenheit',
        'source_name': 'Seattle-Tacoma International Airport',
    },
    'buenos_aires': {
        'lat': -34.5819, 'lon': -58.4804,
        'timezone': 'America/Argentina/Buenos_Aires', 'units': 'celsius',
        'source_name': 'Buenos Aires Observatorio Central',
    },
    'singapore': {
        'lat': 1.3502, 'lon': 103.9894,
        'timezone': 'Asia/Singapore', 'units': 'celsius',
        'source_name': 'Singapore Changi Airport',
    },
    'moscow': {
        'lat': 55.7517, 'lon': 37.6178,
        'timezone': 'Europe/Moscow', 'units': 'celsius',
        'source_name': 'Moscow WMO Station',
    },
    'tokyo': {
        'lat': 35.6894, 'lon': 139.6917,
        'timezone': 'Asia/Tokyo', 'units': 'celsius',
        'source_name': 'Tokyo JMA Observatory',
    },
    'madrid': {
        'lat': 40.4080, 'lon': -3.6833,
        'timezone': 'Europe/Madrid', 'units': 'celsius',
        'source_name': 'Madrid Retiro Observatory',
    },
    'shenzhen': {
        'lat': 22.5493, 'lon': 114.1138,
        'timezone': 'Asia/Shanghai', 'units': 'celsius',
        'source_name': 'Shenzhen National Reference Climate Station',
    },
    'chongqing': {
        'lat': 29.5925, 'lon': 106.4691,
        'timezone': 'Asia/Shanghai', 'units': 'celsius',
        'source_name': 'Chongqing Shapingba Observatory',
    },
    'austin': {
        'lat': 30.1975, 'lon': -97.6664,
        'timezone': 'America/Chicago', 'units': 'fahrenheit',
        'source_name': 'Austin-Bergstrom International Airport',
    },
}


def _fetch_open_meteo_extremes(location_id: str, date: datetime.date) -> tuple[float | None, float | None]:
    """Open-Meteo Archive API at observatory coordinates. Used for all non-US cities."""
    obs = OBSERVATORIES[location_id]
    temp_unit = 'fahrenheit' if obs['units'] == 'fahrenheit' else 'celsius'

    params = {
        'latitude':         obs['lat'],
        'longitude':        obs['lon'],
        'start_date':       date.isoformat(),
        'end_date':         date.isoformat(),
        'daily':            'temperature_2m_max,temperature_2m_min',
        'timezone':         obs['timezone'],
        'temperature_unit': temp_unit,
    }

    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(OPEN_METEO_ARCHIVE, params=params)
            r.raise_for_status()
            data = r.json()

        daily = data.get('daily', {})
        max_vals = daily.get('temperature_2m_max', [None])
        min_vals = daily.get('temperature_2m_min', [None])
        max_t = float(max_vals[0]) if max_vals and max_vals[0] is not None else None
        min_t = float(min_vals[0]) if min_vals and min_vals[0] is not None else None
        return max_t, min_t

    except Exception as e:
        print(f"⚠️  Open-Meteo error for {location_id} on {date}: {e}")
        return None, None


def _fetch_cdo_extremes(station_id: str, date: datetime.date) -> tuple[float | None, float | None]:
    """
    NOAA CDO GHCND daily — actual ASOS station readings in tenths of °F → °F.
    Requires NOAA_CDO_TOKEN env var. Returns (max_f, min_f) or (None, None).
    """
    import os
    token = os.environ.get('NOAA_CDO_TOKEN')
    if not token:
        print("   ⚠️  NOAA_CDO_TOKEN not set — falling back to Open-Meteo")
        return None, None

    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(NOAA_CDO_URL, params={
                'datasetid':  'GHCND',
                'stationid':  f'GHCND:{station_id}',
                'startdate':  date.isoformat(),
                'enddate':    date.isoformat(),
                'datatypeid': 'TMAX,TMIN',
                'units':      'standard',
                'limit':      10,
            }, headers={'token': token})
            r.raise_for_status()
            results = r.json().get('results', [])
    except Exception as e:
        print(f"   ⚠️  NOAA CDO error for {station_id} on {date}: {e}")
        return None, None

    tmax = tmin = None
    for item in results:
        if item['datatype'] == 'TMAX':
            tmax = round(item['value'] / 10.0, 1)   # tenths of °F → °F
        elif item['datatype'] == 'TMIN':
            tmin = round(item['value'] / 10.0, 1)

    return tmax, tmin


def fetch_daily_extremes(location_id: str, date: datetime.date) -> tuple[float | None, float | None]:
    """
    Fetch daily max and min temperature for a location.
    US cities route to NOAA CDO (actual ASOS readings) with Open-Meteo fallback.
    All other cities use Open-Meteo Archive (ERA5 reanalysis).
    Returns (max_temp, min_temp) in the location's native units, or (None, None).
    """
    if location_id in US_GHCND_STATIONS:
        max_t, min_t = _fetch_cdo_extremes(US_GHCND_STATIONS[location_id], date)
        if max_t is not None:
            obs = OBSERVATORIES[location_id]
            units_label = '°F' if obs['units'] == 'fahrenheit' else '°C'
            print(f"   ✅ {location_id:12s} {date}  max={max_t:.1f}{units_label}  min={min_t:.1f}{units_label}  [NOAA CDO]")
            return max_t, min_t
        # CDO token missing or API error — fall through to Open-Meteo
        print(f"   ⚠️  {location_id}: CDO failed, falling back to Open-Meteo")

    max_t, min_t = _fetch_open_meteo_extremes(location_id, date)
    if max_t is not None:
        obs = OBSERVATORIES[location_id]
        units_label = '°F' if obs['units'] == 'fahrenheit' else '°C'
        print(f"   ✅ {location_id:12s} {date}  max={max_t:.1f}{units_label}  min={min_t:.1f}{units_label}  [Open-Meteo]")
    else:
        print(f"   ❌ No data — {location_id} {date}")
    return max_t, min_t


def backfill_outcomes(days_back: int = 7) -> None:
    """
    Pull actual daily temperature extremes from Open-Meteo and upsert into the
    outcomes table. Skips rows already marked verified with a non-null max temp.
    """
    session = init_database()
    backfilled = 0

    print(f"✅ Backfilling outcomes via Open-Meteo (last {days_back} days)")

    for location_id, obs in OBSERVATORIES.items():
        for days_ago in range(1, days_back + 1):
            target_date = datetime.date.today() - datetime.timedelta(days=days_ago)
            target_dt = datetime.datetime.combine(target_date, datetime.time.min)

            existing = (
                session.query(Outcome)
                .filter_by(location_id=location_id, date=target_dt)
                .first()
            )
            if existing and existing.verified and existing.actual_max_temp is not None:
                continue

            max_temp, min_temp = fetch_daily_extremes(location_id, target_date)
            if max_temp is None:
                print(f"   ❌ No data — {location_id} {target_date}")
                continue

            if existing:
                existing.actual_max_temp = max_temp
                existing.actual_min_temp = min_temp
                existing.verified = True
                existing.source = f'open-meteo ({obs["source_name"]})'
            else:
                session.add(Outcome(
                    location_id=location_id,
                    date=target_dt,
                    actual_max_temp=max_temp,
                    actual_min_temp=min_temp,
                    source=f'open-meteo ({obs["source_name"]})',
                    verified=True,
                ))

            backfilled += 1

    session.commit()
    session.close()
    print(f"\n✅ Backfill complete — {backfilled} records upserted")


def backfill_precip_outcomes(months_back: int = 3) -> None:
    """
    Pull monthly precipitation totals and upsert into precip_outcomes.
    Covers completed months only.
    Source routing matches market resolution where possible:
      - ACIS for Seattle/NYC
      - Met Office for London
      - ERA5 for Seoul/Hong Kong (marked low confidence)
    """
    from database_schema import PrecipOutcome
    from precip_forecast_pipeline import (
        PRECIP_LOCATIONS,
        MM_TO_INCHES,
        fetch_accumulated,
    )

    session = init_database()
    backfilled = 0

    today = datetime.date.today()
    print(f"✅ Backfilling monthly precipitation outcomes (last {months_back} months)")

    for location_id, obs in PRECIP_LOCATIONS.items():
        for m_back in range(1, months_back + 1):
            # Step back m_back months from current month
            year  = today.year
            month = today.month - m_back
            while month <= 0:
                month += 12
                year  -= 1

            month_start = datetime.date(year, month, 1)
            last_day    = calendar.monthrange(year, month)[1]
            month_end   = datetime.date(year, month, last_day)

            existing = (
                session.query(PrecipOutcome)
                .filter_by(location_id=location_id, settlement_month=month_start)
                .first()
            )
            if existing and existing.verified and existing.actual_precip is not None:
                continue

            try:
                total_mm = fetch_accumulated(obs, month_start, month_end)
            except Exception as e:
                print(f"   ❌ {location_id} {month_start}: {e}")
                continue

            factor = MM_TO_INCHES if obs['units'] == 'inches' else 1.0
            actual = round(total_mm * factor, 2)
            units  = obs['units']
            unit_label = '"' if units == 'inches' else 'mm'

            if existing:
                existing.actual_precip = actual
                existing.units         = units
                existing.verified      = obs['data_source'] != 'era5'
                if obs['data_source'] == 'era5':
                    existing.source = f'era5 ({obs["name"]})'
                elif obs['data_source'] == 'acis':
                    existing.source = f'acis ({obs["name"]})'
                else:
                    existing.source = f'metoffice ({obs["name"]})'
            else:
                if obs['data_source'] == 'era5':
                    src = f'era5 ({obs["name"]})'
                elif obs['data_source'] == 'acis':
                    src = f'acis ({obs["name"]})'
                else:
                    src = f'metoffice ({obs["name"]})'
                session.add(PrecipOutcome(
                    location_id      = location_id,
                    settlement_month = month_start,
                    actual_precip    = actual,
                    units            = units,
                    source           = src,
                    verified         = obs['data_source'] != 'era5',
                ))

            backfilled += 1
            print(f"   ✅ {location_id:12s} {month_start}  total={actual:.1f}{unit_label}")

    session.commit()
    session.close()
    print(f"\n✅ Precipitation backfill complete — {backfilled} records upserted")


def settle_temperature_pnl(session=None) -> None:
    """
    Update simulated_pnl for past YES sim trades where outcome is now known.
    Win:  actual_max_temp in bucket range → pnl = size/price - size
    Loss: otherwise → pnl = -size
    """
    import datetime as _dt
    from sqlalchemy import func
    from polymarket_dry_run import parse_temp_range
    from database_schema import TradeSimulation, Outcome, init_database

    if session is None:
        session = init_database()

    today = _dt.date.today()
    pending = (
        session.query(TradeSimulation)
        .filter(
            TradeSimulation.order_type == 'YES',
            TradeSimulation.simulated_pnl.is_(None),
            TradeSimulation.market_date < today,
        )
        .all()
    )

    if not pending:
        print("   PnL settler: no unsettled YES trades.")
        return

    settled = 0
    for trade in pending:
        outcome = (
            session.query(Outcome)
            .filter(
                Outcome.location_id == trade.location_id,
                func.date(Outcome.date) == trade.market_date,
                Outcome.verified == True,
            )
            .first()
        )
        if not outcome or outcome.actual_max_temp is None:
            continue

        lo, hi = parse_temp_range(trade.question_text or '')
        if lo is None and hi is None:
            continue

        if getattr(trade, 'mode', 'max') == 'min':
            if outcome.actual_min_temp is None:
                continue
            actual = float(outcome.actual_min_temp)
        else:
            actual = float(outcome.actual_max_temp)
        
        # Single-degree city markets ("be 27°C") resolve YES when the rounded
        # reading equals the bucket integer — not on strict equality with the
        # fractional actual. Range/open-ended buckets keep containment semantics.
        if lo is not None and hi is not None and lo == hi:
            won = round(actual) == lo
        else:
            lower_bound = lo if lo is not None else float('-inf')
            upper_bound = hi if hi is not None else float('inf')
            won = lower_bound <= actual <= upper_bound
        
        size  = float(trade.size)
        price = float(trade.price)
        trade.simulated_pnl = round(size / price - size, 2) if won else round(-size, 2)
        
        print(
            f"   💸 Settled YES: {trade.location_id} on {trade.market_date} | "
            f"Actual: {actual}° | Bucket: [{lo}, {hi}] | {'WIN 🏆' if won else 'LOSS ❌'} | PnL: ${trade.simulated_pnl}"
        )
        settled += 1

    session.commit()
    print(f"   PnL settler: settled {settled} trade(s).")


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == 'precip':
        months = int(_sys.argv[2]) if len(_sys.argv) > 2 else 3
        backfill_precip_outcomes(months)
    else:
        days = int(_sys.argv[1]) if len(_sys.argv) > 1 else 7
        backfill_outcomes(days)
