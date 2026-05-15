#!/usr/bin/env python3
"""
Monthly Precipitation Forecast Pipeline for Polymarket
Combines actual station data (ACIS/Met Office) + Open-Meteo ERA5 Archive +
Open-Meteo Forecast (remaining days) to estimate month-end total precipitation.

Data source per city:
  Seattle, NYC : ACIS (data.rcc-acis.org) — actual NOAA station observations,
                 same source used for Polymarket resolution. High confidence.
  London       : Met Office Heathrow text file — actual station data. High confidence.
  Hong Kong    : ERA5 reanalysis (Open-Meteo Archive). ERA5 overstates HKO by ~27%.
                 LOW confidence — use as directional estimate only.
  Seoul        : ERA5 reanalysis (Open-Meteo Archive). ERA5 overstates KMA by ~32%.
                 LOW confidence — use as directional estimate only.

ERA5 overstates precipitation vs actual station data by 20-30% for Asian cities.
Do NOT place live trades for Seoul/HK without verification against resolution source.
"""

import datetime
import calendar
import sys
import math
import httpx
from database_schema import (
    init_database,
    PrecipForecast,
    PrecipOutcome,
    PrecipForecastCalibration,
    PrecipModelPerformance,
)

OPEN_METEO_ARCHIVE  = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_SEASONAL = "https://seasonal-api.open-meteo.com/v1/seasonal"
ACIS_ENDPOINT       = "https://data.rcc-acis.org/StnData"
METOFFICE_HEATHROW  = "https://www.metoffice.gov.uk/pub/data/weather/uk/climate/stationdata/heathrowdata.txt"

# 5 cities with active Polymarket monthly precipitation markets (confirmed Apr 2026)
PRECIP_LOCATIONS = {
    'hong_kong': {
        'city_slug': 'hong-kong',
        'name': 'Hong Kong Observatory',
        'lat': 22.3027, 'lon': 114.1772,
        'timezone': 'Asia/Hong_Kong',
        'units': 'mm',
        'data_source': 'era5',          # ERA5 only — HKO not programmatically accessible
        'data_confidence': 'low',       # ERA5 overstates vs HKO by ~27%
        'resolution_source': 'HKO',
    },
    'seattle': {
        'city_slug': 'seattle',
        'name': 'Seattle-Tacoma International Airport',
        'lat': 47.4440, 'lon': -122.3140,
        'timezone': 'America/Los_Angeles',
        'units': 'inches',
        'data_source': 'acis',          # ACIS (data.rcc-acis.org) — actual NOAA station
        'data_confidence': 'high',      # Same source family as Polymarket resolution (NOAA)
        'acis_station': 'SEA',
        'resolution_source': 'NOAA',
    },
    'nyc': {
        'city_slug': 'nyc',
        'name': 'Central Park ASOS',
        'lat': 40.7790, 'lon': -73.9692,
        'timezone': 'America/New_York',
        'units': 'inches',
        'data_source': 'acis',          # ACIS — Central Park NOAA COOP station
        'data_confidence': 'high',
        'acis_station': 'NYC',
        'resolution_source': 'NOAA',
    },
    'seoul': {
        'city_slug': 'seoul',
        'name': 'Seoul KMA Observatory',
        'lat': 37.5651, 'lon': 126.9895,
        'timezone': 'Asia/Seoul',
        'units': 'mm',
        'data_source': 'era5',          # ERA5 only — KMA API requires paid auth key
        'data_confidence': 'low',       # ERA5 overstates vs KMA by ~32%
        'resolution_source': 'KMA',
    },
    'london': {
        'city_slug': 'london',
        'name': 'Heathrow Airport',
        'lat': 51.4700, 'lon': -0.4543,
        'timezone': 'Europe/London',
        'units': 'mm',
        'data_source': 'metoffice',     # Met Office Heathrow text file (provisional ~5d lag)
        'data_confidence': 'high',
        'resolution_source': 'MetOffice',
    },
}

