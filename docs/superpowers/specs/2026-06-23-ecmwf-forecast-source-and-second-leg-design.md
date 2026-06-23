# ECMWF Temperature Pipeline — Forecast-Source Fix, Window/Debias Config, Position-Based Second Leg

**Date:** 2026-06-23
**Status:** Design — awaiting review
**Scope:** ECMWF temperature dry-run pipeline (`polymarket_dry_run.py`, `provider_backtest.py`, offline backtest tooling). No precip. No weatherbot-ts.

---

## 1. Problem

A 90-day offline backtest (n≈1120 verified-US, n≈2560 non-US, graded against the
settlement `outcomes` table) established that the live ECMWF F-bucket selection is
materially worse than it should be, for two confirmed bugs plus two suboptimal
config choices:

1. **Forecast-source mismatch (bug).** The runtime *selects* and *gates* providers
   using the multi-provider matrix (debiased MAE ~1.09°C non-US) but then *places
   the F bucket* using a different, worse forecast: the single `forecasts`-table
   aggregate (MAE ~1.51°C) plus a 45-day `LIVE_BIAS` median. The trade path does not
   consume the matrix-selected provider's forecast. This is the dominant driver of
   the recent F-hit collapse (38% → 12% over Jun 18–22).

2. **US ground-truth station mis-grading (bug).** The offline/backtest `CITIES` config
   grades NYC against LaGuardia (KLGA) and Dallas against Love Field (KDAL), but
   Polymarket settles NYC on **Central Park (KNYC)** and Dallas on **DFW (KDFW)** — the
   stations recorded in the `outcomes` table. The mis-grading differed by up to 4°F
   and understated true US F-hit (corrected verified-US debiased@W30 rose 32.8% → 35.0%).

3. **Trailing window too short (config).** Live uses a 7-day window for provider
   selection + bias. The grid showed **14d and 30d consistently beat 7d** in both
   regions. 30d also matches the uncommitted EV analysis in `provider_backtest.py`.

4. **Second leg leaves coverage on the table (opportunity).** F alone hits ~31–35%.
   A position-based second bucket raises P(either-leg-hits) to **~57–60%**.

### Validated configuration (the target)

| Element | Decision | Evidence |
|---|---|---|
| Leg-1 forecast source | matrix-selected best-in-class provider's forecast | source MAE 1.09 vs 1.51 |
| Trailing window | **30 days** (single `TRAIL_DAYS` constant) | grid W30 best/tied; matches EV note |
| Forecast derivative | **debiased**: `forecast − signed_bias` (mean) | raw always worst; half-debias underperforms |
| Provider selection metric | lowest **debiased-MAE** (≥ MIN_SAMPLES) | unchanged; matched grid winner |
| Second leg | **in-bucket-position** rule (NOT bias-sign) | position +25–27pp vs bias_sign +21pp |
| Ground truth | settlement `outcomes` table per city | corrects KLGA/KDAL mis-grading |

**Non-goal / explicitly deferred:** EV/profitability of the second leg. We have
accuracy (P(either)~60%) but not prices, so live second-leg *sizing* stays gated until
forward ladder-price logging produces an EV read. This spec ships the accuracy-validated
rule and the logging that unblocks EV — it does not claim the second leg is +EV.

---

## 2. Architecture & data flow

```text
provider_forecasts (8 models, archive+capture)
        │  select: lowest debiased-MAE over TRAIL_DAYS (no lookahead)
        ▼
  matrix cell = {provider, signed_bias, debiased_mae}      [provider_matrix.json]
        │
        ▼
  Leg-1 F:  corrected = provider_forecast − signed_bias     ← FIX: use matrix provider,
            F = bucket(corrected, width)                       NOT forecasts-table aggregate
        │
        ▼
  Leg-2 (2nd bucket): in-bucket position of `corrected`
            pos = (corrected − F_lo) / width
            2nd = F+1 if pos ≥ 0.5 else F−1
        │
        ▼
  candidate ladder (ALL buckets + prices)  ── read-only LOG ──▶ ladder_snapshots
        │
        ▼
  grading: actual from `outcomes` table (settlement station)  ← FIX: correct station per city
```

Components, each independently testable:

- **Provider/bias selection** (`provider_backtest.py::build_matrix`): pick provider by
  debiased-MAE over `TRAIL_DAYS=30`; emit `{provider, signed_bias, debiased_mae, unit, samples}`.
- **Leg-1 F placement** (`polymarket_dry_run.py`): read the matrix cell, fetch *that
  provider's* forecast for the day, compute `corrected = forecast − signed_bias`,
  `F = bucket(corrected)`. The current 4-way bias cascade (LIVE_BIAS → matrix → static → 0)
  collapses to: **matrix cell only; skip city if no usable cell.**
- **Leg-2 position rule** (`polymarket_dry_run.py::select_entry_pair`): replace
  "highest-priced neighbour" with the in-bucket-position neighbour.
- **Ground-truth resolver**: US cities present in `outcomes` grade off the DB; the
  station identity lives in `outcomes.source`, not a separately-chosen IEM station.
- **Ladder logging** (new, read-only): persist every candidate bucket + YES price per
  scan to a `ladder_snapshots` table, for forward EV analysis.

---

## 3. Detailed requirements

### 3.1 Leg-1 forecast-source fix (bug 1)

- The bucket-decision path MUST use the forecast from the provider named in the matrix
  cell for `(location_id, mode)`. It MUST NOT use the `forecasts`-table aggregate for the
  F decision.
