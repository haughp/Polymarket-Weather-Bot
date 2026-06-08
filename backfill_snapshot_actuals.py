#!/usr/bin/env python3
"""
Back-fill actual_temp and winning_range on weatherbot_signal_snapshots.

For each unresolved snapshot (actual_temp IS NULL, market_date < today):
  1. Fetch IEM ASOS hourly observations grouped by LOCAL calendar date
     (not UTC — avoids the UTC boundary bug where evening peaks fall on the wrong date).
  2. Compute daily max (highest) and daily min (lowest) per city.
  3. Match winning_range from weatherbot_ts_trade_history WIN trades for the same city/date.
  4. UPDATE weatherbot_signal_snapshots.

Run manually or nightly. Safe to re-run — only touches rows where actual_temp IS NULL.
"""

import sys
import re
import requests
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path.home() / "sniff_test_polymarket"))
from dotenv import load_dotenv
load_dotenv(Path.home() / "sniff_test_polymarket" / ".env")

from database_schema import init_database, WeatherBotSignalSnapshot, WeatherBotTsTradeHistory

# Maps city slug → IEM ASOS station + IANA timezone (mirrors nws.ts LOCATIONS + STATION_IDS)
CITY_META = {
    "nyc":     {"station": "KLGA", "tz": "America/New_York"},
    "chicago": {"station": "KORD", "tz": "America/Chicago"},
    "miami":   {"station": "KMIA", "tz": "America/New_York"},
    "dallas":  {"station": "KDAL", "tz": "America/Chicago"},
    "seattle": {"station": "KSEA", "tz": "America/Los_Angeles"},
    "atlanta": {"station": "KATL", "tz": "America/New_York"},
    # US expansion (2026-06-02) — mirrors nws.ts LOCATIONS + STATION_IDS
    "houston":       {"station": "KIAH", "tz": "America/Chicago"},
    "denver":        {"station": "KDEN", "tz": "America/Denver"},
    "los-angeles":   {"station": "KLAX", "tz": "America/Los_Angeles"},
    "san-francisco": {"station": "KSFO", "tz": "America/Los_Angeles"},
    "austin":        {"station": "KAUS", "tz": "America/Chicago"},
}

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def fetch_iem_obs(station: str, tz: str, obs_date: date) -> tuple[float | None, float | None]:
    """Return (daily_max_f, daily_min_f) for obs_date in local timezone from IEM ASOS.
    Passing tz= to IEM returns timestamps in local time so we can group by calendar date
    without UTC boundary errors."""
    params = {
        "station": station,
        "data": "tmpf",
        "year1": obs_date.year,  "month1": obs_date.month,  "day1": obs_date.day,
        "year2": obs_date.year,  "month2": obs_date.month,  "day2": obs_date.day,
        "tz": tz,
        "format": "onlycomma",
        "latlon": "no",
        "direct": "yes",
    }
    try:
        r = requests.get(IEM_URL, params=params, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠️  IEM fetch failed for {station} {obs_date}: {e}")
        return None, None

    temps: list[float] = []
    for line in r.text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        raw = parts[2].strip()
        try:
            temps.append(float(raw))
        except ValueError:
            continue

    if not temps:
        return None, None
    return max(temps), min(temps)


def parse_winning_range(question: str) -> str | None:
    """Extract '82-83' style range from a resolved YES trade question.
    Question text: '...will be between 82 and 83 degrees...' or '...82 to 83°F...'"""
    # Match patterns like '82 to 83', '82 and 83', '82-83'
    m = re.search(r'(\d+)\s*(?:to|and|-)\s*(\d+)', question)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    # Match 'or above' / 'or higher' sentinel buckets
    m2 = re.search(r'(\d+)\s*(?:or above|or higher)', question, re.IGNORECASE)
    if m2:
        return f"ge{m2.group(1)}"
    # Match 'or below' / 'or lower'
    m3 = re.search(r'(\d+)\s*(?:or below|or lower)', question, re.IGNORECASE)
    if m3:
        return f"le{m3.group(1)}"
    return None


def backfill(dry_run: bool = False) -> None:
    session = init_database()
    today = date.today()

    unresolved = (
        session.query(WeatherBotSignalSnapshot)
        .filter(
            WeatherBotSignalSnapshot.actual_temp.is_(None),
            WeatherBotSignalSnapshot.market_date < today,
        )
        .all()
    )

    if not unresolved:
        print("Nothing to back-fill.")
        return

    # Group by (city, market_date, mode) to batch IEM fetches
    needed: dict[tuple, dict] = {}
    for snap in unresolved:
        key = (snap.city, snap.market_date)
        if key not in needed:
            needed[key] = {"max": None, "min": None, "fetched": False}

    print(f"Back-filling {len(unresolved)} snapshots across {len(needed)} city/date pairs …")

    for (city, obs_date) in needed:
        meta = CITY_META.get(city)
        if not meta:
            print(f"  ⚠️  Unknown city slug '{city}' — skipping")
            continue
        daily_max, daily_min = fetch_iem_obs(meta["station"], meta["tz"], obs_date)
        needed[(city, obs_date)]["max"] = daily_max
        needed[(city, obs_date)]["min"] = daily_min
        needed[(city, obs_date)]["fetched"] = True
        tag = f"{city} {obs_date}"
        print(f"  {tag}: high={daily_max}°F  low={daily_min}°F")

    # Pull WIN trade questions for the relevant city/dates to get winning_range
    win_lookup: dict[tuple, str | None] = {}
    for (city, obs_date) in needed:
        wins = (
            session.query(WeatherBotTsTradeHistory)
            .filter(
                WeatherBotTsTradeHistory.location == city,
                WeatherBotTsTradeHistory.market_date == obs_date,
                WeatherBotTsTradeHistory.realized_pnl > 0,
            )
            .all()
        )
        # There may be multiple modes (highest + lowest) — key by mode via question text
        for w in wins:
            mode = "highest" if ("highest" in (w.question or "").lower()) else "lowest"
            parsed = parse_winning_range(w.question or "")
            win_lookup[(city, obs_date, mode)] = parsed

    # Apply updates
    updated = 0
    for snap in unresolved:
        key = (snap.city, snap.market_date)
        d = needed.get(key, {})
        if not d.get("fetched"):
            continue
        actual = d["max"] if snap.mode == "highest" else d["min"]
        if actual is None:
            continue
        snap.actual_temp = Decimal(str(actual))
        wrange = win_lookup.get((snap.city, snap.market_date, snap.mode))
        if wrange:
            snap.winning_range = wrange
        updated += 1

    if dry_run:
        print(f"\nDry-run: would update {updated} snapshots. No commit.")
        session.rollback()
    else:
        session.commit()
        print(f"\n✅ Updated {updated} snapshots.")
    session.close()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Back-fill actual_temp + winning_range on signal snapshots")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = p.parse_args()
    backfill(dry_run=args.dry_run)
