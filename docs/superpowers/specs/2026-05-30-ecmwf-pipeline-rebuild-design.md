# ECMWF Pipeline Rebuild — Design Spec
_Date: 2026-05-30_

## Problem Statement

The ECMWF dry-run simulator has produced zero trades for 7+ consecutive days despite being "alive" (launchd shows the process running). Three independent root causes:

1. **Process crash**: An uncaught exception in `run_ecmwf_loop.py` kills the `while True:` loop. launchd restarts, the pipeline runs once, then dies again. Every "loop tick" is actually a cold restart.
2. **Entry window never matches**: The 30–36h entry window is defined relative to each city's `peak_time`, but with the loop dying after one run, cities never get a price scan during their window.
3. **Circular outcome verification**: Both forecast (`ecmwf_forecast_pipeline.py`) and outcome backfiller (`outcome_backfiller.py`) use Open-Meteo at the same coordinates. For US cities, Polymarket resolves against NWS/NOAA instrument readings — our simulated win/loss rates don't reflect what Polymarket actually settles.

---

## Scope

### In scope
- Workstream 1: Loop stability and 30-minute rescan cadence
- Workstream 2: Resolution source alignment for US cities (forecast + outcome)
- Workstream 3: Coordinate audit for all 20 cities against Polymarket resolution sources

### Out of scope
- Settlement rounding for single-degree markets — already fixed in `outcome_backfiller.py:368-370`
- TypeScript weatherbot-ts — separate system
- Precipitation pipeline — not broken, not touched
- Live execution (real orders) — this is a dry-run simulator rebuild

---

## Workstream 1 — Loop Stability + 30-Minute Rescan

**File:** `run_ecmwf_loop.py`

### Root cause

`_run_pipeline()` wraps each *stage* in try/except, but `importlib.reload()` calls and inter-stage code are unprotected. An exception there propagates to `main()`, which has no try/except. The process dies. launchd KeepAlive restarts it, it runs once (the initial `_run_pipeline(full_refresh=True)` before the loop), enters `while True:`, logs one "Loop tick", sleeps up to 60 minutes, then fails again on the next pipeline run.

### Changes

**Change 1 — Halve the max sleep interval**

```python
# before
sleep_secs = min(3600, max(60, secs_to_ecmwf))

# after
sleep_secs = min(1800, max(60, secs_to_ecmwf))
```

Effect: pending-city price scans run at most 30 minutes apart. Cities inside their 6h entry window get ~12 chances to place a trade (vs ~6 before).

**Change 2 — Crash-safe loop body**

```python
while True:
    try:
        log.info("─── Loop tick — checking wake schedule ───")
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        secs_to_ecmwf = (next_ecmwf - now).total_seconds()
        sleep_secs = min(1800, max(60, secs_to_ecmwf))

        next_wake = now + datetime.timedelta(seconds=sleep_secs)
        log.info(
            "Next wake in %.1fh at %s UTC  (next ECMWF refresh at %s UTC)",
            sleep_secs / 3600,
            next_wake.strftime("%Y-%m-%d %H:%M"),
            next_ecmwf.strftime("%Y-%m-%d %H:%M"),
        )
        time.sleep(sleep_secs)

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        do_full = now >= next_ecmwf
        _run_pipeline(full_refresh=do_full)
        if do_full:
            next_ecmwf = _next_ecmwf_utc()

    except Exception:
        log.exception("Loop body crashed — sleeping 60s before retry")
        time.sleep(60)
```

Effect: any unhandled exception is logged and absorbed. The loop continues. No launchd restart needed.

### Cadence after fix

| Event | Interval |
|---|---|
| ECMWF forecast refresh (Stage 1) | ~6h — aligned to 04:30/10:30/16:30/22:30 UTC |
| Polymarket price scan for pending cities (Stage 2) | ≤30 min |
| Outcome backfill + PnL settlement (Stages 4–5) | On each full refresh |

---

## Workstream 2 — Resolution Source Alignment

### Background

For US cities, Polymarket resolves temperature markets against NWS/NOAA official station readings (the actual thermometer measurement). Open-Meteo's ECMWF model output at those coordinates is a gridded interpolation — it can diverge 1–3°F from the official reading. 1–3°F is exactly the margin that determines bucket hits.

The weatherbot-ts (TypeScript, live) already routes US cities through NWS Point Forecast API. The Python ECMWF pipeline should do the same.

**US cities affected:** `dallas`, `new_york`, `atlanta`, `chicago`, `miami`, `seattle`, `austin` (7 of 20)

### 2a — Forecast source: `ecmwf_forecast_pipeline.py`

Add NWS Point Forecast API routing for US cities. NWS API is free, no key required.

```python
US_CITIES = frozenset({
    'dallas', 'new_york', 'atlanta', 'chicago', 'miami', 'seattle', 'austin'
})

def fetch_nws_forecast(lat: float, lon: float, target_date: datetime.date,
                       units: str) -> tuple[float | None, datetime.datetime | None]:
    """
    NWS Point Forecast API → daily max temp for target_date.
    Returns (forecast_temp_rounded, peak_time) or (None, None) on failure.
    """
    points_url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
    # → forecast grid URL → parse periods for target_date daytime high
    ...
```

`extract_forecast()` dispatches:

```python
def extract_forecast(location_id, obs, target_date):
    if location_id in US_CITIES:
        return fetch_nws_forecast(obs['lat'], obs['lon'], target_date, obs['units'])
    else:
        return fetch_open_meteo_forecast(obs['lat'], obs['lon'], target_date, obs['units'])
```

