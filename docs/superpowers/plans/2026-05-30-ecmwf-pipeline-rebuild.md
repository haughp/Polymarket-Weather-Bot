# ECMWF Pipeline Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the ECMWF dry-run simulator so it runs continuously, rescans pending cities every 30 minutes, and computes win/loss outcomes against the same data sources Polymarket actually resolves against.

**Architecture:** Three independent changes to three files — loop runner gets crash recovery, forecast pipeline routes US cities to NWS, outcome backfiller routes US cities to NOAA CDO. A fourth audit step verifies observatory coordinates against Polymarket market rules before patching. All changes are additive; non-US cities and non-temperature pipelines are untouched.

**Tech Stack:** Python 3.13, httpx, pytest, NWS Point Forecast API (no key), NOAA CDO API (free token), Open-Meteo (unchanged for non-US)

---

## File Map

| File | Change |
|---|---|
| `run_ecmwf_loop.py` | Task 1: try/except around loop body; `min(1800)` sleep cap |
| `ecmwf_forecast_pipeline.py` | Task 2: add `fetch_nws_forecast()`; `NWS_FORECAST_US_CITIES`; dispatch in `extract_forecast()` |
| `outcome_backfiller.py` | Task 3: add `US_GHCND_STATIONS`, `_fetch_cdo_extremes()`; dispatch in `fetch_daily_extremes()` |
| `ecmwf_forecast_pipeline.py` + `outcome_backfiller.py` | Task 4: coordinate patches (audit-driven) |
| `test_ecmwf_loop_crash.py` | Task 1 test |
| `test_nws_forecast.py` | Task 2 test |
| `test_cdo_outcomes.py` | Task 3 test |

---

## Task 1: Loop Crash Recovery + 30-Minute Rescan

**Files:**
- Modify: `run_ecmwf_loop.py:185-212` (`main()`)
- Create: `test_ecmwf_loop_crash.py`

**Root cause:** `_run_pipeline()` inner stages are protected by try/except, but `importlib.reload()` calls between stages are not. An `ImportError` or any inter-stage exception escapes to `main()`, which has no protection. The process dies; launchd restarts it cold.

- [ ] **Step 1: Write the failing test**

Create `test_ecmwf_loop_crash.py`:

```python
import sys
import datetime
import pytest
sys.path.insert(0, ".")
import run_ecmwf_loop


def test_loop_continues_after_pipeline_crash(monkeypatch):
    """Loop body must survive an exception in _run_pipeline and keep iterating."""
    calls = []

    def mock_pipeline(full_refresh=True):
        calls.append(full_refresh)
        if len(calls) == 2:           # first inside-loop call crashes
            raise RuntimeError("simulated import error")
        if len(calls) >= 4:           # stop after 3 successful loop iterations
            raise SystemExit(0)

    monkeypatch.setattr(run_ecmwf_loop, "_run_pipeline", mock_pipeline)
    monkeypatch.setattr(run_ecmwf_loop.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        run_ecmwf_loop, "_next_ecmwf_utc",
        lambda: datetime.datetime.utcnow() - datetime.timedelta(hours=1),
    )

    with pytest.raises(SystemExit):
        run_ecmwf_loop.main()

    assert len(calls) >= 4, (
        f"Loop should have continued after crash on call 2, got {len(calls)} calls"
    )


def test_sleep_cap_is_1800(monkeypatch):
    """sleep_secs must never exceed 1800 (30 min) regardless of ECMWF window distance."""
    sleep_values = []

    def mock_sleep(secs):
        sleep_values.append(secs)
        raise SystemExit(0)   # stop after first sleep

    def mock_pipeline(full_refresh=True):
        pass

    # Put next ECMWF window 10 hours away — old code would sleep 3600, new code 1800
    far_future = datetime.datetime.utcnow() + datetime.timedelta(hours=10)
    monkeypatch.setattr(run_ecmwf_loop, "_run_pipeline", mock_pipeline)
    monkeypatch.setattr(run_ecmwf_loop.time, "sleep", mock_sleep)
    monkeypatch.setattr(run_ecmwf_loop, "_next_ecmwf_utc", lambda: far_future)

    with pytest.raises(SystemExit):
        run_ecmwf_loop.main()

    assert sleep_values[0] <= 1800, (
        f"Expected sleep ≤ 1800s, got {sleep_values[0]}s"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot
python -m pytest test_ecmwf_loop_crash.py -v
```