- Bias applied MUST be the matrix cell's `bias`. **Sign convention (matches existing
  `provider_backtest.py::score`, which is authoritative):** `bias = mean(actual − forecast)`
  over the window, and `corrected = forecast + bias`. (Equivalently `corrected = forecast −
  mean(forecast − actual)`; the offline backtest used that equivalent form. The implementer
  MUST keep the live `forecast + bias` form so the stored matrix `bias` values are applied in
  the correct direction — applying the opposite sign is the Paris-overshoot doubling bug.)
- Remove the `LIVE_BIAS` (45-day median) priority-1 branch and the static-bias branch
  from the F path. Bias source = matrix cell only.
- If the matrix has no usable cell for `(location_id, mode)` (missing, MAE over gate, or
  unit mismatch), **skip the city/mode** (unchanged skip-on-null behaviour).
- MAE gate unchanged: skip if `mae_debiased` > 1.5°F (US) / 1.0°C (non-US).

### 3.2 Trailing window (config 3)

- Introduce a single `TRAIL_DAYS` constant (default **30**) used for BOTH provider
  selection and bias estimation in `build_matrix`. The launchd rebuild
  (`com.sniff.provider-matrix-rebuild.plist`) MUST pass `--days 30`.
- Document `TRAIL_DAYS` as the one tunable; the forward sample may revisit 14 vs 30.

### 3.3 Forecast derivative (config)

- Derivative = **debiased mean**: `corrected = forecast − signed_bias`. No half-shrink,
  no clamp (both underperformed full debias at W30).

### 3.4 Second-leg position rule (opportunity 4)

- Compute `pos = (corrected − F_lo) / width` where `F_lo` is F's lower edge and `width`
  is 2 (US °F) or 1 (non-US °C).
- Second bucket = `F + 1·width` if `pos ≥ 0.5`, else `F − 1·width`.
- The price floor still applies to the second leg: if the chosen 2nd bucket's YES price
  is below `BUCKET_MIN_PRICE`, SKIP the second leg (single-leg trade), do not substitute.
- The ceiling still applies to both legs (unchanged).
- Replaces the "adjacent neighbour with highest market price" rule.

### 3.5 Ground-truth station fix (bug 2)

- US cities present in `outcomes` (nyc/new_york, chicago, miami, dallas, seattle,
  atlanta, austin) MUST be graded against the `outcomes` table (the settlement source),
  not a hardcoded IEM CLI station.
- US cities NOT in `outcomes` (houston, denver, los-angeles, san-francisco) keep IEM CLI
  but MUST be tagged `unverified-vs-settlement` in any report and excluded from headline
  accuracy figures until their settlement station is confirmed.
- The offline tooling's `CITIES` station entries that disagree with settlement
  (NYC KLGA→Central Park, Dallas KDAL→DFW) MUST be corrected or the DB path used.

### 3.6 Ladder logging (deferred-EV enablement)

- New read-only table `ladder_snapshots`: one row per (scan, candidate bucket) with
  `location_id, mode, market_date, captured_at, bucket_lo, bucket_width, yes_price,
  is_F (bool), is_second_leg (bool)`.
- Written at scan time from the in-memory candidate ladder. MUST NOT affect trade
  decisions. Enables forward EV analysis of the second leg.

---

## 4. Testing

- **Leg-1 source fix:** unit test that, given a matrix cell naming provider P with bias B,
  the F bucket equals `bucket(P_forecast − B)` and is independent of the `forecasts`-table
  value. Regression test on the Jun 18–22 days: F-hit must rise vs the `forecasts`-table path.
- **Window:** test `build_matrix` selects over 30 days and the plist passes `--days 30`.
- **Second leg:** unit tests for `pos ≥ 0.5 → F+1`, `pos < 0.5 → F−1`, and price-floor skip.
- **Ground truth:** test US cities resolve actuals from `outcomes`; assert NYC uses
  Central Park value (not KLGA) on a day where they differ.
- **Ladder logging:** test rows are written and that disabling the logger does not change
  the selected pair.
- **Offline parity:** `offline_grid.py` / `offline_secondleg.py` reproduce the documented
  numbers (US debiased@W30 ≈ 35%, non-US median@W30 ≈ 32.5%, P(either) ≈ 57–60%).

---

## 5. Risks & open items

- **Second-leg EV unproven.** Accuracy ≠ profit. Live second-leg sizing stays gated on a
  forward EV read from `ladder_snapshots`. P(2nd)~25% means the 2nd leg loses ~75% alone.
- **Uncommitted working-tree drift.** `provider_backtest.py`, `provider_matrix.json`,
  `test_provider_matrix.py` are currently modified-uncommitted (the 7→30 default + bucket_hit
  fix). Implementation MUST start by reconciling these (commit or revert deliberately), not
  building on an ambiguous base — this repo has a documented uncommitted-drift failure history.
- **Sample size.** 90 days, summer-skewed. Findings (debias>raw, 30d>7d, position>bias-sign)
  are well-powered (n>1000) and consistent across regions, but absolute hit rates (~31–35%)
  remain below a tradeable bar — this fixes a regression and improves selection; it does not
  by itself make the strategy +EV.
- **Non-US ground truth caveats unchanged:** hong_kong/shenzhen remain ERA5/open-meteo
  (IEM-excluded); they stay out of headline accuracy.

---

## 6. Out of scope

- EV/PnL modelling of the second leg (separate spec once ladder data accrues).
- Precip pipeline. weatherbot-ts. Live execution flip (`ECMWF_LIVE_TRADING`).
- Tail-capture (F±2/3/4): ruled out — no pre-event predictor of large misses found.