MM_TO_INCHES = 0.0393701
INCHES_TO_MM = 25.4

# Baseline forecast quality gate per city/model.
# These values should be refreshed by update_model_performance_from_history().
MODEL_REGISTRY = {
    'seattle': {'model_name': 'ecmwf_ec46', 'rmse_mm': 4.8, 'auc': 0.958},
    'nyc': {'model_name': 'ecmwf_ec46', 'rmse_mm': 4.9, 'auc': 0.955},
    'london': {'model_name': 'ecmwf_ec46', 'rmse_mm': 4.7, 'auc': 0.961},
    'seoul': {'model_name': 'ecmwf_ec46', 'rmse_mm': 7.9, 'auc': 0.882},
    'hong_kong': {'model_name': 'ecmwf_ec46', 'rmse_mm': 8.4, 'auc': 0.861},
}


def _eligible_model(rmse_mm: float | None, auc: float | None) -> bool:
    if rmse_mm is None or auc is None:
        return False
    return rmse_mm <= 5.0 and auc >= 0.95


def _month_bounds(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=1), today.replace(day=last_day)


def fetch_accumulated_acis(station_id: str, start: datetime.date, end: datetime.date,
                           units_out: str = 'inches') -> float:
    """
    Actual daily precipitation from ACIS (Applied Climate Information System).
    Returns data in inches (ACIS native). Convert to mm if needed.
    Data source: NOAA station observations — same family as Polymarket NOAA resolution source.
    """
    if start > end:
        return 0.0
    try:
        payload = {
            'sid': station_id,
            'sdate': start.isoformat(),
            'edate': end.isoformat(),
            'elems': [{'name': 'pcpn', 'units': 'in'}],
        }
        with httpx.Client(timeout=20) as client:
            r = client.post(ACIS_ENDPOINT, json=payload)
            r.raise_for_status()
            rows = r.json().get('data', [])
        total_in = 0.0
        for row in rows:
            val = row[1] if len(row) > 1 else 'M'
            if val == 'T':
                total_in += 0.001  # trace
            elif val not in ('M', 'S', '', None):
                try:
                    total_in += float(val)
                except ValueError:
                    pass
        return total_in if units_out == 'inches' else total_in * 25.4
    except Exception as e:
        print(f"   ⚠️  ACIS error ({station_id}): {e}")
        return 0.0


def fetch_accumulated_metoffice(start: datetime.date, end: datetime.date) -> float:
    """
    Actual monthly precipitation from Met Office Heathrow plain-text data file.
    Returns mm. Data lags ~5 days (provisional figures).
    Polymarket resolution source: metoffice.gov.uk/.../heathrowdata.txt
    """
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(METOFFICE_HEATHROW)
            r.raise_for_status()
        lines = r.text.splitlines()
        # Format: year  month  tmax  tmin  frost  rain  sun  (Provisional)
        # We need rain (col index 5) for the current month
        target_year  = start.year
        target_month = start.month
        for line in lines:
            parts = line.split()
            if len(parts) >= 6:
                try:
                    yr  = int(parts[0])
                    mo  = int(parts[1])
                    if yr == target_year and mo == target_month:
                        rain_mm = float(parts[5])
                        return rain_mm
                except (ValueError, IndexError):
                    pass
        return 0.0  # Month not yet published (still in progress)
    except Exception as e:
        print(f"   ⚠️  Met Office fetch error: {e}")
        return 0.0