Expected: both tests FAIL — `test_loop_continues_after_pipeline_crash` fails because the unprotected exception kills `main()` before call 4; `test_sleep_cap_is_1800` fails because sleep is capped at 3600.

- [ ] **Step 3: Implement the fix**

In `run_ecmwf_loop.py`, replace the `main()` function body from `while True:` onwards:

```python
def main() -> None:
    log.info("ECMWF Weather Pipeline Loop Runner started")
    log.info("Working directory: %s", WEATHER_BOT_DIR)

    _run_pipeline(full_refresh=True)
    next_ecmwf = _next_ecmwf_utc()

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

- [ ] **Step 4: Run test to verify it passes**

```bash
python -m pytest test_ecmwf_loop_crash.py -v
```

Expected: both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add run_ecmwf_loop.py test_ecmwf_loop_crash.py
git commit -m "fix(ecmwf-loop): crash-safe while loop + cap sleep at 30 min"
```

---

## Task 2: NWS Forecast Routing for US Cities

**Files:**
- Modify: `ecmwf_forecast_pipeline.py` (add constant, new function, update `extract_forecast()`)
- Create: `test_nws_forecast.py`

**Why:** Polymarket resolves US temperature markets against NWS/NOAA instrument readings. ECMWF model output at the same coordinates diverges 1–3°F — enough to land in the wrong bucket. NWS Point Forecast API is free, no key required.

**US cities in this file's OBSERVATORIES:** `dallas`, `new_york`, `atlanta`, `austin` (4 cities — the ones with `'units': 'fahrenheit'` and US territory).

- [ ] **Step 1: Write the failing test**

Create `test_nws_forecast.py`:

```python
import sys
import datetime
import pytest
from unittest.mock import patch, MagicMock
sys.path.insert(0, ".")


def _make_nws_responses(target_date_str: str, high_f: float):
    """Build mock httpx responses for the two NWS API calls."""
    points_resp = MagicMock()
    points_resp.raise_for_status = lambda: None
    points_resp.json.return_value = {
        "properties": {
            "forecast": "https://api.weather.gov/gridpoints/FWD/66,104/forecast"
        }
    }

    forecast_resp = MagicMock()
    forecast_resp.raise_for_status = lambda: None
    forecast_resp.json.return_value = {
        "properties": {
            "periods": [
                {
                    "startTime": f"{target_date_str}T06:00:00-05:00",
                    "endTime":   f"{target_date_str}T18:00:00-05:00",
                    "isDaytime": True,
                    "temperature": high_f,
                    "temperatureUnit": "F",
                    "name": "Today",
                },
                {
                    "startTime": f"{target_date_str}T18:00:00-05:00",
                    "endTime":   f"{target_date_str}T06:00:00-05:00",
                    "isDaytime": False,
                    "temperature": 72.0,
                    "temperatureUnit": "F",
                    "name": "Tonight",
                },
            ]
        }
    }
    return points_resp, forecast_resp


def test_fetch_nws_forecast_returns_high_temp():
    from ecmwf_forecast_pipeline import fetch_nws_forecast

    target = datetime.date(2026, 6, 1)
    pts, fcast = _make_nws_responses("2026-06-01", 95.0)

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = [pts, fcast]

    with patch("ecmwf_forecast_pipeline.httpx.Client", return_value=client_mock):
        result = fetch_nws_forecast(32.8998, -97.0403, target, "America/Chicago", "fahrenheit")

    assert result is not None
    temp, peak_utc = result
    assert temp == pytest.approx(95.0, abs=0.1)
    assert isinstance(peak_utc, datetime.datetime)


def test_fetch_nws_forecast_converts_to_celsius():
    from ecmwf_forecast_pipeline import fetch_nws_forecast

    target = datetime.date(2026, 6, 1)
    pts, fcast = _make_nws_responses("2026-06-01", 95.0)

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = [pts, fcast]

    with patch("ecmwf_forecast_pipeline.httpx.Client", return_value=client_mock):
        result = fetch_nws_forecast(32.8998, -97.0403, target, "America/Chicago", "celsius")

    assert result is not None
    temp, _ = result
    assert temp == pytest.approx(35.0, abs=0.2)   # (95 - 32) * 5/9


def test_fetch_nws_forecast_returns_none_on_http_error():
    from ecmwf_forecast_pipeline import fetch_nws_forecast
    import httpx

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = httpx.ConnectError("timeout")

    with patch("ecmwf_forecast_pipeline.httpx.Client", return_value=client_mock):
        result = fetch_nws_forecast(32.8998, -97.0403, datetime.date.today(),
                                    "America/Chicago", "fahrenheit")
    assert result is None


def test_extract_forecast_routes_dallas_to_nws(monkeypatch):
    from ecmwf_forecast_pipeline import extract_forecast

    nws_calls = []
    ecmwf_calls = []

    def mock_nws(lat, lon, target_date, timezone, units, mode='max'):
        nws_calls.append(True)
        return (95.0, datetime.datetime(2026, 6, 1, 18, 0, 0))

    def mock_ecmwf(lat, lon, target_date, timezone, units, mode='max'):
        ecmwf_calls.append(True)
        return (20.0, datetime.datetime(2026, 6, 1, 12, 0, 0))

    import ecmwf_forecast_pipeline
    monkeypatch.setattr(ecmwf_forecast_pipeline, "fetch_nws_forecast", mock_nws)
    monkeypatch.setattr(ecmwf_forecast_pipeline, "fetch_ecmwf_daily_and_peak", mock_ecmwf)

    result = extract_forecast("dallas")
    assert result is not None
    assert len(nws_calls) == 1
    assert len(ecmwf_calls) == 0


def test_extract_forecast_routes_shanghai_to_ecmwf(monkeypatch):
    from ecmwf_forecast_pipeline import extract_forecast

    nws_calls = []
    ecmwf_calls = []

    def mock_nws(lat, lon, target_date, timezone, units, mode='max'):
        nws_calls.append(True)
        return (30.0, datetime.datetime(2026, 6, 1, 8, 0, 0))

    def mock_ecmwf(lat, lon, target_date, timezone, units, mode='max'):
        ecmwf_calls.append(True)
        return (32.5, datetime.datetime(2026, 6, 1, 6, 0, 0))

    import ecmwf_forecast_pipeline
    monkeypatch.setattr(ecmwf_forecast_pipeline, "fetch_nws_forecast", mock_nws)
    monkeypatch.setattr(ecmwf_forecast_pipeline, "fetch_ecmwf_daily_and_peak", mock_ecmwf)

    result = extract_forecast("shanghai")
    assert result is not None
    assert len(nws_calls) == 0
    assert len(ecmwf_calls) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest test_nws_forecast.py -v
```