Non-US cities: unchanged (Open-Meteo ECMWF).

### 2b — Outcome source: `outcome_backfiller.py`

Replace Open-Meteo with NOAA CDO API (GHCND daily) for US cities. GHCND returns the actual ASOS station readings — the same data NWS publishes as official records.

**Requires:** Free NOAA CDO token. Request at `ncdc.noaa.gov/cdo-web/token` (email, instant). Store as env var `NOAA_CDO_TOKEN`.

**GHCND station IDs:**

| location_id | GHCND Station ID | Station name |
|---|---|---|
| new_york | USW00094728 | NY Central Park ASOS |
| atlanta | USW00013874 | Hartsfield-Jackson Intl |
| dallas | USW00003927 | Dallas/Fort Worth Intl |
| chicago | USW00094846 | O'Hare Intl |
| miami | USW00012839 | Miami Intl |
| seattle | USW00024233 | Seattle-Tacoma Intl |
| austin | USW00013958 | Austin-Bergstrom Intl |

**CDO endpoint:**
```
GET https://www.ncdc.noaa.gov/cdo-web/api/v2/data
  ?datasetid=GHCND
  &stationid=GHCND:{station_id}
  &startdate={date}
  &enddate={date}
  &datatypeid=TMAX,TMIN
  &units=standard          # Fahrenheit for US
```
Header: `token: {NOAA_CDO_TOKEN}`

TMAX/TMIN are returned in tenths of degrees Fahrenheit → divide by 10.

`fetch_daily_extremes()` dispatches:
```python
US_GHCND = {
    'new_york': 'USW00094728',
    'atlanta':  'USW00013874',
    ...
}

def fetch_daily_extremes(location_id, date):
    if location_id in US_GHCND:
        return _fetch_cdo_extremes(US_GHCND[location_id], date, units='fahrenheit')
    else:
        return _fetch_open_meteo_extremes(OBSERVATORIES[location_id], date)
```

**Fallback:** If CDO token is missing or CDO returns an error, fall back to Open-Meteo and log a warning. Prevents total backfill failure if the token expires.

---

## Workstream 3 — Coordinate Audit

### Objective

Confirm that each city's `(lat, lon)` in `OBSERVATORIES` is the correct official station that Polymarket's market resolution clause cites. A 0.5° coordinate error at mid-latitudes is ~50km — a different station, a different climate pocket.

### Process

For each of the 20 cities:
1. Pull a live Polymarket temperature market for that city
2. Extract the resolution clause (typically "…will resolve YES if the official high temperature reading from [source] is…")
3. Look up official coordinates for that station from the relevant authority (NWS, HKO, CMA, JMA, Met Office, etc.)
4. Compare against `OBSERVATORIES`
5. Patch mismatches in both `ecmwf_forecast_pipeline.py` and `outcome_backfiller.py`

### Known risk cases

| City | Risk | Current coords |
|---|---|---|
| hong_kong | HKO station is at ~(22.302, 114.174) — close but unverified | (22.3027, 114.1772) |
| seoul | Flagged in experiment log as ERA5 fallback (not KMA) — may not be in markets at all | N/A |
| shanghai | Xujiahui station — CMA-official but coordinates need confirmation | (31.1678, 121.4369) |
| All 7 US | NOAA ASOS station coordinates — should be confirmed against NOAA station metadata | Various |

### Outcome

One patch commit updating any mismatched coordinates in both files simultaneously. No coordinate should diverge by more than 0.05° from the official station.

---

## Data flow after rebuild

```
Every 30 min (loop wake):
  ├── If past ECMWF window:
  │     Stage 1: fetch forecast
  │       US cities → NWS Point Forecast API
  │       non-US   → Open-Meteo ECMWF
  │     Stage 3: precipitation pipeline (unchanged)
  │     Stage 4: backfill outcomes
  │       US cities → NOAA CDO (GHCND)
  │       non-US   → Open-Meteo Archive
  │     Stage 5: settle PnL
  └── Always:
        Stage 2: scan Polymarket prices for pending cities
          → classify_markets() picks top-2 by midpoint distance (unchanged)
          → entry window gate (30–36h)
          → record dry-run trade if gate passes
```

---

## Files changed

| File | Workstream | Change |
|---|---|---|
| `run_ecmwf_loop.py` | 1 | Try/except around loop body; `min(1800, ...)` sleep cap |
| `ecmwf_forecast_pipeline.py` | 2a | Add `fetch_nws_forecast()`; dispatch US cities to NWS |
| `outcome_backfiller.py` | 2b | Add `_fetch_cdo_extremes()`; dispatch US cities to NOAA CDO |
| `ecmwf_forecast_pipeline.py` + `outcome_backfiller.py` | 3 | Coordinate patches (audit-driven) |

No changes to: `polymarket_dry_run.py`, `database_schema.py`, `precip_forecast_pipeline.py`, `polymarket_precip_dry_run.py`, launchd plists, TypeScript bot.

---

## Open prerequisites

- NOAA CDO token: request before starting workstream 2b implementation
- Live Polymarket market sample for coordinate audit: needed before workstream 3 patches

---

## Success criteria

1. `grep "Loop tick" /tmp/ecmwf_weather.log | tail -5` shows entries spaced ≤30 min apart
2. Bot runs continuously for 48h without launchd restart (process stays alive)
3. At least 1 simulated trade placed within 5 days of rebuild
4. Simulated win/loss outcomes for US cities match NWS-verified actuals (not Open-Meteo)
5. All 20 city coordinates confirmed against Polymarket resolution sources