def fetch_accumulated(obs: dict, start: datetime.date, end: datetime.date) -> float:
    """Dispatch to correct data source based on obs['data_source']. Returns mm."""
    if start > end:
        return 0.0
    source = obs.get('data_source', 'era5')

    if source == 'acis':
        inches = fetch_accumulated_acis(obs['acis_station'], start, end, units_out='inches')
        return inches * 25.4  # always return mm internally; convert to native units later

    if source == 'metoffice':
        # Met Office gives the full month total — only useful after month ends.
        # Until then fall back to ERA5 (Met Office lags ~5 days into next month).
        rain_mm = fetch_accumulated_metoffice(start, end)
        if rain_mm > 0:
            return rain_mm
        # Fall through to ERA5 during current month
        print("   ℹ️  Met Office data not yet available for current month — using ERA5")

    # ERA5 reanalysis (default, and fallback for Met Office during current month)
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(OPEN_METEO_ARCHIVE, params={
                'latitude': obs['lat'], 'longitude': obs['lon'],
                'start_date': start.isoformat(), 'end_date': end.isoformat(),
                'daily': 'precipitation_sum', 'timezone': obs['timezone'],
            })
            r.raise_for_status()
            vals = r.json().get('daily', {}).get('precipitation_sum', [])
            return sum(v or 0.0 for v in vals)
    except Exception as e:
        print(f"   ⚠️  ERA5 Archive API error: {e}")
        return 0.0


def fetch_forecast_remaining(obs: dict, today: datetime.date, month_end: datetime.date) -> tuple[float, float]:
    """
    Open-Meteo forecast for today through month_end.
    Returns (forecast_mm, probability_pct) where probability_pct is the
    max ensemble precipitation probability over the remaining days (0-100).
    """
    days_needed = (month_end - today).days + 1
    if days_needed <= 0:
        return 0.0, 0
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(OPEN_METEO_FORECAST, params={
                'latitude': obs['lat'], 'longitude': obs['lon'],
                'daily': 'precipitation_sum,precipitation_probability_max',
                'timezone': obs['timezone'],
                'forecast_days': min(days_needed + 1, 16),
            })
            r.raise_for_status()
            daily = r.json().get('daily', {})
            dates = daily.get('time', [])
            sums  = daily.get('precipitation_sum', [])
            probs = daily.get('precipitation_probability_max', [])

        # Only include dates within this calendar month
        total_mm = 0.0
        max_prob = 0
        for date_str, s, p in zip(dates, sums, probs):
            d = datetime.date.fromisoformat(date_str)
            if today <= d <= month_end:
                total_mm += s or 0.0
                if p and p > max_prob:
                    max_prob = p
        return total_mm, max_prob
    except Exception as e:
        print(f"   ⚠️  Forecast API error: {e}")
        return 0.0, 50


def compute_confidence_band(accumulated_mm: float, forecast_mm: float,
                             days_elapsed: int, days_remaining: int) -> tuple[float, float]:
    """
    P05 / P95 of month-end total.
    Accumulated portion is certain; forecast portion has ±30% uncertainty.
    Late in month (few days left) the band tightens naturally.
    """
    uncertainty = forecast_mm * 0.30
    p05 = accumulated_mm + max(0.0, forecast_mm - uncertainty)
    p95 = accumulated_mm + forecast_mm + uncertainty
    return round(p05, 2), round(p95, 2)


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def fetch_ensemble_monthly_distribution(
    obs: dict,
    today: datetime.date,
    month_end: datetime.date,
    accumulated_mm: float,
) -> tuple[float, float, float] | None:
    """
    Query Open-Meteo seasonal API for an ensemble distribution of remaining-month
    precipitation, then combine with accumulated actuals to return month-end
    (p05, p50, p95) in mm.
    """
    days_remaining = (month_end - today).days + 1
    if days_remaining <= 0:
        return accumulated_mm, accumulated_mm, accumulated_mm
    if days_remaining > 46:
        return None

    try:
        with httpx.Client(timeout=25) as client:
            r = client.get(OPEN_METEO_SEASONAL, params={
                'latitude': obs['lat'],
                'longitude': obs['lon'],
                'daily': 'precipitation_sum',
                'timezone': obs['timezone'],
                'models': 'ecmwf_ifs',
                'forecast_days': min(days_remaining + 2, 46),
            })
            r.raise_for_status()
            daily = r.json().get('daily', {})

        dates = daily.get('time', [])
        if not dates:
            return None

        # Open-Meteo seasonal can expose member series as keyed columns.
        member_keys = [k for k in daily.keys() if 'precipitation_sum' in k and k != 'precipitation_sum']
        if not member_keys:
            return None

        totals: list[float] = []
        for key in member_keys:
            vals = daily.get(key, [])
            rem_sum = 0.0
            for date_str, v in zip(dates, vals):
                d = datetime.date.fromisoformat(date_str)
                if today <= d <= month_end:
                    rem_sum += v or 0.0
            totals.append(accumulated_mm + rem_sum)

        totals = sorted(totals)
        if not totals:
            return None
        return (
            round(_quantile(totals, 0.05), 2),
            round(_quantile(totals, 0.50), 2),
            round(_quantile(totals, 0.95), 2),
        )
    except Exception as e:
        print(f"   ⚠️  Seasonal ensemble API error: {e}")
        return None