Expected: all 5 tests FAIL with `ImportError` — `fetch_nws_forecast` does not exist yet.

- [ ] **Step 3: Add the NWS constant and function to `ecmwf_forecast_pipeline.py`**

After the `OPEN_METEO_DAILY` constant (line 14) and before `LOCATION_TIMEZONES`, add:

```python
NWS_POINTS_URL = "https://api.weather.gov/points"
NWS_USER_AGENT = "polymarket-weather-bot/1.0 (contact: admin@example.com)"

# US cities served by NWS Point Forecast API — used instead of Open-Meteo ECMWF
# for better alignment with Polymarket's NWS-based resolution source.
NWS_FORECAST_US_CITIES = frozenset({'dallas', 'new_york', 'atlanta', 'austin'})
```

After the `fetch_ecmwf_daily_and_peak()` function and before `extract_forecast()`, add:

```python
def fetch_nws_forecast(
    lat: float,
    lon: float,
    target_date: datetime.date,
    timezone: str,
    units: str,
    mode: str = 'max',
) -> tuple[float, datetime.datetime] | None:
    """
    NWS Point Forecast API for US cities. No API key required.
    Returns (forecast_temp, peak_time_utc) or None on failure.
    NWS always returns °F; converted to celsius if units='celsius'.
    """
    try:
        with httpx.Client(
            timeout=20,
            headers={'User-Agent': NWS_USER_AGENT},
        ) as client:
            r = client.get(f"{NWS_POINTS_URL}/{lat:.4f},{lon:.4f}")
            r.raise_for_status()
            forecast_url = r.json()['properties']['forecast']

            r2 = client.get(forecast_url)
            r2.raise_for_status()
            periods = r2.json()['properties']['periods']
    except Exception as e:
        print(f"   ⚠️  NWS error for ({lat},{lon}): {e}")
        return None

    target_str = target_date.isoformat()
    want_daytime = (mode == 'max')

    for period in periods:
        if period['startTime'][:10] == target_str and period['isDaytime'] == want_daytime:
            temp_f = float(period['temperature'])
            if units == 'celsius':
                forecast_temp = round((temp_f - 32.0) * 5.0 / 9.0, 1)
            else:
                forecast_temp = round(temp_f, 1)
            peak_utc = (
                datetime.datetime.fromisoformat(period['startTime'])
                .astimezone(datetime.timezone.utc)
                .replace(tzinfo=None)
            )
            return forecast_temp, peak_utc

    label = 'daytime' if want_daytime else 'nighttime'
    print(f"   ⚠️  NWS: no {label} period for {target_str} at ({lat},{lon})")
    return None
```

