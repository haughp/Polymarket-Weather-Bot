#!/usr/bin/env python3
"""Backtest forecast providers against official NWS CLI station actuals.

For each weatherbot city, pulls what every model *predicted* at ~1-day lead
(Open-Meteo Previous Runs API, archived since Jan 2024) and scores it against
the official NWS climate-report high/low for the resolution station (IEM CLI
archive) — i.e. the exact numbers Polymarket settles on.

This replaces weeks of live capture: an immediate apples-to-apples provider
comparison on the stations we trade.

Usage:
    python3 provider_backtest.py                  # last 90 days, all cities
    python3 provider_backtest.py --days 60 --lead 2
    python3 provider_backtest.py --cities nyc,miami
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

# Mirrors src/nws.ts LOCATIONS / STATION_IDS (lat/lon are the airport stations).
CITIES = {
    "nyc":           {"lat": 40.7772, "lon": -73.8726,  "tz": "America/New_York",    "station": "KLGA"},
    "chicago":       {"lat": 41.9742, "lon": -87.9073,  "tz": "America/Chicago",     "station": "KORD"},
    "miami":         {"lat": 25.7959, "lon": -80.287,   "tz": "America/New_York",    "station": "KMIA"},
    "dallas":        {"lat": 32.8471, "lon": -96.8518,  "tz": "America/Chicago",     "station": "KDAL"},
    "seattle":       {"lat": 47.4502, "lon": -122.3088, "tz": "America/Los_Angeles", "station": "KSEA"},
    "atlanta":       {"lat": 33.6407, "lon": -84.4277,  "tz": "America/New_York",    "station": "KATL"},
    "houston":       {"lat": 29.6375, "lon": -95.2825,  "tz": "America/Chicago",     "station": "KHOU"},  # Hobby — PM resolves here, not KIAH
    "denver":        {"lat": 39.7133, "lon": -104.7581, "tz": "America/Denver",      "station": "KBKF"},  # Buckley SFB — PM resolves here, not KDEN
    "los-angeles":   {"lat": 34.0536, "lon": -118.2456, "tz": "America/Los_Angeles", "station": "KLAX"},
    "san-francisco": {"lat": 37.7749, "lon": -122.4194, "tz": "America/Los_Angeles", "station": "KSFO"},
    "austin":        {"lat": 30.2672, "lon": -97.7431,  "tz": "America/Chicago",     "station": "KAUS"},
}

MODELS = [
    "ecmwf_ifs025",         # ECMWF IFS 0.25° (what NYC now uses)
    "ecmwf_aifs025_single", # ECMWF AIFS — AI model, operational since Feb 2025
    "ncep_nbm_conus",       # NOAA National Blend of Models — closest proxy for NWS point forecast
    "gfs_seamless",         # NOAA GFS (+HRRR blend at short range)
    "icon_seamless",        # DWD ICON
    "gem_seamless",         # Canadian GEM
    "ukmo_seamless",        # UK Met Office
]

PREV_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
IEM_CLI_URL = "https://mesonet.agron.iastate.edu/json/cli.py"
IEM_DAILY_URL = "https://mesonet.agron.iastate.edu/api/1/daily.json"

# ASOS network per station, for the METAR-daily fallback when a station has
# no CLI product (e.g. KDAL — Love Field gets no climate report).
ASOS_NETWORKS = {"KDAL": "TX_ASOS", "KBKF": "CO_ASOS"}  # KBKF (Buckley SFB) has no CLI product


def http_json(url, params, retries=3):
    qs = urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(f"{url}?{qs}", timeout=60) as r:
                return json.load(r)
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def fetch_actuals(station, start, end):
    """Official CLI climate-report high/low per date — Polymarket's resolution source."""
    out = {}
    for year in range(start.year, end.year + 1):
        data = http_json(IEM_CLI_URL, {"station": station, "year": year})
        for row in data.get("results", []):
            d = row.get("valid")
            if d and start.isoformat() <= d <= end.isoformat():
                hi, lo = row.get("high"), row.get("low")
                out[d] = {
                    "high": hi if isinstance(hi, (int, float)) else None,
                    "low": lo if isinstance(lo, (int, float)) else None,
                }
    if not out and station in ASOS_NETWORKS:
        # No CLI product for this station — fall back to IEM daily summaries
        # (METARs grouped by local day; matches Wunderground settlement).
        cur = start.replace(day=1)
        months = set()
        while cur <= end:
            months.add((cur.year, cur.month))
            cur = (cur.replace(day=28) + timedelta(days=5)).replace(day=1)
        for year, month in sorted(months):
            data = http_json(IEM_DAILY_URL, {
                "station": station.lstrip("K"), "network": ASOS_NETWORKS[station],
                "year": year, "month": month,
            })
            for row in data.get("data", []):
                d = row.get("date")
                if d and start.isoformat() <= d <= end.isoformat():
                    out[d] = {"high": row.get("max_tmpf"), "low": row.get("min_tmpf")}
    return out