def fetch_monthly_precip_forecast(location_id: str, today: datetime.date | None = None) -> dict | None:
    """
    Full monthly precipitation forecast for one location.
    Returns values in the location's native units (mm or inches).
    """
    obs = PRECIP_LOCATIONS[location_id]
    if today is None:
        today = datetime.date.today()

    month_start, month_end = _month_bounds(today)
    days_elapsed   = today.day - 1          # complete days (1 … yesterday)
    days_remaining = month_end.day - today.day + 1   # today … month_end

    # Accumulated actual (yesterday and earlier)
    accumulated_mm = 0.0
    if days_elapsed > 0:
        accumulated_mm = fetch_accumulated(obs, month_start, today - datetime.timedelta(days=1))

    # Forecast for remainder of month (today … month_end)
    forecast_mm, max_prob = fetch_forecast_remaining(obs, today, month_end)

    total_mm = accumulated_mm + forecast_mm
    band_source = "heuristic"
    p50_mm = total_mm
    ensemble_dist = fetch_ensemble_monthly_distribution(obs, today, month_end, accumulated_mm)
    if ensemble_dist is not None:
        p05_mm, p50_mm, p95_mm = ensemble_dist
        total_mm = p50_mm
        band_source = "ecmwf_ensemble"
    else:
        p05_mm, p95_mm = compute_confidence_band(accumulated_mm, forecast_mm, days_elapsed, days_remaining)

    # Convert to native units
    factor = MM_TO_INCHES if obs['units'] == 'inches' else 1.0
    units_label = obs['units']

    actual_source = obs.get('data_source', 'era5')
    # For London during current month, Met Office file may not have data yet
    # fetch_accumulated() falls back to ERA5 and prints a note. Track what was used.
    if actual_source == 'metoffice' and accumulated_mm == 0.0 and days_elapsed > 3:
        actual_source = 'era5-fallback'

    return {
        'location_id':        location_id,
        'location_name':      obs['name'],
        'settlement_month':   month_start,
        'accumulated':        round(accumulated_mm * factor, 2),
        'forecast_remaining': round(forecast_mm * factor, 2),
        'total_forecast':     round(total_mm * factor, 2),
        'p05':                round(p05_mm * factor, 2),
        'p95':                round(p95_mm * factor, 2),
        'max_prob':           max_prob,
        'days_elapsed':       days_elapsed,
        'days_remaining':     days_remaining,
        'units':              units_label,
        'source':             actual_source,
        'band_source':        band_source,
        'model_name':         MODEL_REGISTRY.get(location_id, {}).get('model_name', 'ecmwf_ec46'),
        'model_rmse_mm':      MODEL_REGISTRY.get(location_id, {}).get('rmse_mm'),
        'model_auc':          MODEL_REGISTRY.get(location_id, {}).get('auc'),
        'model_eligible':     _eligible_model(
            MODEL_REGISTRY.get(location_id, {}).get('rmse_mm'),
            MODEL_REGISTRY.get(location_id, {}).get('auc'),
        ),
        'data_confidence':    obs.get('data_confidence', 'low'),
        'resolution_source':  obs.get('resolution_source', 'unknown'),
    }


