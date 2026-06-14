#!/usr/bin/env python3
"""
Official Observatory Outcome Backfiller
Uses Open-Meteo Archive API for all locations — free, no key, global coverage.
Replaces per-observatory government APIs (NWS UK bug, HK daily-max bug).
"""

import sys
import datetime
import calendar
import os
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

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# IEM ASOS airport stations — the ICAO code each Polymarket market names as its
# resolution station (read from the market description / wunderground resolutionSource
# URL, not guessed). Daily extremes are derived from hourly METAR temps grouped by the
# local calendar date, which matches Weather Underground's settlement method.
#
# Validated 2026-06-08 over 30 days: iem-in-Polymarket-band 89-100% for 19/20 cities
# vs ERA5 7-68%. See validate_outcome_source.py.
#
# EXCEPTIONS (do NOT trust IEM here — see HKO/SHENZHEN notes below):
#   hong_kong — Polymarket resolves on the Hong Kong Observatory "Absolute Daily Max",
#               not an airport METAR. VHHH only reaches ~43% band agreement.
#   shenzhen  — correct station (ZGSZ) but only ~11% agreement (ERA5 also ~29%);
#               anomalous, needs deeper review before trusting either source.
IEM_STATIONS = {
    'new_york':     {'station': 'KLGA', 'tz': 'America/New_York',               'units': 'fahrenheit'},  # LaGuardia
    'atlanta':      {'station': 'KATL', 'tz': 'America/New_York',               'units': 'fahrenheit'},
    'dallas':       {'station': 'KDAL', 'tz': 'America/Chicago',                'units': 'fahrenheit'},  # Love Field
    'chicago':      {'station': 'KORD', 'tz': 'America/Chicago',                'units': 'fahrenheit'},
    'miami':        {'station': 'KMIA', 'tz': 'America/New_York',               'units': 'fahrenheit'},
    'seattle':      {'station': 'KSEA', 'tz': 'America/Los_Angeles',            'units': 'fahrenheit'},
    'austin':       {'station': 'KAUS', 'tz': 'America/Chicago',                'units': 'fahrenheit'},
    'london':       {'station': 'EGLC', 'tz': 'Europe/London',                  'units': 'celsius'},     # London City
    'paris':        {'station': 'LFPB', 'tz': 'Europe/Paris',                   'units': 'celsius'},     # Le Bourget
    'madrid':       {'station': 'LEMD', 'tz': 'Europe/Madrid',                  'units': 'celsius'},     # Barajas
    'moscow':       {'station': 'UUWW', 'tz': 'Europe/Moscow',                  'units': 'celsius'},     # Vnukovo
    'beijing':      {'station': 'ZBAA', 'tz': 'Asia/Shanghai',                  'units': 'celsius'},     # Capital
    'shanghai':     {'station': 'ZSPD', 'tz': 'Asia/Shanghai',                  'units': 'celsius'},     # Pudong
    'singapore':    {'station': 'WSSS', 'tz': 'Asia/Singapore',                 'units': 'celsius'},     # Changi
    'tokyo':        {'station': 'RJTT', 'tz': 'Asia/Tokyo',                     'units': 'celsius'},     # Haneda
    'taipei':       {'station': 'RCSS', 'tz': 'Asia/Taipei',                    'units': 'celsius'},     # Songshan
    'chongqing':    {'station': 'ZUCK', 'tz': 'Asia/Shanghai',                  'units': 'celsius'},     # Jiangbei
    'qingdao':      {'station': 'ZSQD', 'tz': 'Asia/Shanghai',                  'units': 'celsius'},     # Jiaodong
    'buenos_aires': {'station': 'SAEZ', 'tz': 'America/Argentina/Buenos_Aires', 'units': 'celsius'},     # Ezeiza
    'lucknow':      {'station': 'VILK', 'tz': 'Asia/Kolkata',                   'units': 'celsius'},     # CCS
    'toronto':      {'station': 'CYYZ', 'tz': 'America/Toronto',                'units': 'celsius'},     # Pearson
}

