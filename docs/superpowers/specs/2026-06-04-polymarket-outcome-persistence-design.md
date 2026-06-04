# Polymarket Outcome Persistence System — Design Spec

**Date:** 2026-06-04  
**Status:** Approved for implementation

---

## Overview

Persist Polymarket-resolved temperature outcomes per city/date/metric to a new `polymarket_temp_outcomes` table, enabling empirical measurement of ECMWF forecast accuracy and ongoing recalibration of `FORECAST_BIAS` in both bots.

The source of truth is the Polymarket-settled YES bucket (not ERA5). The midpoint of the winning bucket is `polymarket_actual`. This is more city-specific than ERA5 reanalysis, particularly for coastal markets (Miami) where ERA5 smooths out ASOS/local effects.

---

## What Gets Built

One script: `temp_forecast_backtest.py` is stripped of diagnostic functions and redefined as an operational persistence tool with three CLI modes.

One new DB table: `polymarket_temp_outcomes`.

One migration entry in `migrate_schema()` in `database_schema.py`.

No changes to running daemons, `nws.ts`, `polymarket_dry_run.py`, or `ecmwf_forecast_pipeline.py`.

---

## Script Changes — What Is Stripped vs Kept

### Strip (diagnostic only, no DB writes)
- `run_bias_report()` — ERA5-comparison print table
- `run_trade_sim()` — win-rate simulation print output
- `build_actuals_cache()` — ERA5 fetching via `outcome_backfiller.py`
- `PROVIDERS` list — drop GFS; ECMWF only
- `--bias-report`, `--trade-sim`, `--all` CLI flags

### Keep (reuse directly)
- `TempMarket` class
- `_parse_yes_won()`, `_fetch_gamma_slug()`
- `fetch_resolved_temp_markets()` — unchanged
- `_fetch_hist_forecast()` — ECMWF only (remove `model` parameter; hardcode `ecmwf_ifs025`)

### Redefine
- `build_forecast_cache()` → `_resolve_ecmwf_forecast(city_id, date)`:
  - Tries `Forecast` table first (`SELECT forecast_temp FROM forecasts WHERE location_id = ? AND DATE(peak_time) = ? AND mode = ? ORDER BY created_at DESC LIMIT 1`)
  - Falls back to Open-Meteo Historical Forecast API (`HIST_FORECAST_API = "https://historical-forecast-api.open-meteo.com/v1/forecast"`)
  - Returns `(value, source)` where source is `"live_db"` or `"historical_api"`
- `main()` → three modes described below

---

## DB Table: `polymarket_temp_outcomes`

One row per (city, date, metric). Unique constraint prevents duplicates on re-run.

```python
class PolymarketTempOutcome(Base):
    __tablename__ = "polymarket_temp_outcomes"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    location_id       = Column(String(32), index=True)          # miami, new_york, etc.
    market_date       = Column(Date, index=True)
    mode              = Column(String(8), index=True)            # max / min
    winning_lo        = Column(Numeric(4, 1), nullable=True)     # lower bound of YES bucket (NULL = "below X")
    winning_hi        = Column(Numeric(4, 1), nullable=True)     # upper bound (NULL = "above X")
    polymarket_actual = Column(Numeric(4, 1))                    # midpoint of winning bucket
    ecmwf_forecast    = Column(Numeric(4, 1), nullable=True)     # Open-Meteo ecmwf_ifs025 for that date
    db_forecast       = Column(Numeric(4, 1), nullable=True)     # Forecast table value (NULL if pre-collection)
    forecast_source   = Column(String(16))                       # "live_db" or "historical_api" (ecmwf_forecast provenance)
    forecast_error    = Column(Numeric(4, 1), nullable=True)     # polymarket_actual − ecmwf_forecast
    created_at        = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__    = (UniqueConstraint("location_id", "market_date", "mode"),)
```

### Bias convention
`forecast_error = polymarket_actual − ecmwf_forecast`

Positive → ECMWF ran cold (actual was warmer). Same sign convention as `FORECAST_BIAS` in both bots: add a positive bias to the forecast to correct.

### Calibration query (usable once ≥10 rows per cell)
```sql
SELECT location_id, mode,
       AVG(forecast_error) AS mean_error,
       COUNT(*)            AS n
FROM polymarket_temp_outcomes
GROUP BY location_id, mode
HAVING COUNT(*) >= 10
ORDER BY location_id, mode;
```

### Midpoint derivation for open-ended buckets
- Both bounds present: `midpoint = (lo + hi) / 2`
- Only `hi` (e.g. "below 72°F"): `midpoint = hi − 1.0` (conservative; flag in `forecast_source` notes)
- Only `lo` (e.g. "above 90°F"): `midpoint = lo + 1.0` (conservative)

---

## CLI Modes

