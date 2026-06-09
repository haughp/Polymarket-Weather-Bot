#!/usr/bin/env python3
"""
Phase 0 validation harness (READ-ONLY).

Question: does IEM-ASOS hourly-METAR daily extremes match the value Polymarket
actually settles on, better than the current ERA5 (Open-Meteo Archive) outcomes?

For each city × last N days it compares three numbers:
  1. IEM   — daily max/min derived from hourly METAR temps (= Weather Underground's method)
  2. ERA5  — the current `outcomes.actual_max_temp` (what the bias corrector trains on today)
  3. PM    — the Polymarket winning bucket (ground truth: which band YES paid out in)

Outputs one row per city: n, MAE(iem-era5), bias(iem-era5), iem-in-PM-band %,
era5-in-PM-band %, iem coverage %. Writes NOTHING — pure measurement.

Usage:  python3 validate_outcome_source.py [days_back=30]
"""

import sys
import math
import datetime
from pathlib import Path

import requests
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from database_schema import init_database
from outcome_backfiller import OBSERVATORIES
from polymarket_dry_run import (
    LOCATION_SLUGS,
    fetch_event_markets,
    parse_temp_range,
)

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# Resolution stations taken from each Polymarket market's own description /
# resolutionSource URL (the authoritative source — NOT guessed from OBSERVATORIES).
# Every temperature market states "...recorded at the <Airport> Station..." and a
# wunderground.com/history/daily/.../<ICAO> link. hong_kong is the exception: it
# resolves on the Hong Kong Observatory "Absolute Daily Max", not an airport METAR.
IEM_STATIONS = {
    'new_york':     'KLGA',   # LaGuardia (NOT Central Park/KNYC)
    'atlanta':      'KATL',   # Hartsfield-Jackson
    'dallas':       'KDAL',   # Dallas Love Field
    'chicago':      'KORD',   # O'Hare
    'miami':        'KMIA',   # Miami Intl
    'seattle':      'KSEA',   # Sea-Tac
    'austin':       'KAUS',   # Austin-Bergstrom
    'london':       'EGLC',   # London City (NOT Heathrow/EGLL)
    'paris':        'LFPB',   # Paris-Le Bourget (NOT Orly/CDG)
    'madrid':       'LEMD',   # Barajas
    'moscow':       'UUWW',   # Vnukovo (PM resolutionSource blank; verify)
    'beijing':      'ZBAA',   # Capital
    'shanghai':     'ZSPD',   # Pudong
    'hong_kong':    'VHHH',   # Chek Lap Kok — but PM resolves on HKO, not this; see notes
    'singapore':    'WSSS',   # Changi
    'tokyo':        'RJTT',   # Haneda
    'taipei':       'RCSS',   # Songshan
    'chongqing':    'ZUCK',   # Jiangbei
    'qingdao':      'ZSQD',   # Jiaodong
    'shenzhen':     'ZGSZ',   # Bao'an
    'buenos_aires': 'SAEZ',   # Ezeiza/Pistarini (NOT Aeroparque/SABE)
    'lucknow':      'VILK',   # Chaudhary Charan Singh
    'toronto':      'CYYZ',   # Pearson
}


def fetch_iem_extremes(station: str, tz: str, units: str, obs_date: datetime.date):
    """Daily (max, min) from IEM ASOS hourly METAR, grouped by LOCAL date.
    Returns native units (°F if units=='fahrenheit', else °C). (None, None) on no data."""
    data_field = 'tmpf' if units == 'fahrenheit' else 'tmpc'
    params = {
        'station': station, 'data': data_field,
        'year1': obs_date.year, 'month1': obs_date.month, 'day1': obs_date.day,
        'year2': obs_date.year, 'month2': obs_date.month, 'day2': obs_date.day,
        'tz': tz, 'format': 'onlycomma', 'latlon': 'no', 'direct': 'yes',
    }
    try:
        r = requests.get(IEM_URL, params=params, timeout=20)
        r.raise_for_status()
    except Exception:
        return None, None
    temps = []
    for line in r.text.splitlines():
        parts = line.strip().split(',')
        if len(parts) < 3:
            continue
        try:
            temps.append(float(parts[2].strip()))
        except ValueError:
            continue
    if not temps:
        return None, None
    return max(temps), min(temps)