def fetch_forecasts(city_cfg, start, end, lead, models):
    """Per-model daily max/min built from previous-run hourly temps at the given lead."""
    var = f"temperature_2m_previous_day{lead}"
    data = http_json(PREV_RUNS_URL, {
        "latitude": city_cfg["lat"], "longitude": city_cfg["lon"],
        "hourly": var, "models": ",".join(models),
        "temperature_unit": "fahrenheit", "timezone": city_cfg["tz"],
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    })
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    out = {}  # model -> date -> {fmax, fmin}
    for model in models:
        key = f"{var}_{model}" if len(models) > 1 else var
        vals = hourly.get(key, [])
        days = defaultdict(list)
        for t, v in zip(times, vals):
            if v is not None:
                days[t[:10]].append(v)
        out[model] = {d: {"fmax": max(vs), "fmin": min(vs)} for d, vs in days.items() if len(vs) >= 20}
    return out


def bucket(t):
    """Polymarket 2°F buckets are even-aligned (78-79, 80-81, ...)."""
    return int(round(t)) // 2


def score(pairs):
    """pairs: list of (forecast, actual). Returns metric dict."""
    if not pairs:
        return None
    errs = [a - f for f, a in pairs]
    n = len(errs)
    mean_e = sum(errs) / n
    # Debiased = after removing the constant station offset (the bot already
    # applies per-city bias correction, so this is the deployable skill).
    derrs = [e - mean_e for e in errs]
    return {
        "n": n,
        "mae": sum(abs(e) for e in errs) / n,
        "bias": mean_e,
        "mae_debiased": sum(abs(e) for e in derrs) / n,
        "within1": sum(abs(e) <= 1 for e in errs) / n,
        "within3": sum(abs(e) <= 3 for e in errs) / n,
        "within1_db": sum(abs(e) <= 1 for e in derrs) / n,
        "bucket_hit": sum(bucket(f) == bucket(a) for f, a in pairs) / n,
    }


MATRIX_PATH = os.path.join(os.path.dirname(__file__), "provider_matrix.json")


def build_matrix(results, incumbent: dict, min_samples: int = 30,
                 improvement_threshold: float = 0.03) -> dict:
    """Pick the best provider per (city, mode) from backtest results.

    Promotion gate: only switch from incumbent if the new winner has
    >= min_samples AND its debiased MAE is > improvement_threshold better
    than the incumbent provider's MAE. This avoids noisy churn on thin data.
    """
    cities_out = {}
    for city, modes in results.items():
        cities_out[city] = {}
        for mode, model_scores in modes.items():
            if not model_scores:
                continue
            # Find the best model meeting the sample gate
            ranked = sorted(model_scores.items(), key=lambda kv: kv[1]["mae_debiased"])
            best_model, best_metrics = next(
                ((m, s) for m, s in ranked if s["n"] >= min_samples), (None, None)
            )
            if best_model is None:
                continue

            # Check incumbent
            inc_cell = incumbent.get(city, {}).get(mode, {})
            inc_provider = inc_cell.get("provider")
            inc_mae = inc_cell.get("mae_debiased", float("inf"))

            # If incumbent is still one of the scored models, use its current MAE
            if inc_provider and inc_provider in model_scores:
                inc_mae = model_scores[inc_provider].get("mae_debiased", inc_mae)

            new_mae = best_metrics["mae_debiased"]
            if inc_provider and new_mae >= inc_mae * (1 - improvement_threshold):
                # Not enough improvement — keep incumbent provider but refresh bias
                provider = inc_provider
                m = model_scores.get(inc_provider, best_metrics)
            else:
                provider = best_model
                m = best_metrics

            cities_out[city][mode] = {
                "provider": provider,
                "bias": round(m["bias"], 2),
                "mae": round(m["mae"], 2),
                "mae_debiased": round(m["mae_debiased"], 2),
                "samples": m["n"],
            }
    return cities_out