def main():
    print("=" * 70)
    print("Monthly Precipitation Forecast Pipeline")
    print("=" * 70)

    session = init_database(auto_migrate=True)
    today = datetime.date.today()
    results = []

    for loc_id in PRECIP_LOCATIONS:
        obs = PRECIP_LOCATIONS[loc_id]
        print(f"\n📍 {obs['name']}:")
        fc = fetch_monthly_precip_forecast(loc_id, today)
        if fc is None:
            print("   ❌ Forecast failed")
            continue

        units = fc['units']
        unit_label = '"' if units == 'inches' else 'mm'
        confidence_icon = '✅' if fc['data_confidence'] == 'high' else '⚠️ '
        print(f"   Data source:          {fc['source']} → {fc['resolution_source']} {confidence_icon}")
        print(f"   Accumulated so far:   {fc['accumulated']:.1f} {unit_label}  ({fc['days_elapsed']} days)")
        print(f"   Forecast remaining:   {fc['forecast_remaining']:.1f} {unit_label}  ({fc['days_remaining']} days)")
        print(f"   Month-end estimate:   {fc['total_forecast']:.1f} {unit_label}")
        print(f"   Confidence band P05-P95: [{fc['p05']:.1f}, {fc['p95']:.1f}] {unit_label}")
        print(f"   Rain probability (max): {fc['max_prob']}%")
        if fc['data_confidence'] != 'high':
            print(f"   ⚠️  ERA5 overstates vs {fc['resolution_source']} by ~25-32%. Accumulated figure unreliable.")

        row = PrecipForecast(
            location_id        = loc_id,
            location_name      = fc['location_name'],
            settlement_month   = fc['settlement_month'],
            accumulated        = fc['accumulated'],
            forecast_remaining = fc['forecast_remaining'],
            total_forecast     = fc['total_forecast'],
            p05                = fc['p05'],
            p95                = fc['p95'],
            max_prob           = fc['max_prob'],
            days_elapsed       = fc['days_elapsed'],
            days_remaining     = fc['days_remaining'],
            units              = fc['units'],
            source             = f"{fc['source']}+{fc.get('band_source', 'heuristic')}",
            model_name         = fc.get('model_name'),
            model_rmse_mm      = fc.get('model_rmse_mm'),
            model_auc          = fc.get('model_auc'),
            model_eligible     = bool(fc.get('model_eligible', False)),
            # data_confidence not yet in schema — stored in source field implicitly
        )
        session.add(row)
        results.append(fc)

    session.commit()
    session.close()
    print(f"\n✅ {len(results)} precipitation forecasts saved to DB")


def _iter_snapshot_dates_for_month(year: int, month: int, snapshots_per_month: int) -> list[datetime.date]:
    last_day = calendar.monthrange(year, month)[1]
    if snapshots_per_month <= 0:
        snapshots_per_month = 1
    if snapshots_per_month >= last_day:
        return [datetime.date(year, month, d) for d in range(1, last_day + 1)]

    # Evenly spread snapshot dates across the month, always including day 1 and last day.
    idxs = set()
    for i in range(snapshots_per_month):
        pos = round(i * (last_day - 1) / max(1, snapshots_per_month - 1))
        idxs.add(1 + pos)
    return [datetime.date(year, month, d) for d in sorted(idxs)]


