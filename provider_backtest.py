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

# US cities (°F) — mirrors src/nws.ts LOCATIONS / STATION_IDS (lat/lon are the
# airport stations). Keyed by the weatherbot-ts slug; actuals from IEM CLI.
# Each city carries `unit` ("F"|"C") so the bias cap and Open-Meteo fetch unit
# are chosen per city. Non-US cities (°C) below are keyed by the ECMWF bot's
# `location_id` and resolve actuals from the `outcomes` DB table (no CLI
# product); their coords mirror ecmwf_forecast_pipeline.OBSERVATORIES
# (verified observatory metadata 2026-05-31).
CITIES = {
    # ── US (°F, IEM CLI actuals) ──────────────────────────────────────────────
    "nyc":           {"lat": 40.7772, "lon": -73.8726,  "tz": "America/New_York",    "unit": "F", "station": "KLGA"},
    "chicago":       {"lat": 41.9742, "lon": -87.9073,  "tz": "America/Chicago",     "unit": "F", "station": "KORD"},
    "miami":         {"lat": 25.7959, "lon": -80.287,   "tz": "America/New_York",    "unit": "F", "station": "KMIA"},
    "dallas":        {"lat": 32.8471, "lon": -96.8518,  "tz": "America/Chicago",     "unit": "F", "station": "KDAL"},
    "seattle":       {"lat": 47.4502, "lon": -122.3088, "tz": "America/Los_Angeles", "unit": "F", "station": "KSEA"},
    "atlanta":       {"lat": 33.6407, "lon": -84.4277,  "tz": "America/New_York",    "unit": "F", "station": "KATL"},
    "houston":       {"lat": 29.6375, "lon": -95.2825,  "tz": "America/Chicago",     "unit": "F", "station": "KHOU"},  # Hobby — PM resolves here, not KIAH
    "denver":        {"lat": 39.7133, "lon": -104.7581, "tz": "America/Denver",      "unit": "F", "station": "KBKF"},  # Buckley SFB — PM resolves here, not KDEN
    "los-angeles":   {"lat": 34.0536, "lon": -118.2456, "tz": "America/Los_Angeles", "unit": "F", "station": "KLAX"},
    "san-francisco": {"lat": 37.7749, "lon": -122.4194, "tz": "America/Los_Angeles", "unit": "F", "station": "KSFO"},
    "austin":        {"lat": 30.2672, "lon": -97.7431,  "tz": "America/Chicago",     "unit": "F", "station": "KAUS"},
    # ── Non-US (°C, `outcomes` DB actuals) — keyed by ECMWF location_id ────────
    "shanghai":      {"lat": 31.1678, "lon": 121.4369,  "tz": "Asia/Shanghai",       "unit": "C", "db_actuals": True},
    "hong_kong":     {"lat": 22.3027, "lon": 114.1772,  "tz": "Asia/Hong_Kong",      "unit": "C", "db_actuals": True},
    "qingdao":       {"lat": 36.0667, "lon": 120.3333,  "tz": "Asia/Shanghai",       "unit": "C", "db_actuals": True},
    "beijing":       {"lat": 39.9333, "lon": 116.2833,  "tz": "Asia/Shanghai",       "unit": "C", "db_actuals": True},
    "london":        {"lat": 51.4700, "lon": -0.4543,   "tz": "Europe/London",       "unit": "C", "db_actuals": True},
    "paris":         {"lat": 48.8225, "lon": 2.3372,    "tz": "Europe/Paris",        "unit": "C", "db_actuals": True},
    "taipei":        {"lat": 25.0378, "lon": 121.5644,  "tz": "Asia/Taipei",         "unit": "C", "db_actuals": True},
    "lucknow":       {"lat": 26.7606, "lon": 80.8893,   "tz": "Asia/Kolkata",        "unit": "C", "db_actuals": True},
    "toronto":       {"lat": 43.6777, "lon": -79.6248,  "tz": "America/Toronto",     "unit": "C", "db_actuals": True},
    "buenos_aires":  {"lat": -34.5819, "lon": -58.4804, "tz": "America/Argentina/Buenos_Aires", "unit": "C", "db_actuals": True},
    "singapore":     {"lat": 1.3502,  "lon": 103.9894,  "tz": "Asia/Singapore",      "unit": "C", "db_actuals": True},
    "moscow":        {"lat": 55.8300, "lon": 37.6100,   "tz": "Europe/Moscow",       "unit": "C", "db_actuals": True},
    "tokyo":         {"lat": 35.6900, "lon": 139.7500,  "tz": "Asia/Tokyo",          "unit": "C", "db_actuals": True},
    "madrid":        {"lat": 40.4080, "lon": -3.6833,   "tz": "Europe/Madrid",       "unit": "C", "db_actuals": True},
    "shenzhen":      {"lat": 22.5493, "lon": 114.1138,  "tz": "Asia/Shanghai",       "unit": "C", "db_actuals": True},
    "chongqing":     {"lat": 29.5925, "lon": 106.4691,  "tz": "Asia/Shanghai",       "unit": "C", "db_actuals": True},
}