- [ ] **Step 4: Update `extract_forecast()` to dispatch on city**

Replace the body of `extract_forecast()` (currently lines 267–288):

```python
def extract_forecast(location_id: str, mode: str = 'max') -> dict | None:
    """Build a forecast row for a single (location_id, mode). Returns None on failure."""
    obs = OBSERVATORIES[location_id]
    target_date = target_date_for(location_id)

    if location_id in NWS_FORECAST_US_CITIES:
        result = fetch_nws_forecast(
            obs['lat'], obs['lon'], target_date,
            LOCATION_TIMEZONES[location_id], obs['units'], mode,
        )
    else:
        result = fetch_ecmwf_daily_and_peak(
            obs['lat'], obs['lon'], target_date,
            LOCATION_TIMEZONES[location_id], obs['units'], mode,
        )

    if result is None:
        return None
    temp, peak_utc = result

    return {
        'location_id':   location_id,
        'mode':          mode,
        'name':          obs['name'],
        'forecast_temp': round(temp, 1),
        'target_date':   target_date,
        'peak_time':     peak_utc,
        'units':         obs['units'],
        'confidence':    0.962,
    }
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest test_nws_forecast.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add ecmwf_forecast_pipeline.py test_nws_forecast.py
git commit -m "feat(ecmwf): route US cities to NWS Point Forecast API"
```

---

## Task 3: NOAA CDO Outcome Routing for US Cities

**Files:**
- Modify: `outcome_backfiller.py` (add constants, new function, update `fetch_daily_extremes()`)
- Create: `test_cdo_outcomes.py`

**Why:** `outcome_backfiller.py` currently uses Open-Meteo ERA5 reanalysis for all cities. For US cities, Polymarket resolves against actual NOAA ASOS station readings (GHCND daily data). ERA5 at the same coordinates can diverge 1–3°F from the physical station reading — the same margin that determines a bucket win or loss.

**Prerequisite:** Obtain a free NOAA CDO API token at `https://www.ncdc.noaa.gov/cdo-web/token`. Store it as env var `NOAA_CDO_TOKEN`. The token is valid indefinitely unless you exceed rate limits (1000 requests/day on free tier — well within our usage).

- [ ] **Step 1: Write the failing test**

Create `test_cdo_outcomes.py`:

