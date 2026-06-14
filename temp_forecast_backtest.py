#!/usr/bin/env python3
"""
Forecast provider accuracy backtest for Polymarket temperature markets.

Compares ECMWF IFS vs GFS (NWS/GFS proxy) forecast accuracy at ~24-48hr lead
time against resolved Polymarket daily high/low temperature markets for the 6
cities tracked by weatherbot-ts.

Usage:
    python temp_forecast_backtest.py --bias-report   # Provider accuracy table
    python temp_forecast_backtest.py --trade-sim     # Win-rate simulation per provider
    python temp_forecast_backtest.py --all           # Both reports
    python temp_forecast_backtest.py --days 90       # Limit lookback window (default 180)
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from collections import defaultdict
from typing import Optional

import httpx

from polymarket_dry_run import parse_temp_range, bucket_contains, MONTHS
from outcome_backfiller import OBSERVATORIES, fetch_daily_extremes

GAMMA_API = "https://gamma-api.polymarket.com"
HIST_FORECAST_API = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# The 6 weatherbot-ts cities: Polymarket slug → OBSERVATORIES key
BOT_CITIES: dict[str, dict] = {
    "miami":    {"slug": "miami",   "obs": "miami"},
    "new_york": {"slug": "nyc",     "obs": "new_york"},
    "chicago":  {"slug": "chicago", "obs": "chicago"},
    "dallas":   {"slug": "dallas",  "obs": "dallas"},
    "seattle":  {"slug": "seattle", "obs": "seattle"},
    "atlanta":  {"slug": "atlanta", "obs": "atlanta"},
}

PROVIDERS = [
    ("ecmwf", "ecmwf_ifs025"),
    ("gfs",   "gfs_seamless"),
]


# ── Data structures ────────────────────────────────────────────────────────────

class TempMarket:
    __slots__ = ("city", "date", "metric", "bucket_lo", "bucket_hi", "bucket_width", "yes_won", "question")

    def __init__(
        self,
        city: str,
        date: datetime.date,
        metric: str,
        bucket_lo: Optional[float],
        bucket_hi: Optional[float],
        yes_won: bool,
        question: str,
        bucket_width: Optional[float] = None,
    ) -> None:
        self.city = city
        self.date = date
        self.metric = metric
        self.bucket_lo = bucket_lo
        self.bucket_hi = bucket_hi
        self.bucket_width = bucket_width
        self.yes_won = yes_won
        self.question = question


# ── Gamma API helpers ──────────────────────────────────────────────────────────

def _parse_yes_won(market: dict) -> Optional[bool]:
    raw = market.get("outcomePrices", [])
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if len(prices) != 2:
            return None
        return float(prices[0]) > 0.5
    except Exception:
        return None


def _fetch_gamma_slug(client: httpx.Client, slug: str) -> list[dict]:
    for attempt in range(3):
        try:
            r = client.get(f"{GAMMA_API}/events", params={"slug": slug})
            if r.status_code == 429:
                time.sleep(min(30.0, 4 ** attempt))
                continue
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            if attempt == 2:
                return []
            time.sleep(min(5.0, 2 ** attempt))
    return []


def fetch_resolved_temp_markets(days_back: int = 180) -> list[TempMarket]:
    """
    Pull all resolved Polymarket temperature markets for the 6 bot cities
    using exact Gamma API slug lookup for each (city, metric, date).
    """
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days_back)
    markets: list[TempMarket] = []

    print(f"Scanning Gamma API for resolved temp markets ({cutoff} → {today - datetime.timedelta(days=1)})...")

    with httpx.Client(timeout=15) as client:
        for city_id, city_info in BOT_CITIES.items():
            city_slug = city_info["slug"]
            city_count = 0

            for metric in ("max", "min"):
                prefix = "highest" if metric == "max" else "lowest"
                d = cutoff
                while d < today:
                    month_name = MONTHS[d.month - 1]
                    slug = f"{prefix}-temperature-in-{city_slug}-on-{month_name}-{d.day}-{d.year}"
                    events = _fetch_gamma_slug(client, slug)

                    for ev in events:
                        for mkt in ev.get("markets", []):
                            yes_won = _parse_yes_won(mkt)
                            if yes_won is None:
                                continue
                            question = mkt.get("question", "")
                            lo, hi, width = parse_temp_range(question)
                            if lo is None and hi is None:
                                continue
                            markets.append(TempMarket(
                                city=city_id,
                                date=d,
                                metric=metric,
                                bucket_lo=lo,
                                bucket_hi=hi,
                                bucket_width=width,
                                yes_won=yes_won,
                                question=question,
                            ))
                            city_count += 1

                    d += datetime.timedelta(days=1)
                    time.sleep(0.08)

            print(f"  {city_id:<12}: {city_count} resolved bucket markets")

    return markets


# ── Open-Meteo forecast helpers ────────────────────────────────────────────────

def _fetch_hist_forecast(
    obs_id: str,
    date: datetime.date,
    model: str,
) -> dict[str, Optional[float]]:
    """
    Fetch daily max/min temperature forecast for (obs_id, date) from
    Open-Meteo Historical Forecast API (~24-48hr lead time seamless run).
    """
    obs = OBSERVATORIES[obs_id]
    temp_unit = "fahrenheit" if obs["units"] == "fahrenheit" else "celsius"

    params = {
        "latitude": obs["lat"],
        "longitude": obs["lon"],
        "start_date": date.isoformat(),
        "end_date": date.isoformat(),
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": temp_unit,
        "timezone": obs["timezone"],
        "models": model,
    }

    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(HIST_FORECAST_API, params=params)
            r.raise_for_status()
            data = r.json()

        daily = data.get("daily", {})
        max_vals = daily.get("temperature_2m_max", [None])
        min_vals = daily.get("temperature_2m_min", [None])
        return {
            "max": float(max_vals[0]) if max_vals and max_vals[0] is not None else None,
            "min": float(min_vals[0]) if min_vals and min_vals[0] is not None else None,
        }
    except Exception:
        return {"max": None, "min": None}


def build_forecast_cache(
    markets: list[TempMarket],
) -> dict[str, dict[str, dict[str, Optional[float]]]]:
    """
    Fetch ECMWF and GFS forecasts for all unique (city, date) pairs.
    Returns: cache[f"{city}|{date_iso}"][provider] = {"max": F, "min": F}
    """
    pairs = sorted({(m.city, m.date) for m in markets})
    total = len(pairs) * len(PROVIDERS)
    cache: dict = {}

    print(f"\nFetching forecasts ({len(pairs)} city×date pairs × {len(PROVIDERS)} models = {total} API calls)...")
    done = 0

    for (city_id, date) in pairs:
        obs_id = BOT_CITIES[city_id]["obs"]
        cache_key = f"{city_id}|{date.isoformat()}"
        cache[cache_key] = {}

        for provider, model in PROVIDERS:
            result = _fetch_hist_forecast(obs_id, date, model)
            cache[cache_key][provider] = result
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{total} fetched...")
            time.sleep(0.1)

    return cache


def build_actuals_cache(
    markets: list[TempMarket],
) -> dict[str, dict[str, Optional[float]]]:
    """Fetch ERA5 actual daily max/min for all unique (city, date) pairs."""
    pairs = sorted({(m.city, m.date) for m in markets})
    cache: dict = {}

    print(f"\nFetching ERA5 actuals ({len(pairs)} city×date pairs)...")

    for (city_id, date) in pairs:
        obs_id = BOT_CITIES[city_id]["obs"]
        max_t, min_t = fetch_daily_extremes(obs_id, date)
        cache[f"{city_id}|{date.isoformat()}"] = {"max": max_t, "min": min_t}
        time.sleep(0.1)

    return cache


# ── Reports ────────────────────────────────────────────────────────────────────

def run_bias_report(
    markets: list[TempMarket],
    forecast_cache: dict,
    actuals_cache: dict,
) -> None:
    """Print per-(provider, city, metric) bias and MAE against ERA5 actuals."""

    # Collect one error per unique (city, date, metric) — not per bucket
    unique_pairs = {(m.city, m.date, m.metric) for m in markets}
    stats: dict[tuple, list[float]] = defaultdict(list)

    for (city_id, date, metric) in unique_pairs:
        cache_key = f"{city_id}|{date.isoformat()}"
        actual = actuals_cache.get(cache_key, {}).get(metric)
        if actual is None:
            continue

        fc = forecast_cache.get(cache_key, {})
        for provider, _ in PROVIDERS:
            forecast = fc.get(provider, {}).get(metric)
            if forecast is None:
                continue
            stats[(provider, city_id, metric)].append(forecast - actual)  # positive = warm bias

    print("\n" + "=" * 80)
    print("  FORECAST PROVIDER ACCURACY — Historical Forecast vs ERA5 Actual (~24-48hr lead)")
    print("=" * 80)
    print(f"  {'Provider':<8} {'City':<12} {'Metric':<5} {'N':>4}  {'Mean Err':>9}  {'MAE':>7}  {'Warm%':>6}  {'Cold%':>6}")
    print(f"  {'-'*8} {'-'*12} {'-'*5} {'-'*4}  {'-'*9}  {'-'*7}  {'-'*6}  {'-'*6}")

    for metric in ("max", "min"):
        for city_id in BOT_CITIES:
            for provider, _ in PROVIDERS:
                errors = stats.get((provider, city_id, metric), [])
                n = len(errors)
                if n < 3:
                    continue
                mean_err = sum(errors) / n
                mae = sum(abs(e) for e in errors) / n
                warm_pct = 100 * sum(1 for e in errors if e > 0.5) / n
                cold_pct = 100 * sum(1 for e in errors if e < -0.5) / n
                print(
                    f"  {provider.upper():<8} {city_id:<12} {'max' if metric == 'max' else 'min':<5} "
                    f"{n:>4}  {mean_err:>+8.2f}°F  {mae:>6.2f}°F  {warm_pct:>5.0f}%  {cold_pct:>5.0f}%"
                )

    # Side-by-side advantage summary
    print(f"\n  ECMWF vs GFS — negative MAE diff means ECMWF is more accurate")
    print(f"  {'City':<12} {'Metric':<5}  {'MAE diff (ECMWF−GFS)':>22}  {'Mean err diff':>14}  {'N'}")
    print(f"  {'-'*12} {'-'*5}  {'-'*22}  {'-'*14}  {'-'*4}")

    for metric in ("max", "min"):
        for city_id in BOT_CITIES:
            ecmwf_e = stats.get(("ecmwf", city_id, metric), [])
            gfs_e   = stats.get(("gfs",   city_id, metric), [])
            n = min(len(ecmwf_e), len(gfs_e))
            if n < 3:
                continue
            mae_ecmwf = sum(abs(e) for e in ecmwf_e) / len(ecmwf_e)
            mae_gfs   = sum(abs(e) for e in gfs_e)   / len(gfs_e)
            diff_mae  = mae_ecmwf - mae_gfs
            diff_mean = (sum(ecmwf_e) / len(ecmwf_e)) - (sum(gfs_e) / len(gfs_e))
            print(
                f"  {city_id:<12} {'max' if metric == 'max' else 'min':<5}  "
                f"{diff_mae:>+21.2f}°F  {diff_mean:>+13.2f}°F  {n}"
            )


def run_trade_sim(
    markets: list[TempMarket],
    forecast_cache: dict,
    actuals_cache: dict,
) -> None:
    """
    Simulate win rates per forecast provider.
    For each resolved (city, date, metric): which bucket does the provider's
    forecast fall into, and did that bucket resolve YES?
    """
    # Group buckets by (city, date, metric)
    groups: dict[tuple, list[TempMarket]] = defaultdict(list)
    for m in markets:
        groups[(m.city, m.date, m.metric)].append(m)

    wins: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])  # [wins, total]
    no_bucket: dict[str, int] = defaultdict(int)

    for (city_id, date, metric), bucket_markets in groups.items():
        cache_key = f"{city_id}|{date.isoformat()}"
        fc = forecast_cache.get(cache_key, {})

        for provider, _ in PROVIDERS:
            forecast = fc.get(provider, {}).get(metric)
            if forecast is None:
                continue

            # Find the bucket this forecast falls into (half-open [lo, hi) for
            # finite buckets; inclusive bound for tails).
            predicted = None
            for bm in bucket_markets:
                if bucket_contains(bm.bucket_lo, bm.bucket_hi, bm.bucket_width, forecast):
                    predicted = bm
                    break

            if predicted is None:
                no_bucket[provider] += 1
                continue

            wins[(provider, city_id, metric)][1] += 1
            if predicted.yes_won:
                wins[(provider, city_id, metric)][0] += 1

    print("\n" + "=" * 80)
    print("  TRADE SIMULATION — Which bucket does each provider forecast select?")
    print("  (Win = forecast temperature falls in the bucket that resolved YES)")
    print("=" * 80)

    for metric in ("max", "min"):
        metric_label = "HIGH (max)" if metric == "max" else "LOW (min)"
        print(f"\n  Daily {metric_label} Temperature")
        print(f"  {'Provider':<8} {'City':<12} {'Wins':>5} {'Total':>6}  {'WR%':>6}")
        print(f"  {'-'*8} {'-'*12} {'-'*5} {'-'*6}  {'-'*6}")

        for city_id in BOT_CITIES:
            for provider, _ in PROVIDERS:
                w, t = wins.get((provider, city_id, metric), [0, 0])
                if t == 0:
                    continue
                wr = 100.0 * w / t
                print(f"  {provider.upper():<8} {city_id:<12} {w:>5} {t:>6}  {wr:>5.1f}%")

    # Aggregate comparison
    print(f"\n  Aggregate across all cities and metrics")
    print(f"  {'Provider':<8} {'Wins':>5} {'Total':>6}  {'WR%':>6}  {'No-bucket':>10}")
    for provider, _ in PROVIDERS:
        total_w = sum(v[0] for k, v in wins.items() if k[0] == provider)
        total_t = sum(v[1] for k, v in wins.items() if k[0] == provider)
        wr = 100.0 * total_w / total_t if total_t else 0.0
        nb = no_bucket.get(provider, 0)
        print(f"  {provider.upper():<8} {total_w:>5} {total_t:>6}  {wr:>5.1f}%  {nb:>10}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Weather forecast provider accuracy backtest")
    parser.add_argument("--bias-report", action="store_true", help="Provider accuracy vs ERA5")
    parser.add_argument("--trade-sim",   action="store_true", help="Win-rate simulation per provider")
    parser.add_argument("--all",         action="store_true", help="Run both reports")
    parser.add_argument("--days",        type=int, default=180, metavar="N",
                        help="Lookback window in days (default 180)")
    args = parser.parse_args()

    if not (args.bias_report or args.trade_sim or args.all):
        parser.print_help()
        sys.exit(1)

    do_bias = args.bias_report or args.all
    do_sim  = args.trade_sim   or args.all

    markets = fetch_resolved_temp_markets(days_back=args.days)
    print(f"\nTotal resolved bucket markets found: {len(markets)}")

    if not markets:
        print("No markets found — check Gamma API connectivity or try --days 365.")
        sys.exit(0)

    forecast_cache = build_forecast_cache(markets)
    actuals_cache  = build_actuals_cache(markets)

    if do_bias:
        run_bias_report(markets, forecast_cache, actuals_cache)
    if do_sim:
        run_trade_sim(markets, forecast_cache, actuals_cache)

    print()


if __name__ == "__main__":
    main()
