#!/usr/bin/env python3
"""Capture each provider's day-18 month-end precipitation forecast per city.

Forward-capture leg of the PRECIPITATION provider matrix — the precip analogue
of capture_provider_forecasts.py. For every PRECIP_LOCATIONS city and every
ensemble provider, this records ONE month-end-total forecast snapshot at the
trade window (~day 18): accumulated actuals (days 1..yesterday) + the ensemble
distribution of remaining-month precip from the Open-Meteo ENSEMBLE API.

Why this exists / why it's forward-only (verified 2026-06-15):
  - The Open-Meteo Previous Runs API (which the TEMP matrix replays) is
    temperature-only — there is no archived precip-forecast source.
  - The Ensemble API serves per-member precip to >=20 forecast_days for 4
    providers at all 5 cities, but only for the live/current run.
  - So the matrix fills going forward, ~1 sample per city/provider/month.

Idempotent: a UNIQUE index on (location_id, settlement_month, provider,
asof_day) means a day-19/20 rerun no-ops if day-18 already captured. Run via
launchd on days 18/19/20 (com.sniff.precip-provider-matrix-rebuild), or:
    python3 capture_precip_ensemble.py
    python3 capture_precip_ensemble.py --asof 2026-06-18   # force a date
"""

from __future__ import annotations

import argparse
import datetime
import sys

import httpx

from database_schema import init_database, PrecipProviderForecast
from precip_forecast_pipeline import (
    PRECIP_LOCATIONS,
    OPEN_METEO_ENSEMBLE,
    ENSEMBLE_API_MODELS,
    MM_TO_INCHES,
    _month_bounds,
    _quantile,
    fetch_accumulated,
)

# Providers verified to return per-member precip at >=20 forecast_days for all
# 5 cities (2026-06-15). ecmwf_aifs025 is the AIFS ensemble (the deterministic
# ecmwf_aifs025_single caps at ~16d and cannot reach month-end on day 18).
PROVIDERS = ["ecmwf_ifs025", "ecmwf_aifs025", "gfs025", "icon_seamless", "gem_global"]


def fetch_provider_month_end(obs: dict, provider: str, today: datetime.date,
                             month_end: datetime.date, accumulated_mm: float
                             ) -> tuple[float, float, float, int] | None:
    """Ensemble month-end-total distribution (p05, p50, p95 mm) + member count.

    Sums each member's daily precip over today..month_end, adds the (certain)
    accumulated actuals, then takes percentiles across members.
    """
    days_remaining = (month_end - today).days + 1
    if days_remaining <= 0:
        return accumulated_mm, accumulated_mm, accumulated_mm, 0
    if provider not in ENSEMBLE_API_MODELS:
        return None
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(OPEN_METEO_ENSEMBLE, params={
                "latitude": obs["lat"],
                "longitude": obs["lon"],
                "daily": "precipitation_sum",
                "timezone": obs["timezone"],
                "models": provider,
                "forecast_days": min(days_remaining + 2, 35),
            })
            r.raise_for_status()
            daily = r.json().get("daily", {})
    except Exception as e:
        print(f"   ⚠️  {provider} ensemble error: {e}", file=sys.stderr)
        return None

    dates = daily.get("time", [])
    member_keys = [k for k in daily if "precipitation_sum" in k and k != "precipitation_sum"]
    if not dates or not member_keys:
        return None

    totals: list[float] = []
    for key in member_keys:
        vals = daily.get(key, [])
        rem = 0.0
        any_val = False
        for date_str, v in zip(dates, vals):
            d = datetime.date.fromisoformat(date_str)
            if today <= d <= month_end and v is not None:
                rem += v
                any_val = True
        if any_val:
            totals.append(accumulated_mm + rem)

    if not totals:
        return None
    totals.sort()
    return (
        round(_quantile(totals, 0.05), 3),
        round(_quantile(totals, 0.50), 3),
        round(_quantile(totals, 0.95), 3),
        len(totals),
    )


def capture(asof: datetime.date | None = None) -> int:
    # auto_migrate=False: the provider tables already exist (created at setup /
    # by the launchd rebuild). Running full migrate_schema() on every capture
    # races ALTER TABLE against live daemons and hits lock timeouts.
    session = init_database(auto_migrate=False)
    today = asof or datetime.date.today()
    month_start, month_end = _month_bounds(today)
    asof_day = today.day

    # Accumulated actuals (days 1..yesterday) are provider-independent — fetch once per city.
    inserted = 0
    skipped = 0
    print(f"Precip provider capture — asof {today} (day {asof_day}), month {month_start:%Y-%m}")
    for loc_id, obs in PRECIP_LOCATIONS.items():
        days_elapsed = today.day - 1
        accumulated_mm = 0.0
        if days_elapsed > 0:
            try:
                accumulated_mm = fetch_accumulated(obs, month_start, today - datetime.timedelta(days=1))
            except Exception as e:
                print(f"  {loc_id}: accumulated fetch failed ({e}); using 0", file=sys.stderr)

        for provider in PROVIDERS:
            dist = fetch_provider_month_end(obs, provider, today, month_end, accumulated_mm)
            if dist is None:
                print(f"  {loc_id:10s} {provider:14s}  no data — skipped")
                continue
            p05, p50, p95, n_members = dist

            # Idempotent upsert: the UNIQUE index guards re-runs within [18,20].
            existing = (
                session.query(PrecipProviderForecast)
                .filter_by(location_id=loc_id, settlement_month=month_start,
                           provider=provider, asof_day=asof_day)
                .first()
            )
            if existing is not None:
                skipped += 1
                continue

            session.add(PrecipProviderForecast(
                location_id       = loc_id,
                settlement_month  = month_start,
                asof_day          = asof_day,
                provider          = provider,
                accumulated_mm    = round(accumulated_mm, 3),
                remaining_p05_mm  = p05,
                remaining_p50_mm  = p50,
                remaining_p95_mm  = p95,
                total_forecast_mm = round(p50, 3),
                n_members         = n_members,
                units             = obs["units"],
                source            = "ensemble_capture",
            ))
            inserted += 1
            unit = '"' if obs["units"] == "inches" else "mm"
            disp = (p50 * MM_TO_INCHES) if obs["units"] == "inches" else p50
            print(f"  {loc_id:10s} {provider:14s}  month-end p50={disp:.2f}{unit}  members={n_members}")

    session.commit()
    session.close()
    print(f"\ncapture complete: inserted={inserted} skipped(existing)={skipped}")
    return inserted


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture day-18 ensemble precip forecasts per city/provider.")
    ap.add_argument("--asof", type=str, default=None,
                    help="ISO date to capture as-of (default: today). Use to force a specific day.")
    args = ap.parse_args()
    asof = datetime.date.fromisoformat(args.asof) if args.asof else None
    capture(asof)


if __name__ == "__main__":
    main()
