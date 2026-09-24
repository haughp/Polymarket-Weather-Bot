# Handoff: ECMWF ensemble Kalman filter (for review before integration)

This file briefs a Claude Code instance, running on the user's desktop
in VS Code, that will review and possibly change this work before it is
wired into the trading bot. It was written by the cloud session that built
the code.

## Ground rules

- Work on branch `claude/loving-mccarthy-r0813s`. **Do not merge to `main`
  or open a PR** unless the user asks.
- **Do not change the TypeScript bot (`src/`).** Integration is a later
  step; this review is only about `ecmwf_kf/`.
- Run the tests before and after any change: `cd ecmwf_kf && pytest`
  (29 tests, all offline).
- Unlike the cloud session, the desktop has internet access and the
  user's real ECMWF files. Use them to verify the parts marked **untested**
  below.

## What it is

A Python package, `ecmwf_kf/`, that corrects bias in the ECMWF ensemble
forecast of daily high temperature at the bot's six stations. It uses an
online 2-parameter Kalman filter (dynamic linear model):

    y_t = alpha_t + beta_t * ens_mean_t + v_t,   V_t = sigma0^2 + gamma * ens_var_t
    theta_t = theta_{t-1} + w_t,                 W = diag(1e-4, 1e-5)

The user supplied this spec in four phases:
1. ingestion and feature engineering
2. state-space model
3. NumPy implementation: θ₀ = [0, 1], P₀ = diag(10, 1), σ₀² = var(y − x̄)
   over the first 30 rows
4. output DataFrame, MAE/RMSE verification and a matplotlib plot

## Layout

| file | role |
|---|---|
| `ecmwf_kf/ingest.py` | Loaders (Open-Meteo, GRIB/NetCDF via cfgrib/xarray, ECMWF open-data byte-range downloader), bilinear/nearest point extraction, member frames. Keeps `init_time` (the forecast run) per row. |
| `ecmwf_kf/ensemble.py` | Ensemble mean and unbiased variance (ddof=1), with M counted per row |
| `ecmwf_kf/observations.py` | CSV, NWS API and IEM ASOS loaders; local observation archive |
| `ecmwf_kf/align.py` | Per-member daily aggregation in local time; nearest-time alignment |
| `ecmwf_kf/kalman.py` | The filter, verification summary, plot; `python -m ecmwf_kf.kalman` |
| `ecmwf_kf/cli.py` | Phase 1 CLI, `python -m ecmwf_kf`. `CITIES` mirrors `src/nws.ts`. |
| `ecmwf_kf/desktop.py` | **Main entry point** for the user's folder of files: `python -m ecmwf_kf.desktop` |
| `run_desktop.ps1` / `run_desktop.sh` | Launchers: create `.venv`, install `requirements.txt`, run `desktop` |
| `tests/` | `test_phase1.py`, `test_kalman.py`, `test_desktop.py` |
| `README.md` | User documentation |

The desktop flow: scan folder → extract all cities per file (cached in
`desktop_output/.cache`) → per-member daily max in city-local time at a
fixed `--lead-days` → ensemble stats → observation archive
(`desktop_output/obs/<city>.csv`, IEM by default) → filter →
`forecasts.json`, `verification.csv`, `<city>_kalman.csv/.png`.

## Verification status

**Tested in the cloud session:**
- Real ECMWF open-data GRIB (the AWS mirror, run 2026-09-23 00z, steps 0 h
  and 24 h) decodes, point-extracts, and runs through the desktop pipeline.
  The 00z value at LaGuardia was 16.9 °C, with 50 members.
- A simulated 40-run NetCDF folder with a known +2 °C bias: the filter
  learns the bias and more than halves RMSE (`tests/test_desktop.py`).
- `run_desktop.sh` from a fresh virtualenv.

**Untested (no network access or no Windows there). Please check these:**
1. `run_desktop.ps1` on Windows, and whether `pip install eccodes`
   works there. The conda fallback is in the README.
2. The IEM fetcher, `fetch_iem_observations`, against the live site.
   Check the station ID (the leading `K` is stripped, e.g. `KLGA` → `LGA`),
   the `report_type=3,4` parameters, and the CSV columns
   (`station,valid,tmpf`).
3. The NWS observation fetcher and the Open-Meteo ensemble loader against
   the live APIs. Their parsers are unit-tested against the documented
   formats only.
4. **The user's own ECMWF files.** It's unknown whether they are GRIB or
   NetCDF, which parameters they hold, whether the run time is present
   (NetCDF from Copernicus may use `forecast_reference_time`), and the
   step spacing. Run
   `python -m ecmwf_kf.desktop --data-dir <folder> --cities nyc` and
   inspect the outputs.

## Decisions worth a second opinion

- **γ = 1.0.** The spec doesn't give a value; `--gamma` changes it.
- **σ₀².** It is computed over the first 30 *observed* rows. The spec says
  y₁:₃₀; this matters when early rows have no observation.
- **Default W = diag(1e-4, 1e-5).** It adapts slowly: in a synthetic test,
  a bias drifting 2 °C over a year was only about one-third tracked.
  `--w-alpha 1e-3` tracked it better. Tune W on real verification scores.
- **The corrected forecast uses the prior state (out-of-sample).** The
  alpha/beta columns show the posterior. There's a test for this.
- **`kalman_gain`** is H·K (a scalar). The components are in
  `kalman_gain_alpha` and `kalman_gain_beta`.
- **Lead time.** `lead_days` = local target date − **UTC** date of the run.
  When runs tie at the same lead, the latest wins.
- **Default thresholds.** `--min-samples 4` (forecast values per member per
  day, suited to 6-hourly steps) and `--obs-min-samples 18` (METARs per
  day).
- **Instantaneous `2t` at 3- or 6-hourly steps underestimates the daily
  high.** The filter absorbs this as bias. `--param mx2t6`/`mx2t3` may be
  better. However, their values are stamped at the end of the max window,
  so a window can straddle local midnight. Check this.
- **The Phase 1 CLI mixes lead times.** With overlapping runs,
  `python -m ecmwf_kf --daily` keeps the latest run per *valid time*,
  which mixes leads. Only `desktop.py` enforces a fixed lead.
- **The ECMWF AWS mirror has 50 members, not 51.** Its `enfo` files
  currently hold only the 50 perturbed members (a warning is printed).
  It also throttles hard (`503 SlowDown`); the downloader backs off,
  retries and resumes per member.

## Before integration with the bot (not started)

- **Where the bot would read from.** `src/nws.ts` returns a daily max
  in °F per date. `forecasts.json` has `corrected_f` and `std_f` per
  city and date, which would replace or blend with that.
- **Resolution source.** Polymarket resolves on the station's reported
  daily high (in whole °F). Confirm the observation source matches, and
  consider using `std_f` to turn the forecast into bucket probabilities
  instead of a single point.