def load_incumbent_matrix() -> dict:
    if os.path.exists(MATRIX_PATH):
        with open(MATRIX_PATH) as f:
            return json.load(f).get("cities", {})
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--lead", type=int, default=1, choices=range(1, 8),
                    help="forecast lead in days (previous_dayN)")
    ap.add_argument("--cities", default=None, help="comma list, default all")
    ap.add_argument("--out", default="provider_backtest_results.json")
    ap.add_argument("--matrix", default=True, action=argparse.BooleanOptionalAction,
                    help="write provider_matrix.json (default: True)")
    args = ap.parse_args()

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days - 1)
    cities = args.cities.split(",") if args.cities else list(CITIES)

    results = {}  # city -> mode -> model -> metrics
    for city in cities:
        cfg = CITIES[city]
        print(f"[{city}] actuals from {cfg['station']} CLI + {len(MODELS)} models "
              f"@ lead {args.lead}d, {start} → {end} ...", file=sys.stderr)
        actuals = fetch_actuals(cfg["station"], start, end)
        forecasts = fetch_forecasts(cfg, start, end, args.lead, MODELS)
        results[city] = {}
        for mode, fkey, akey in (("max", "fmax", "high"), ("min", "fmin", "low")):
            results[city][mode] = {}
            for model in MODELS:
                pairs = []
                for d, act in actuals.items():
                    fc = forecasts.get(model, {}).get(d)
                    if fc and act[akey] is not None:
                        pairs.append((fc[fkey], act[akey]))
                m = score(pairs)
                if m:
                    results[city][mode][model] = m
        time.sleep(1)  # be polite to free APIs

    meta = {"start": start.isoformat(), "end": end.isoformat(),
            "lead_days": args.lead, "models": MODELS,
            "actuals_source": "NWS CLI climate reports via IEM (resolution source)"}
    with open(args.out, "w") as f:
        json.dump({"meta": meta, "results": results}, f, indent=2)

    # Report: per city/mode, models ranked by MAE
    print(f"\nProvider backtest {start} → {end}, lead {args.lead}d, "
          f"actuals = official CLI reports\n")
    overall = defaultdict(lambda: defaultdict(list))  # mode -> model -> maes
    for city in cities:
        for mode in ("max", "min"):
            rows = results[city].get(mode, {})
            if not rows:
                continue
            ranked = sorted(rows.items(), key=lambda kv: kv[1]["mae_debiased"])
            print(f"{city} {mode}  (n={ranked[0][1]['n']})")
            for model, m in ranked:
                print(f"  {model:<22} MAE* {m['mae_debiased']:.2f}  raw {m['mae']:.2f}  "
                      f"bias {m['bias']:+.2f}  ±1°F* {m['within1_db']*100:4.0f}%  "
                      f"±3°F {m['within3']*100:4.0f}%  bucket {m['bucket_hit']*100:4.0f}%")
                overall[mode][model].append(m["mae_debiased"])
            print()

    for mode in ("max", "min"):
        print(f"=== OVERALL {mode} (mean debiased MAE across cities) ===")
        agg = sorted(((sum(v)/len(v), k) for k, v in overall[mode].items()))
        for mae, model in agg:
            print(f"  {model:<22} {mae:.2f}")
        print()

    if args.matrix:
        incumbent = load_incumbent_matrix()
        matrix_cities = build_matrix(results, incumbent)
        matrix = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_days": args.days,
            "lead_days": args.lead,
            "cities": matrix_cities,
        }
        with open(MATRIX_PATH, "w") as f:
            json.dump(matrix, f, indent=2)
        print(f"\n✅ provider_matrix.json written ({len(matrix_cities)} cities)")
        print("\nProvider matrix summary:")
        for city in sorted(matrix_cities):
            for mode in ("max", "min"):
                cell = matrix_cities[city].get(mode, {})
                if cell:
                    inc = incumbent.get(city, {}).get(mode, {}).get("provider", "—")
                    changed = " ← CHANGED" if cell["provider"] != inc else ""
                    print(f"  {city:<14} {mode}  {cell['provider']:<22} "
                          f"MAE* {cell['mae_debiased']:.2f}  bias {cell['bias']:+.2f}{changed}")


if __name__ == "__main__":
    main()