```python
import sys
import os
import datetime
import pytest
from unittest.mock import patch, MagicMock
sys.path.insert(0, ".")


def _make_cdo_response(tmax_tenths: int, tmin_tenths: int):
    resp = MagicMock()
    resp.raise_for_status = lambda: None
    resp.json.return_value = {
        "results": [
            {"datatype": "TMAX", "value": tmax_tenths},
            {"datatype": "TMIN", "value": tmin_tenths},
        ]
    }
    return resp


def test_fetch_cdo_extremes_returns_fahrenheit(monkeypatch):
    from outcome_backfiller import _fetch_cdo_extremes

    monkeypatch.setenv("NOAA_CDO_TOKEN", "test-token-123")

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.return_value = _make_cdo_response(tmax_tenths=950, tmin_tenths=720)

    with patch("outcome_backfiller.httpx.Client", return_value=client_mock):
        max_t, min_t = _fetch_cdo_extremes("USW00094728", datetime.date(2026, 5, 29))

    assert max_t == pytest.approx(95.0, abs=0.01)   # 950 / 10
    assert min_t == pytest.approx(72.0, abs=0.01)   # 720 / 10


def test_fetch_cdo_extremes_returns_none_without_token(monkeypatch):
    from outcome_backfiller import _fetch_cdo_extremes

    monkeypatch.delenv("NOAA_CDO_TOKEN", raising=False)
    max_t, min_t = _fetch_cdo_extremes("USW00094728", datetime.date(2026, 5, 29))
    assert max_t is None
    assert min_t is None


def test_fetch_cdo_extremes_returns_none_on_http_error(monkeypatch):
    from outcome_backfiller import _fetch_cdo_extremes
    import httpx

    monkeypatch.setenv("NOAA_CDO_TOKEN", "test-token-123")

    client_mock = MagicMock()
    client_mock.__enter__ = lambda s: s
    client_mock.__exit__ = MagicMock(return_value=False)
    client_mock.get.side_effect = httpx.ConnectError("timeout")

    with patch("outcome_backfiller.httpx.Client", return_value=client_mock):
        max_t, min_t = _fetch_cdo_extremes("USW00094728", datetime.date(2026, 5, 29))

    assert max_t is None
    assert min_t is None


def test_fetch_daily_extremes_routes_new_york_to_cdo(monkeypatch):
    from outcome_backfiller import fetch_daily_extremes

    cdo_calls = []
    om_calls = []

    def mock_cdo(station_id, date):
        cdo_calls.append(station_id)
        return (88.0, 65.0)

    def mock_om(location_id, date):
        om_calls.append(location_id)
        return (30.0, 20.0)

    import outcome_backfiller
    monkeypatch.setattr(outcome_backfiller, "_fetch_cdo_extremes", mock_cdo)
    monkeypatch.setattr(outcome_backfiller, "_fetch_open_meteo_extremes", mock_om)

    max_t, min_t = fetch_daily_extremes("new_york", datetime.date(2026, 5, 29))

    assert len(cdo_calls) == 1
    assert cdo_calls[0] == "USW00094728"
    assert len(om_calls) == 0
    assert max_t == pytest.approx(88.0)


def test_fetch_daily_extremes_routes_shanghai_to_open_meteo(monkeypatch):
    from outcome_backfiller import fetch_daily_extremes

    cdo_calls = []
    om_calls = []

    def mock_cdo(station_id, date):
        cdo_calls.append(station_id)
        return (30.0, 20.0)

    def mock_om(location_id, date):
        om_calls.append(location_id)
        return (33.5, 25.1)

    import outcome_backfiller
    monkeypatch.setattr(outcome_backfiller, "_fetch_cdo_extremes", mock_cdo)
    monkeypatch.setattr(outcome_backfiller, "_fetch_open_meteo_extremes", mock_om)

    max_t, min_t = fetch_daily_extremes("shanghai", datetime.date(2026, 5, 29))

    assert len(cdo_calls) == 0
    assert len(om_calls) == 1
    assert max_t == pytest.approx(33.5)


def test_fetch_daily_extremes_falls_back_to_open_meteo_when_cdo_fails(monkeypatch):
    from outcome_backfiller import fetch_daily_extremes

    om_calls = []

    def mock_cdo(station_id, date):
        return (None, None)   # CDO failed

    def mock_om(location_id, date):
        om_calls.append(location_id)
        return (90.0, 68.0)

    import outcome_backfiller
    monkeypatch.setattr(outcome_backfiller, "_fetch_cdo_extremes", mock_cdo)
    monkeypatch.setattr(outcome_backfiller, "_fetch_open_meteo_extremes", mock_om)

    max_t, min_t = fetch_daily_extremes("dallas", datetime.date(2026, 5, 29))

    assert len(om_calls) == 1   # fallback triggered
    assert max_t == pytest.approx(90.0)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest test_cdo_outcomes.py -v
```

Expected: all 6 tests FAIL — `_fetch_cdo_extremes` and `_fetch_open_meteo_extremes` do not exist yet.

- [ ] **Step 3: Refactor `fetch_daily_extremes()` to extract an Open-Meteo helper**

In `outcome_backfiller.py`, add these constants after the `OPEN_METEO_ARCHIVE` constant and before `OBSERVATORIES`:

```python
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
```

Extract the Open-Meteo logic from the current `fetch_daily_extremes()` into a private helper. Add this function **before** `fetch_daily_extremes()`:

```python
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
    import os
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
```

- [ ] **Step 4: Replace `fetch_daily_extremes()` body with the dispatch logic**