# Cities whose Polymarket market does NOT resolve on an airport METAR, so IEM must
# not be used as ground truth. Routed to the ERA5 fallback until a proper source
# (e.g. HKO daily extract) is wired in.
IEM_EXCLUDED = {'hong_kong', 'shenzhen'}

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
        'lat': 55.8300, 'lon': 37.6100,
        'timezone': 'Europe/Moscow', 'units': 'celsius',
        'source_name': 'Moscow WMO Station',
    },
    'tokyo': {
        'lat': 35.6900, 'lon': 139.7500,
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


def _fetch_iem_extremes(location_id: str, date: datetime.date) -> tuple[float | None, float | None]:
    """
    IEM ASOS daily extremes from hourly METAR temps grouped by the LOCAL calendar date.
    Matches Weather Underground's settlement method (hourly METARs + SPECIs → daily high/low).
    Returns (max, min) in the location's native units (°F if fahrenheit, else °C),
    or (None, None) if the city has no IEM station or no data was returned.
    """
    meta = IEM_STATIONS.get(location_id)
    if not meta:
        return None, None
    data_field = 'tmpf' if meta['units'] == 'fahrenheit' else 'tmpc'
    params = {
        'station':  meta['station'],
        'data':     data_field,
        'year1':    date.year,  'month1': date.month, 'day1': date.day,
        'year2':    date.year,  'month2': date.month, 'day2': date.day,
        'tz':       meta['tz'],
        'format':   'onlycomma',
        'latlon':   'no',
        'direct':   'yes',
    }
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(IEM_URL, params=params)
            r.raise_for_status()
            body = r.text
    except Exception as e:
        print(f"   ⚠️  IEM error for {meta['station']} on {date}: {e}")
        return None, None

    temps: list[float] = []
    for line in body.splitlines():
        parts = line.strip().split(',')
        if len(parts) < 3:
            continue
        try:
            temps.append(float(parts[2].strip()))
        except ValueError:
            continue   # header row or 'M' (missing)

    if not temps:
        return None, None
    return round(max(temps), 1), round(min(temps), 1)


def fetch_daily_extremes(location_id: str, date: datetime.date) -> tuple[float | None, float | None]:
    """
    Fetch daily max and min temperature for a location, in the location's native units.

    Source cascade (all cities resolve on Weather Underground airport METARs, so IEM is
    the universal first choice):
      1. IEM ASOS hourly METAR  — = Weather Underground's method; the settlement source.
      2. NOAA CDO GHCND          — US-only cross-check / fallback if IEM is empty.
      3. Open-Meteo Archive ERA5 — last-resort fallback (and the only path for
                                    IEM_EXCLUDED cities like hong_kong/shenzhen).
    Returns (max_temp, min_temp) or (None, None).
    """
    obs = OBSERVATORIES[location_id]
    units_label = '°F' if obs['units'] == 'fahrenheit' else '°C'

    if location_id in IEM_STATIONS and location_id not in IEM_EXCLUDED:
        max_t, min_t = _fetch_iem_extremes(location_id, date)
        if max_t is not None:
            print(f"   ✅ {location_id:12s} {date}  max={max_t:.1f}{units_label}  min={min_t:.1f}{units_label}  [IEM ASOS]")
            return max_t, min_t
        # IEM empty (latency / missing day) — fall through.

    if location_id in US_GHCND_STATIONS:
        max_t, min_t = _fetch_cdo_extremes(US_GHCND_STATIONS[location_id], date)
        if max_t is not None:
            print(f"   ✅ {location_id:12s} {date}  max={max_t:.1f}{units_label}  min={min_t:.1f}{units_label}  [NOAA CDO]")
            return max_t, min_t
        # CDO token missing or API error — fall through to Open-Meteo

    max_t, min_t = _fetch_open_meteo_extremes(location_id, date)
    if max_t is not None:
        print(f"   ✅ {location_id:12s} {date}  max={max_t:.1f}{units_label}  min={min_t:.1f}{units_label}  [Open-Meteo]")
    else:
        print(f"   ❌ No data — {location_id} {date}")
    return max_t, min_t


def _source_label(location_id: str) -> str:
    """How fetch_daily_extremes will source this city, as a row-`source` prefix.
    IEM cities (non-excluded) → 'iem'; everything else → 'open-meteo'. The actual
    fetch may still fall back, but this labels the intended primary source so the
    DB row is self-documenting and re-migrations are idempotent."""
    if location_id in IEM_STATIONS and location_id not in IEM_EXCLUDED:
        return 'iem'
    return 'open-meteo'


def backfill_outcomes(days_back: int = 7, overwrite_sources: set[str] | None = None) -> None:
    """
    Pull actual daily temperature extremes via fetch_daily_extremes (IEM → CDO →
    Open-Meteo cascade) and upsert into the outcomes table.

    By default skips rows already marked verified with a non-null max temp.
    Pass `overwrite_sources` (e.g. {'open-meteo','era5'}) to ALSO re-fetch verified
    rows whose existing `source` starts with one of those prefixes — used to migrate
    legacy ERA5 rows onto the airport-METAR (IEM) source. Idempotent: once a row's
    source becomes 'iem (...)' it won't be re-fetched on a subsequent migrate run.
    """
    session = init_database()
    backfilled = 0

    mode = f"migrate(overwrite={sorted(overwrite_sources)})" if overwrite_sources else "skip-verified"
    print(f"✅ Backfilling outcomes (last {days_back} days) — {mode}")

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
                stale = bool(
                    overwrite_sources
                    and existing.source
                    and any(existing.source.startswith(s) for s in overwrite_sources)
                )
                if not stale:
                    continue

            max_temp, min_temp = fetch_daily_extremes(location_id, target_date)
            if max_temp is None:
                print(f"   ❌ No data — {location_id} {target_date}")
                continue

            src_label = f'{_source_label(location_id)} ({obs["source_name"]})'
            if existing:
                existing.actual_max_temp = max_temp
                existing.actual_min_temp = min_temp
                existing.verified = True
                existing.source = src_label
            else:
                session.add(Outcome(
                    location_id=location_id,
                    date=target_dt,
                    actual_max_temp=max_temp,
                    actual_min_temp=min_temp,
                    source=src_label,
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
    from polymarket_dry_run import parse_temp_range, bucket_contains
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

        lo, hi, width = parse_temp_range(trade.question_text or '')
        if lo is None and hi is None:
            continue

        if getattr(trade, 'mode', 'max') == 'min':
            if outcome.actual_min_temp is None:
                continue
            actual = float(outcome.actual_min_temp)
        else:
            actual = float(outcome.actual_max_temp)

        # Half-open [lo, lo+width) for finite buckets; inclusive bound for tails.
        # "62-63°F" -> [62,64) wins on 62/63 not 64; "be 13°C" -> [13,14).
        won = bucket_contains(lo, hi, width, actual)

        size  = float(trade.size)
        price = float(trade.price)
        trade.simulated_pnl = round(size / price - size, 2) if won else round(-size, 2)
        
        print(
            f"   💸 Settled YES: {trade.location_id} on {trade.market_date} | "
            f"Actual: {actual}° | Bucket: [{lo}, {hi}) w={width} | {'WIN 🏆' if won else 'LOSS ❌'} | PnL: ${trade.simulated_pnl}"
        )
        settled += 1

    session.commit()
    print(f"   PnL settler: settled {settled} trade(s).")


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == 'precip':
        months = int(_sys.argv[2]) if len(_sys.argv) > 2 else 3
        backfill_precip_outcomes(months)
    elif len(_sys.argv) > 1 and _sys.argv[1] == 'migrate':
        # Re-fetch legacy ERA5/Open-Meteo rows onto the airport-METAR (IEM) source.
        days = int(_sys.argv[2]) if len(_sys.argv) > 2 else 70
        backfill_outcomes(days, overwrite_sources={'open-meteo', 'era5'})
    else:
        days = int(_sys.argv[1]) if len(_sys.argv) > 1 else 7
        backfill_outcomes(days)