# Matrix-key aliases: a single computed cell is written under every key its
# consumers look up. weatherbot-ts reads by its LOCATIONS slug (e.g. "nyc");
# the ECMWF bot reads by its location_id (e.g. "new_york"). They agree on
# dallas/atlanta/austin but diverge on New York, so emit that cell under both.
MATRIX_ALIASES = {"nyc": "new_york"}

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


# DSN for the bot's postgres (non-US actuals live in `outcomes`). Mirrors the
# default used by capture_provider_forecasts.py / polymarket_dry_run.py.
DB_DSN = os.getenv("DATABASE_URL", "postgresql://padraighaughey@localhost:5432/gmgn_trading")


def fetch_actuals_db(location_id, start, end):
    """Resolved high/low per date from the `outcomes` table (Polymarket's settled
    values, the same source the ECMWF bot resolves on). Used for non-US cities
    that have no IEM CLI product. Values are already in the city's native unit
    (°C for non-US). Returns {date_iso: {"high": float|None, "low": float|None}}.
    """
    import psycopg2  # local import — only non-US runs need the DB
    out = {}
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DATE(date) AS d, actual_max_temp, actual_min_temp
                FROM outcomes
                WHERE location_id = %s AND DATE(date) BETWEEN %s AND %s
                """,
                (location_id, start.isoformat(), end.isoformat()),
            )
            for d, hi, lo in cur.fetchall():
                out[d.isoformat()] = {
                    "high": float(hi) if hi is not None else None,
                    "low": float(lo) if lo is not None else None,
                }
    finally:
        conn.close()
    return out


def fetch_forecasts(city_cfg, start, end, lead, models):
    """Per-model daily max/min built from previous-run hourly temps at the given lead.

    Forecast unit follows the city's `unit` so it matches the actuals unit
    (US °F via IEM CLI; non-US °C via the `outcomes` table) before scoring.
    """
    temp_unit = "celsius" if city_cfg.get("unit") == "C" else "fahrenheit"
    var = f"temperature_2m_previous_day{lead}"
    data = http_json(PREV_RUNS_URL, {
        "latitude": city_cfg["lat"], "longitude": city_cfg["lon"],
        "hourly": var, "models": ",".join(models),
        "temperature_unit": temp_unit, "timezone": city_cfg["tz"],
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


def bucket(t, unit="F"):
    """Polymarket bucket index. US °F buckets are 2° wide and even-aligned
    (78-79, 80-81, ...); non-US °C buckets are 1° wide (single-degree "be 26°C").
    Used only for the informational bucket_hit metric, not for selection."""
    width = 1 if unit == "C" else 2
    return int(round(t)) // width


def score(pairs, unit="F"):
    """pairs: list of (forecast, actual) in the city's native unit. Returns metric dict.
    `unit` only affects the informational bucket_hit width; MAE/bias are unit-native."""
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
        "bucket_hit": sum(bucket(f, unit) == bucket(a, unit) for f, a in pairs) / n,
    }


MATRIX_PATH = os.path.join(os.path.dirname(__file__), "provider_matrix.json")


# Debiased-MAE cap (in the city's native unit) a provider must clear to be selected.
# A provider whose typical error after debiasing exceeds ~one bucket cannot reliably
# land in the right bucket, so it is dropped and the next-smallest-MAE eligible provider
# takes the slot — falling through to "no cell" only when NONE clear it (the runtime gate
# then skips the city). Added 2026-06-16 after NYC + Dallas June-15 losses where the
# matrix-chosen provider's mae_debiased of 1.6–1.9°F still traded and missed by a bucket.
#   US (°F): 1.5°F.  Non-US (°C): 1.0°C.
# A separate |bias| <= 4°F/2°C cap previously ran alongside this one. Dropped 2026-06-19:
# debiasing already subtracts the bias before this cap is checked, so a high-bias/low-MAE
# provider (e.g. Miami AIFS: MAE 0.85, bias +4.97) is already corrected to a trustworthy
# debiased error — penalizing it twice for the same number was redundant gating, not an
# independent risk check.
MAX_MAE_F = 1.5
MAX_MAE_C = 1.0


def max_mae_for(unit: str) -> float:
    return MAX_MAE_C if unit == "C" else MAX_MAE_F


def build_matrix(results, incumbent: dict, min_samples: int = 5,
                 city_units: dict | None = None) -> dict:
    """Pick the best provider per (city, mode) from backtest results.

    7-day rolling selection (no hysteresis): the provider with the lowest
    debiased MAE that meets the sample gate wins outright, every rebuild.
    Responsiveness is the point — a short window must track regime shifts, so
    we deliberately do NOT keep an incumbent. The `incumbent` arg is retained
    only so the report can flag CHANGED cells.

    Gates (a provider must clear BOTH to be eligible):
      - sample gate: >= min_samples (default 5) forecast/actual pairs.
      - MAE gate: mae_debiased <= mae_cap (1.5°F US, 1.0°C non-US — see
        max_mae_for). The lowest-MAE provider that clears this cap wins; if
        even the best provider exceeds it, no provider is eligible and no cell
        is written (the runtime gate then skips that city).
    A city/mode where no provider clears both gates writes NO cell — getMae()
    then returns null in the bot, which means "don't gate, fall back to
    FORECAST_PROVIDER" (e.g. NWS, which has no archive and only accrues samples
    from the live capture table over time).

    Cells are also emitted under MATRIX_ALIASES keys so both the weatherbot-ts
    slug and the ECMWF location_id resolve the same cell.
    """
    city_units = city_units or {}
    cities_out = {}
    for city, modes in results.items():
        unit = city_units.get(city, "F")
        mae_cap = max_mae_for(unit)
        cities_out.setdefault(city, {})
        for mode, model_scores in modes.items():
            if not model_scores:
                continue
            # Lowest debiased MAE among providers clearing the sample + MAE gates.
            # If the smallest-MAE provider still exceeds mae_cap it is skipped and
            # the next-best ELIGIBLE provider takes the slot. Only when no provider
            # clears both gates is no cell written (the runtime gate then skips the city).
            ranked = sorted(model_scores.items(), key=lambda kv: kv[1]["mae_debiased"])
            best_model, best_metrics = next(
                ((m, s) for m, s in ranked
                 if s["n"] >= min_samples
                 and s["mae_debiased"] <= mae_cap),
                (None, None)
            )
            if best_model is None:
                continue  # no eligible provider — write no cell

            cities_out[city][mode] = {
                "provider": best_model,
                "bias": round(best_metrics["bias"], 2),
                "mae": round(best_metrics["mae"], 2),
                "mae_debiased": round(best_metrics["mae_debiased"], 2),
                "samples": best_metrics["n"],
                "unit": unit,
            }

    # Emit each cell under its alias key too, so both consumers resolve it.
    for canonical, alias in MATRIX_ALIASES.items():
        if cities_out.get(canonical):
            cities_out[alias] = cities_out[canonical]
    return cities_out


def load_incumbent_matrix() -> dict:
    if os.path.exists(MATRIX_PATH):
        with open(MATRIX_PATH) as f:
            return json.load(f).get("cities", {})
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
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
        unit = cfg.get("unit", "F")
        if cfg.get("db_actuals"):
            src = f"outcomes DB (°{unit})"
            actuals = fetch_actuals_db(city, start, end)
        else:
            src = f"{cfg['station']} CLI (°{unit})"
            actuals = fetch_actuals(cfg["station"], start, end)
        print(f"[{city}] actuals from {src} + {len(MODELS)} models "
              f"@ lead {args.lead}d, {start} → {end} ...", file=sys.stderr)
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
                m = score(pairs, unit)
                if m:
                    results[city][mode][model] = m
        time.sleep(1)  # be polite to free APIs

    meta = {"start": start.isoformat(), "end": end.isoformat(),
            "lead_days": args.lead, "models": MODELS,
            "actuals_source": "US: NWS CLI via IEM (°F); non-US: outcomes DB table (°C)"}
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
        city_units = {c: cfg.get("unit", "F") for c, cfg in CITIES.items()}
        matrix_cities = build_matrix(results, incumbent, city_units=city_units)
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