Replace the entire `fetch_daily_extremes()` function:

```python
def fetch_daily_extremes(location_id: str, date: datetime.date) -> tuple[float | None, float | None]:
    """
    Fetch daily max and min temperature for a location.
    US cities route to NOAA CDO (actual ASOS readings) with Open-Meteo fallback.
    All other cities use Open-Meteo Archive (ERA5 reanalysis).
    Returns (max_temp, min_temp) in the location's native units, or (None, None).
    """
    if location_id in US_GHCND_STATIONS:
        max_t, min_t = _fetch_cdo_extremes(US_GHCND_STATIONS[location_id], date)
        if max_t is not None:
            obs = OBSERVATORIES[location_id]
            units_label = '°F' if obs['units'] == 'fahrenheit' else '°C'
            print(f"   ✅ {location_id:12s} {date}  max={max_t:.1f}{units_label}  min={min_t:.1f}{units_label}  [NOAA CDO]")
            return max_t, min_t
        # CDO token missing or API error — fall through to Open-Meteo
        print(f"   ⚠️  {location_id}: CDO failed, falling back to Open-Meteo")

    max_t, min_t = _fetch_open_meteo_extremes(location_id, date)
    if max_t is not None:
        obs = OBSERVATORIES[location_id]
        units_label = '°F' if obs['units'] == 'fahrenheit' else '°C'
        print(f"   ✅ {location_id:12s} {date}  max={max_t:.1f}{units_label}  min={min_t:.1f}{units_label}  [Open-Meteo]")
    else:
        print(f"   ❌ No data — {location_id} {date}")
    return max_t, min_t
```

Also remove the `print` statement from `backfill_outcomes()` that previously did the "✅ {location_id}" output (it was inside that function after the call to `fetch_daily_extremes`). The logging is now inside `fetch_daily_extremes()`.

In `backfill_outcomes()`, find the block that reads:
```python
        backfilled += 1
        units_label = '°F' if obs['units'] == 'fahrenheit' else '°C'
        print(f"   ✅ {location_id:12s} {target_date}  max={max_temp:.1f}{units_label}  min={min_temp:.1f}{units_label}")
```

Replace with:
```python
        backfilled += 1
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest test_cdo_outcomes.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add outcome_backfiller.py test_cdo_outcomes.py
git commit -m "feat(backfiller): route US cities to NOAA CDO for actual ASOS readings"
```

---

## Task 4: Coordinate Audit + Patch

**Files:**
- Modify: `ecmwf_forecast_pipeline.py` (OBSERVATORIES dict, patch mismatches)
- Modify: `outcome_backfiller.py` (OBSERVATORIES dict, same patches)

**Goal:** Confirm every city's `(lat, lon)` matches the station Polymarket's market resolution clause cites. Patch any mismatch in both files simultaneously.

**Method:** Use the Polymarket MCP tool (`get_markets` or equivalent) to fetch active temperature markets for each city and extract the resolution clause. Compare against OBSERVATORIES entries.

- [ ] **Step 1: Pull Polymarket market descriptions for each city**

Use the Polymarket MCP server (configured locally — see CLAUDE.md) to look up active markets for each of the 20 cities. For each city, find a temperature "Will the high temperature in [city] on [date] be X?" market and read its resolution source statement.

Search query pattern: `"high temperature in [city]"` or `"daily high [city]"`.

Record findings in this format per city:

```
city: [city_name]
polymarket_resolution_source: [exact text from market description]
expected_station: [station name + official coords from resolution clause]
current_coords: [lat, lon from OBSERVATORIES]
status: MATCH | MISMATCH | UNVERIFIED
```

- [ ] **Step 2: Cross-reference official station coordinates**

For each city where the resolution clause names a specific station, look up its official coordinates:

- **US cities** (NWS/NOAA): Use NOAA station metadata at `https://www.ncei.noaa.gov/cdo-web/datasets/GHCND/stations/GHCND:USW000XXXXX/detail`
- **HK**: HKO official station at `22.3020°N, 114.1740°E` — compare against our `(22.3027, 114.1772)`
- **Chinese cities** (CMA): Xujiahui Shanghai `31.1678°N, 121.4369°E`; Beijing `39.9289°N, 116.3689°E`; Qingdao `36.0667°N, 120.3333°E`
- **London** (Met Office/Heathrow): ICAO EGLL is `51.4775°N, 0.4613°W` — compare against our `(51.4700, -0.4543)`
- **Tokyo** (JMA): Main observatory `35.6941°N, 139.7514°E` — compare against our `(35.6894, 139.6917)`
- **Paris** (Météo-France Montsouris): `48.8214°N, 2.3378°E` — compare against our `(48.8225, 2.3372)`