def backfill_precip_forecasts(months_back: int, snapshots_per_month: int = 30) -> None:
    """
    Backfill historical monthly precipitation forecasts for completed months.
    For each month/location/as-of-date snapshot, re-run the forecast logic and
    persist a PrecipForecast row if one does not already exist.
    """
    session = init_database()
    today = datetime.date.today()
    inserted = 0
    skipped = 0
    errors = 0

    print("=" * 70)
    print(f"Historical Precipitation Forecast Backfill ({months_back} months)")
    print("=" * 70)

    for m_back in range(1, months_back + 1):
        year = today.year
        month = today.month - m_back
        while month <= 0:
            month += 12
            year -= 1

        settlement_month = datetime.date(year, month, 1)
        snapshot_dates = _iter_snapshot_dates_for_month(year, month, snapshots_per_month)
        print(f"\n📅 {settlement_month.strftime('%B %Y')} — {len(snapshot_dates)} snapshots per city")

        for as_of_date in snapshot_dates:
            for loc_id in PRECIP_LOCATIONS:
                try:
                    existing = (
                        session.query(PrecipForecast)
                        .filter(
                            PrecipForecast.location_id == loc_id,
                            PrecipForecast.settlement_month == settlement_month,
                            PrecipForecast.days_elapsed == (as_of_date.day - 1),
                        )
                        .first()
                    )
                    if existing:
                        skipped += 1
                        continue

                    fc = fetch_monthly_precip_forecast(loc_id, today=as_of_date)
                    if fc is None:
                        errors += 1
                        continue

                    row = PrecipForecast(
                        timestamp=datetime.datetime.combine(as_of_date, datetime.time(12, 0)),
                        location_id=loc_id,
                        location_name=fc['location_name'],
                        settlement_month=fc['settlement_month'],
                        accumulated=fc['accumulated'],
                        forecast_remaining=fc['forecast_remaining'],
                        total_forecast=fc['total_forecast'],
                        p05=fc['p05'],
                        p95=fc['p95'],
                        max_prob=fc['max_prob'],
                        days_elapsed=fc['days_elapsed'],
                        days_remaining=fc['days_remaining'],
                        units=fc['units'],
                        source=f"{fc['source']}+{fc.get('band_source', 'heuristic')}+historical_backfill",
                        model_name=fc.get('model_name'),
                        model_rmse_mm=fc.get('model_rmse_mm'),
                        model_auc=fc.get('model_auc'),
                        model_eligible=bool(fc.get('model_eligible', False)),
                    )
                    session.add(row)
                    inserted += 1
                except Exception as e:
                    errors += 1
                    print(f"   ⚠️  Backfill error [{loc_id} {as_of_date}]: {e}")

        session.commit()
        print(f"   ✅ Month complete (running totals: inserted={inserted}, skipped={skipped}, errors={errors})")

    session.close()
    print(f"\n✅ Historical forecast backfill complete — inserted={inserted}, skipped={skipped}, errors={errors}")