### `--sniff [N=20]`
Validates timing alignment before committing to a full backfill.

1. Pull last N resolved markets from Gamma API.
2. For each, look up the matching `Forecast` table row (live DB).
3. Also call Open-Meteo Historical Forecast API for the same (city, date, mode).
4. Print a comparison table: `DB forecast | API forecast | diff`.
5. Report mean absolute difference and verdict: **PASS** (MAE < 1.0°F) or **FAIL**.
6. Writes nothing to DB. Exit only.

If sniff FAILS, the historical API is not a valid proxy for the live forecast. Investigate before running `--backfill`.

### `--backfill [DAYS=180]`
Retrospective population of `polymarket_temp_outcomes`.

1. `fetch_resolved_temp_markets(days_back=DAYS)` — all resolved bucket markets.
2. For each unique (city, date, metric):
   - Find the YES-winning bucket. Skip if no bucket won (all prices ~0.5 = unresolved).
   - Compute `polymarket_actual` from midpoint.
   - Call `_resolve_ecmwf_forecast()` → try DB first, fall back to API.
   - Insert row via SQLAlchemy `INSERT ... ON CONFLICT DO NOTHING` (idempotent).
3. Print progress and final row count.

### `--nightly`
Captures yesterday's settled markets. Designed for cron.

Identical to `--backfill 2` in logic but named distinctly so it's clear in launchd output. Exits 0 on success, 1 on API failure (to surface in cron logs).

---

## Migration

Add to `migrate_schema()` in `database_schema.py`:

```python
conn.execute(text("""
    CREATE TABLE IF NOT EXISTS polymarket_temp_outcomes (
        id                SERIAL PRIMARY KEY,
        location_id       VARCHAR(32) NOT NULL,
        market_date       DATE NOT NULL,
        mode              VARCHAR(8) NOT NULL,
        winning_lo        NUMERIC(4,1),
        winning_hi        NUMERIC(4,1),
        polymarket_actual NUMERIC(4,1),
        ecmwf_forecast    NUMERIC(4,1),
        db_forecast       NUMERIC(4,1),
        forecast_source   VARCHAR(16),
        forecast_error    NUMERIC(4,1),
        created_at        TIMESTAMP NOT NULL DEFAULT now(),
        CONSTRAINT uq_polymarket_temp_outcomes
            UNIQUE (location_id, market_date, mode)
    )
"""))
conn.execute(text(
    "CREATE INDEX IF NOT EXISTS ix_pto_location_date ON polymarket_temp_outcomes (location_id, market_date)"
))
```

---

## Column Provenance — Data Availability Check

| Column | Source | Available for backfill? |
|--------|--------|------------------------|
| `location_id` | `TempMarket.city` | Yes — from Gamma API slug |
| `market_date` | `TempMarket.date` | Yes |
| `mode` | `TempMarket.metric` | Yes (`max`/`min`) |
| `winning_lo` / `winning_hi` | `TempMarket.bucket_lo/.bucket_hi` (from `parse_temp_range()`) | Yes |
| `polymarket_actual` | Derived midpoint from winning YES bucket | Yes |
| `ecmwf_forecast` | `_fetch_hist_forecast(obs_id, date, "ecmwf_ifs025")` | Yes — Historical Forecast API (ECMWF from Jan 2024) |
| `db_forecast` | `Forecast.forecast_temp` WHERE date matches | Partial — only dates since ECMWF pipeline started collecting |
| `forecast_source` | `"live_db"` if DB row found, else `"historical_api"` | Derived |
| `forecast_error` | `polymarket_actual − ecmwf_forecast` | Derived (NULL if `ecmwf_forecast` is NULL) |

---

## Timing Alignment — Critical Constraint

The live ECMWF pipeline calls `api.open-meteo.com/v1/forecast` with `temperature_2m_max` for `forecast_date = tomorrow` using `models=ecmwf_ifs025`. The historical backfill uses `historical-forecast-api.open-meteo.com/v1/forecast` with the same variables for a past date.

The sniff test (`--sniff`) is the gate: it directly compares what the live bot stored in `Forecast.forecast_temp` against what the Historical Forecast API returns for the same (city, date, mode). If these align within 1°F MAE on a 20-market sample, the historical API is a valid proxy and the backfill is safe to run. If they diverge, we investigate whether to switch to `previous-runs-api.open-meteo.com` with `_previous_day1` suffix before proceeding.

**Do not run `--backfill` before running `--sniff` and confirming PASS.**

---

## Out of Scope

- Adding `corrected_temp` column to `TradeSimulation` or `weatherbot_ts_trade_history` (separate migration)
- Modifying `FORECAST_BIAS` values based on outcomes (manual step after ≥10 rows per cell)
- GFS comparison (dropped — ECMWF only for this system)
- Automated bias recalibration (human review step intentionally preserved)