- [ ] **Step 3: Patch mismatches**

For any city where the delta exceeds 0.05° in either lat or lon, update the coordinates in **both** files:

In `ecmwf_forecast_pipeline.py` OBSERVATORIES, update the affected city's `'lat'` and `'lon'`:
```python
'[city]': {
    'name': '...',
    'lat': [corrected_lat],    # was X.XXXX — corrected to match [source]
    'lon': [corrected_lon],
    'units': '...',
    'volume': ...,
},
```

Apply the identical coordinate change in `outcome_backfiller.py` OBSERVATORIES for the same city.

If no mismatches are found, document the verification result in a comment at the top of `ecmwf_forecast_pipeline.py`:
```python
# Observatory coordinates verified against Polymarket resolution sources 2026-05-30.
# All 20 cities within 0.05° of official station. See docs/superpowers/specs/2026-05-30-ecmwf-pipeline-rebuild-design.md
```

- [ ] **Step 4: Commit**

```bash
git add ecmwf_forecast_pipeline.py outcome_backfiller.py
git commit -m "fix(coords): verify + patch observatory coordinates against Polymarket resolution sources"
```

---

## Task 5: Smoke Test End-to-End

**Goal:** Verify the rebuilt loop runs for 1h without dying, produces at least one "Loop tick" every 30 min, and correctly routes forecasts + outcomes.

- [ ] **Step 1: Confirm all unit tests still pass**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot
python -m pytest test_ecmwf_loop_crash.py test_nws_forecast.py test_cdo_outcomes.py -v
```

Expected: all 12 tests PASS.

- [ ] **Step 2: Set NOAA_CDO_TOKEN and do a one-shot backfill for a US city**

```bash
export NOAA_CDO_TOKEN="<your-token>"
python -c "
import datetime
from outcome_backfiller import fetch_daily_extremes
max_t, min_t = fetch_daily_extremes('new_york', datetime.date.today() - datetime.timedelta(days=2))
print(f'NYC: max={max_t}°F  min={min_t}°F')
"
```

Expected: prints a plausible NYC temperature pair with `[NOAA CDO]` tag.

- [ ] **Step 3: Do a one-shot NWS forecast for a US city**

```bash
python -c "
import datetime
from ecmwf_forecast_pipeline import fetch_nws_forecast
tomorrow = datetime.date.today() + datetime.timedelta(days=1)
result = fetch_nws_forecast(32.8998, -97.0403, tomorrow, 'America/Chicago', 'fahrenheit')
print(f'DFW tomorrow: {result}')
"
```

Expected: prints `DFW tomorrow: (XX.X, datetime(...))`  — a Fahrenheit temperature and a UTC datetime.

- [ ] **Step 4: Tail the log for 35 minutes after restarting the daemon**

```bash
# Restart the loop (via launchd or manually)
launchctl stop com.sniff.ecmwf_weather 2>/dev/null; sleep 2
launchctl start com.sniff.ecmwf_weather

# Then tail
tail -f /tmp/ecmwf_weather.log | grep -E "Loop tick|Stage|Error|crashed"
```

Expected within 35 minutes:
- At least 1 "Loop tick" log line
- No "crashed — sleeping 60s" lines (if there are, check Stage 1 for NWS API errors)
- "Stage 2 ✅" on the first pass

- [ ] **Step 5: Verify loop tick spacing**

```bash
grep "Loop tick" /tmp/ecmwf_weather.log | tail -5
```

Expected: timestamps spaced ≤30 minutes apart.

---

## Success Criteria

1. `grep "Loop tick" /tmp/ecmwf_weather.log | tail -5` — entries ≤30 min apart
2. Bot runs for 48h without launchd restart (process stays alive)
3. At least 1 simulated trade placed within 5 days of rebuild going live
4. NYC/Atlanta/Dallas/Austin outcomes sourced from NOAA CDO (`[NOAA CDO]` in log), not Open-Meteo
5. All 20 city coordinates confirmed or patched against Polymarket resolution sources
