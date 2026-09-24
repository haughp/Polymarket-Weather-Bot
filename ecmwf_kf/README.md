# ECMWF ensemble → Kalman filter: Phase 1 (ingestion & alignment)

A Python pipeline that turns ECMWF ensemble forecasts and station observations
into an aligned training table for the online 2-parameter Kalman filter
(Dynamic Linear Model) that will correct forecast bias and scale errors.

It is separate from the TypeScript trading bot. `--city` reuses the bot's
coordinates and NWS stations from `src/nws.ts`.

## What it does

1. **Ensemble ingestion.** Loads the ensemble members (1 control + 50 perturbed)
   from any of three sources:
   | `--source` | Input | Notes |
   |---|---|---|
   | `open-meteo` | Open-Meteo ensemble API (`ecmwf_ifs025`) | Point JSON; lightest option |
   | `grib` | Local GRIB2 or NetCDF files | Control and perturbed members are merged |
   | `ecmwf-aws` | ECMWF open data, AWS mirror | Uses the `.index` byte ranges to fetch only the requested parameter (~0.65 MB per member per step instead of ~6 GB per step) |

   Then, for each time step *t*:
   - ensemble mean: x̄ₜ = (1/M) Σᵢ xᵢ,ₜ
   - unbiased spread: s²ₜ = 1/(M−1) Σᵢ (xᵢ,ₜ − x̄ₜ)²

   *M* is counted per step, so a missing member only drops out of the steps
   where it's missing. Steps with fewer than 2 members are dropped.
2. **Observation matching.** Reads yₜ from a CSV (`--obs-csv`) or from NWS
   station observations (`--nws-station`, or the station implied by `--city`).
   Readings the NWS flags as rejected (`X`) or questioned (`Q`) are dropped.
3. **Alignment and preprocessing.**
   - Gridded fields are interpolated to the station point by bilinear
     interpolation (default) or nearest neighbour (`--interp nearest`). Grids
     using either the 0–360 or the −180–180 longitude convention work.
   - Each forecast step is paired with the nearest observation within
     `--tolerance` (default 30 min; METARs land at :51, for example).
   - Rows with a missing mean, spread or observation are dropped.
   - Optional `--daily max|min|mean` aggregation over **local** calendar days.
     It aggregates each member first, then computes the statistics. The max of
     the ensemble mean is not the mean of the members' maxima, and daily-max
     markets need the latter. `--min-samples` drops days with too few values.

All temperatures are converted to °C: GRIB Kelvin, Open-Meteo °F and CSV
°F/K inputs.

## Output

A CSV (`--out`, default `aligned.csv`) indexed by `valid_time` (UTC), or by
`date` with `--daily`. Columns:

| column | meaning |
|---|---|
| `ens_mean` | ensemble mean x̄ₜ |
| `ens_var` | ensemble variance s²ₜ |
| `n_members` | members used at that step (M) |
| `obs` | matched observation yₜ |
| `obs_time` | timestamp of the matched observation (hourly mode only) |

The run also prints the raw bias, RMSE and mean spread as a sanity check.

## Usage

```bash
cd ecmwf_kf
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Last 30 days of daily highs at LaGuardia vs the ECMWF ensemble
python -m ecmwf_kf --city nyc --source open-meteo --past-days 30 --daily max --min-samples 20

# Raw ECMWF open data for one run, 3-hourly out to 72 h, with your own observations
python -m ecmwf_kf --city chicago --source ecmwf-aws --run 2026092300 --steps 0-72/3 \
    --obs-csv kord.csv --obs-units F

# Local files (CDS/MARS downloads, NetCDF, ...)
python -m ecmwf_kf --lat 47.45 --lon -122.31 --source grib --files data/*.grib2 --obs-csv ksea.csv

pytest   # offline unit tests
```

## Things to know

- **Lead time.** Open-Meteo `past_days` stitches together the most recent runs,
  so its history is mostly short-lead forecasts. To train the filter on one
  fixed lead time (for example, "tomorrow's forecast"), download successive
  runs with `ecmwf-aws` at a consistent step.
- **Retention.** ECMWF open data covers only the last few days of runs. Build
  a history by running the download on a schedule; files are cached in
  `--cache-dir`.
- **Throttling.** The AWS mirror sometimes answers `503 SlowDown` for minutes
  at a time. The downloader backs off and retries (up to about 8 minutes per
  request).
- **Members.** The AWS mirror currently publishes only the 50 perturbed
  members in the `enfo` files, so expect `M = 50` there (a warning is
  printed). Open-Meteo returns all 51.
- **Daily max from 3-hourly steps** underestimates the true maximum. Use
  `--param mx2t3` (the maximum over the previous 3 h) with `ecmwf-aws`/`grib`
  when the daily high matters. Likewise, hourly METARs can miss the peak, so
  the station's reported daily maximum is a better yₜ when you have it.