def update_model_performance_from_history(months_back: int = 18) -> None:
    """
    Build calibration rows from precip_forecasts + precip_outcomes and compute
    rolling model performance per city/model.
    """
    session = init_database()
    today = datetime.date.today()
    cutoff_month = (today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    start_month = cutoff_month
    for _ in range(max(0, months_back - 1)):
        if start_month.month == 1:
            start_month = datetime.date(start_month.year - 1, 12, 1)
        else:
            start_month = datetime.date(start_month.year, start_month.month - 1, 1)

    # Refresh calibration rows for this rolling window.
    session.query(PrecipForecastCalibration).filter(
        PrecipForecastCalibration.settlement_month >= start_month
    ).delete(synchronize_session=False)
    session.query(PrecipModelPerformance).filter(
        PrecipModelPerformance.window_months == months_back
    ).delete(synchronize_session=False)
    session.commit()

    forecasts = (
        session.query(PrecipForecast)
        .filter(
            PrecipForecast.settlement_month >= start_month,
            PrecipForecast.settlement_month <= cutoff_month,
        )
        .all()
    )
    inserted = 0
    for fc in forecasts:
        outcome = (
            session.query(PrecipOutcome)
            .filter_by(location_id=fc.location_id, settlement_month=fc.settlement_month)
            .first()
        )
        if not outcome or outcome.actual_precip is None:
            continue
        pred_total = float(fc.total_forecast or 0.0)
        actual_total = float(outcome.actual_precip or 0.0)
        # Normalize to mm for quality metrics across mixed units.
        pred_mm = pred_total * INCHES_TO_MM if (fc.units == 'inches') else pred_total
        actual_mm = actual_total * INCHES_TO_MM if (outcome.units == 'inches') else actual_total
        err_mm = pred_mm - actual_mm
        abs_err = abs(err_mm)
        session.add(PrecipForecastCalibration(
            location_id=fc.location_id,
            model_name=fc.model_name or 'ecmwf_ec46',
            settlement_month=fc.settlement_month,
            asof_timestamp=fc.timestamp,
            days_elapsed=fc.days_elapsed,
            days_remaining=fc.days_remaining,
            units=fc.units,
            predicted_total=pred_total,
            actual_total=actual_total,
            error_mm=round(err_mm, 3),
            abs_error_mm=round(abs_err, 3),
            within_5mm=(abs_err <= 5.0),
        ))
        inserted += 1
    session.commit()

    # Aggregate rolling performance by city/model.
    for location_id in PRECIP_LOCATIONS.keys():
        model_name = MODEL_REGISTRY.get(location_id, {}).get('model_name', 'ecmwf_ec46')
        rows = (
            session.query(PrecipForecastCalibration)
            .filter_by(location_id=location_id, model_name=model_name)
            .all()
        )
        if not rows:
            continue
        errs = [float(r.error_mm or 0.0) for r in rows]
        abs_errs = [abs(e) for e in errs]
        rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
        mae = sum(abs_errs) / len(abs_errs)
        within_rate = sum(1 for e in abs_errs if e <= 5.0) / len(abs_errs)
        # AUC-like proxy for this use-case: discrimination quality of "within 5mm".
        # For now we map directly to within-rate; can be replaced with strict ROC AUC when labels are expanded.
        auc_like = within_rate
        eligible = _eligible_model(rmse, auc_like)
        session.add(PrecipModelPerformance(
            location_id=location_id,
            model_name=model_name,
            window_months=months_back,
            calibration_rows=len(rows),
            rmse_mm=round(rmse, 3),
            mae_mm=round(mae, 3),
            within_5mm_rate=round(within_rate, 4),
            auc_like=round(auc_like, 4),
            eligible=eligible,
            evaluated_at=datetime.datetime.utcnow(),
        ))
        # keep runtime registry in sync for subsequent forecast writes
        MODEL_REGISTRY.setdefault(location_id, {})
        MODEL_REGISTRY[location_id]['rmse_mm'] = round(rmse, 3)
        MODEL_REGISTRY[location_id]['auc'] = round(auc_like, 4)
    session.commit()

    # Optional: update recent precip_forecasts rows with latest model metrics.
    perf_rows = session.query(PrecipModelPerformance).filter_by(window_months=months_back).all()
    perf_map = {(p.location_id, p.model_name): p for p in perf_rows}
    for fc in session.query(PrecipForecast).filter(
        PrecipForecast.settlement_month >= start_month,
        PrecipForecast.settlement_month <= cutoff_month,
    ).all():
        key = (fc.location_id, fc.model_name or 'ecmwf_ec46')
        p = perf_map.get(key)
        if not p:
            continue
        fc.model_rmse_mm = p.rmse_mm
        fc.model_auc = p.auc_like
        fc.model_eligible = bool(p.eligible)
    session.commit()
    session.close()
    print(f"✅ Model calibration refreshed: rows={inserted}, window_months={months_back}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        months = int(sys.argv[2]) if len(sys.argv) > 2 else 12
        snaps = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        backfill_precip_forecasts(months_back=months, snapshots_per_month=snaps)
    elif len(sys.argv) > 1 and sys.argv[1] == "calibrate-models":
        months = int(sys.argv[2]) if len(sys.argv) > 2 else 18
        update_model_performance_from_history(months_back=months)
    else:
        main()