def polymarket_winning_band(location_id: str, date: datetime.date, mode: str = 'max'):
    """Return (low, high) of the bucket whose YES paid out, or None if not resolved.
    For single-degree °C buckets parse_temp_range returns (X, X); we widen to the
    rounding band [X-0.5, X+0.5] since Polymarket rounds the reading to that integer.
    """
    slug = LOCATION_SLUGS.get(location_id)
    if not slug:
        return None
    markets = fetch_event_markets(slug, date, mode)
    if not markets:
        return None
    for m in markets:
        op = m.get('outcomePrices')
        if isinstance(op, str):
            import json
            try:
                op = json.loads(op)
            except Exception:
                continue
        if not op or str(op[0]) != '1':   # YES (index 0) must have paid out
            continue
        low, high = parse_temp_range(m.get('question', ''))
        if low is None and high is None:
            continue
        # Single-degree exact bucket → widen to rounding band.
        if low is not None and high is not None and low == high:
            return (low - 0.5, high + 0.5)
        return (low, high)
    return None


def in_band(value, band):
    """Is value within the (low, high) band? Open-ended bounds use None."""
    if value is None or band is None:
        return None
    low, high = band
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True


def main(days_back: int = 30):
    session = init_database()
    today = datetime.date.today()
    cities = sorted(IEM_STATIONS.keys())

    print(f"\nPhase 0 validation — last {days_back} days, {len(cities)} cities")
    print("IEM (hourly METAR) vs ERA5 (current outcomes) vs Polymarket winning band\n")
    header = (f"{'city':<13} {'unit':<4} {'n':>3} {'cov%':>5} "
              f"{'MAE':>6} {'bias':>6} {'iem∈PM%':>8} {'era5∈PM%':>9} {'pm_n':>5}")
    print(header)
    print("-" * len(header))

    for loc in cities:
        obs = OBSERVATORIES.get(loc)
        if not obs:
            continue
        units = obs['units']
        tz = obs['timezone']
        station = IEM_STATIONS[loc]
        unit_lbl = '°F' if units == 'fahrenheit' else '°C'

        n = iem_cov = 0
        abs_errs = []
        signed_errs = []
        iem_hits = era5_hits = pm_n = 0

        for d in range(1, days_back + 1):
            day = today - datetime.timedelta(days=d)

            iem_max, _ = fetch_iem_extremes(station, tz, units, day)
            row = session.execute(text(
                "SELECT actual_max_temp FROM outcomes "
                "WHERE location_id=:c AND date::date=:d"
            ), {'c': loc, 'd': day}).fetchone()
            era5_max = float(row[0]) if row and row[0] is not None else None

            n += 1
            if iem_max is not None:
                iem_cov += 1
            if iem_max is not None and era5_max is not None:
                abs_errs.append(abs(iem_max - era5_max))
                signed_errs.append(iem_max - era5_max)

            band = polymarket_winning_band(loc, day, 'max')
            if band is not None:
                pm_n += 1
                if in_band(iem_max, band):
                    iem_hits += 1
                if in_band(era5_max, band):
                    era5_hits += 1

        mae = sum(abs_errs) / len(abs_errs) if abs_errs else float('nan')
        bias = sum(signed_errs) / len(signed_errs) if signed_errs else float('nan')
        cov = 100.0 * iem_cov / n if n else 0.0
        iem_pm = 100.0 * iem_hits / pm_n if pm_n else float('nan')
        era5_pm = 100.0 * era5_hits / pm_n if pm_n else float('nan')

        def fmt(x):
            return f"{x:6.2f}" if not math.isnan(x) else "   n/a"

        print(f"{loc:<13} {unit_lbl:<4} {n:>3} {cov:>5.0f} "
              f"{fmt(mae)} {fmt(bias)} {fmt(iem_pm):>8} {fmt(era5_pm):>9} {pm_n:>5}")

    print("\nGate: iem coverage ≥90% for ≥18/20 cities; non-US iem∈PM% materially > era5∈PM%.")
    print("Flag any city with low coverage or low iem∈PM% for manual ICAO review.\n")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    main(days)
