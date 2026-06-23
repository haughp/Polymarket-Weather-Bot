# ECMWF Forecast-Source Fix, 30d/Debias Config, Position-Based Second Leg — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ECMWF dry-run pipeline place the F bucket using the matrix-selected provider's forecast + matrix bias (fixing 3 silent-fallback defects), switch the matrix window to 30 days, replace the highest-price second leg with an in-bucket-position rule, grade US cities against the settlement `outcomes` table, and add read-only candidate-ladder logging.

**Architecture:** All changes are in the `Polymarket-Weather-Bot` repo. The matrix-aware forecast fetch already exists in `polymarket_dry_run.py`; we fix why it silently falls back, collapse the bias cascade to the matrix cell, and adjust `select_entry_pair`. The window change is a one-line default + plist arg. Ladder logging is a new table + a write call that cannot affect decisions.

**Tech Stack:** Python 3.13, SQLAlchemy + psycopg2, PostgreSQL (`gmgn_trading`), pytest, launchd.

## Global Constraints

- Sign convention (authoritative, from `provider_backtest.py::score`): `bias = mean(actual − forecast)`; `corrected = forecast + bias`. Applying the opposite sign is the Paris-overshoot doubling bug.
- MAE gate (unchanged): skip city/mode if `mae_debiased` > 1.5 (°F, US) / 1.0 (°C, non-US).
- Price gates (unchanged constants): `BUCKET_MIN_PRICE = 0.12`, `BUCKET_MAX_PRICE = 0.35`. Floor applies to the neighbour leg only; ceiling to both.
- Bucket widths: US markets 2°F, non-US 1°C. `parse_temp_range` returns `(lo, hi, width)` half-open `[lo, hi)`, `hi == lo + width`.
- On a missing/unusable matrix provider forecast: **skip the city/mode — never fall back to the `forecasts`-table aggregate.**
- Reconcile the pre-existing uncommitted working-tree changes (`provider_backtest.py`, `provider_matrix.json`, `test_provider_matrix.py`) in Task 1 before any new work. Do NOT build over an ambiguous base.
- Ladder logging is read-only: it MUST NOT change which buckets are selected.
- All work on the current feature branch; commit after each task.

---

### Task 1: Reconcile pre-existing uncommitted drift

**Files:**
- Modify (commit as-is or revert): `provider_backtest.py`, `provider_matrix.json`, `test_provider_matrix.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a clean working tree so later tasks start from a known base.

- [ ] **Step 1: Inspect the uncommitted diffs**

Run: `git diff provider_backtest.py test_provider_matrix.py`
Run: `git diff --stat provider_matrix.json`
Expected: `provider_backtest.py` shows the `bucket_hit` debiased-scoring fix + `--days` default 7→30 + the EV-analysis comment; `test_provider_matrix.py` shows matching test updates; `provider_matrix.json` is a regenerated artifact.

- [ ] **Step 2: Verify the test suite passes on the uncommitted code**

Run: `python3 -m pytest test_provider_matrix.py -q`
Expected: PASS. If FAIL, stop and report — do not proceed.

- [ ] **Step 3: Commit the reconciled changes**

The `--days 30` default and `bucket_hit` debiased fix are wanted by this plan (Task 5), so commit them.

```bash
git add provider_backtest.py test_provider_matrix.py provider_matrix.json
git commit -m "chore: commit provider_backtest 30d default + debiased bucket_hit (pre-existing drift)"
```

---

### Task 2: Fix defect 1a — `new_york`→`nyc` city-key alias in the provider-forecast lookup

**Files:**
- Modify: `polymarket_dry_run.py` (the matrix-aware fetch block, ~L838–871)
- Test: `test_provider_source.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: module-level `PROVIDER_FORECASTS_CITY: dict[str, str]` and helper `pf_city(location_id: str) -> str` returning the `provider_forecasts.city` key for a `location_id`.

- [ ] **Step 1: Write the failing test**

```python
# test_provider_source.py
import polymarket_dry_run as pdr

def test_pf_city_maps_new_york_to_nyc():
    assert pdr.pf_city("new_york") == "nyc"

def test_pf_city_identity_for_others():
    assert pdr.pf_city("paris") == "paris"
    assert pdr.pf_city("shanghai") == "shanghai"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_provider_source.py -q`
Expected: FAIL with `AttributeError: module 'polymarket_dry_run' has no attribute 'pf_city'`.

- [ ] **Step 3: Add the alias map and helper**

Add near the other module-level maps (after `LOCATION_SLUGS`, ~L60):

```python
# provider_forecasts.city uses 'nyc' for New York; the runtime keys everything
# else by location_id, which equals provider_forecasts.city. Only NYC diverges.
PROVIDER_FORECASTS_CITY: dict[str, str] = {"new_york": "nyc"}

def pf_city(location_id: str) -> str:
    """Map a runtime location_id to its provider_forecasts.city key."""
    return PROVIDER_FORECASTS_CITY.get(location_id, location_id)
```

- [ ] **Step 4: Use `pf_city` in the provider-forecast lookup**

In the matrix-aware fetch block (~L850–856), change the query param from `loc_id` to `pf_city(loc_id)`:

```python
            row = session.execute(sa_text("""
                SELECT forecast_temp FROM provider_forecasts
                WHERE city = :city AND mode = :mode AND provider = :prov
                  AND target_date = :tdate
                ORDER BY captured_at DESC LIMIT 1
            """), {"city": pf_city(loc_id), "mode": mode_, "prov": provider,
                   "tdate": target_date}).fetchone()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest test_provider_source.py -q`
Expected: PASS.

- [ ] **Step 6: Verify the live effect on NYC**

Run: `python3 -c "import polymarket_dry_run as p; print(p.pf_city('new_york'))"`
Expected: `nyc`

- [ ] **Step 7: Commit**

```bash
git add polymarket_dry_run.py test_provider_source.py
git commit -m "fix(ecmwf): map new_york->nyc in provider_forecasts lookup (defect 1a)"
```

---

### Task 3: Fix defect 1b + 1c — skip on missing provider row; collapse bias cascade to matrix bias

**Files:**
- Modify: `polymarket_dry_run.py` — fetch fallback (~L873–874) and `record_dry_run` bias block (~L667–685)
- Test: `test_provider_source.py` (extend)

**Interfaces:**
- Consumes: `pf_city` (Task 2).
- Produces: `record_dry_run` applies bias from the matrix cell only (no `LIVE_BIAS`/static branch); a missing provider forecast yields a skip, not a forecasts-table fallback.

- [ ] **Step 1: Write the failing tests**

```python
# append to test_provider_source.py
import types

def _make_session_stub():
    # minimal stub: record_dry_run only needs load_provider_matrix + DB reads it does itself
    return None

def test_bias_block_uses_matrix_cell_only(monkeypatch):
    # The bias source resolution helper returns (bias, source) from the matrix cell,
    # ignoring LIVE_BIAS / static dicts entirely.
    import polymarket_dry_run as pdr
    cell = {"provider": "icon_seamless", "bias": -0.41, "mae_debiased": 0.53, "unit": "C"}
    bias, source = pdr.resolve_bias("paris", "max", cell)
    assert bias == -0.41
    assert source.startswith("matrix")

def test_bias_block_skips_when_no_cell():
    import polymarket_dry_run as pdr
    assert pdr.resolve_bias("paris", "max", None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_provider_source.py -q`
Expected: FAIL with `AttributeError: ... has no attribute 'resolve_bias'`.

- [ ] **Step 3: Add `resolve_bias` and use it in `record_dry_run`**

Add helper (near `record_dry_run`):

```python
def resolve_bias(location_id: str, mode: str, matrix_cell: dict | None):
    """Bias for the F decision = the matrix cell's bias ONLY (sign: corrected = raw + bias).
    Returns (bias_float, source_str) or None if there is no usable cell (caller skips)."""
    if matrix_cell is None:
        return None
    return float(matrix_cell.get("bias", 0.0)), f"matrix(provider={matrix_cell.get('provider','?')})"
```

Replace the cascade in `record_dry_run` (~L667–680) with:

```python
    _bias = resolve_bias(location_id, mode, matrix_cell)
    if _bias is None:
        print(f"   ⏭️  No usable matrix cell for {location_id}/{mode} — skipping")
        return
    bias, source = _bias
    corrected_temp = raw_temp + bias
```

(Leave the existing `📐 Bias:` print line directly after, unchanged.)

- [ ] **Step 4: Make the fetch path SKIP (not fall back) on a null-provider cell or missing row**

In the fetch block (~L873–874), replace the unconditional fallback:

```python
        # Skip when the matrix names no usable provider or its forecast row is absent —
        # do NOT fall back to the forecasts-table aggregate (defect 1b).
        if fc is None:
            cell = matrix.get(loc_id, {}).get(mode_)
            if cell and cell.get("provider") not in (None, "nws"):
                print(f"   ⏭️  {loc_id}/{mode_}: matrix provider {cell.get('provider')} "
                      f"has no provider_forecasts row for target date — skipping")
            else:
                print(f"   ⏭️  {loc_id}/{mode_}: matrix cell has no usable provider — skipping")
            continue
```

(Remove the old `if fc is None: fc = _get_latest_from_forecasts(loc_id, mode_)` fallback line; keep the subsequent `if fc is None: continue` guard.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest test_provider_source.py -q`
Expected: PASS.

- [ ] **Step 6: Remove now-dead bias machinery used only by the old cascade**

If `_load_live_bias`, `LIVE_BIAS`, `LIVE_BIAS_N`, `FORECAST_BIAS`, `_STATIC_BIAS_KEYS` are no longer referenced anywhere (grep first), delete them.

Run: `grep -nE "LIVE_BIAS|FORECAST_BIAS|_STATIC_BIAS_KEYS|_load_live_bias" polymarket_dry_run.py`
Expected after edit: only the definitions (if kept) — if zero call sites remain, delete the definitions and their loader call in `main()`. If any remain (e.g. telemetry), leave them and note why in the commit.

- [ ] **Step 7: Run the full suite for regressions**

Run: `python3 -m pytest -q`
Expected: PASS (no regressions).

- [ ] **Step 8: Commit**

```bash
git add polymarket_dry_run.py test_provider_source.py
git commit -m "fix(ecmwf): matrix-bias-only + skip-not-fallback on missing provider (defects 1b,1c)"
```

---

### Task 4: Replace the second leg with the in-bucket-position rule

**Files:**
- Modify: `polymarket_dry_run.py::select_entry_pair` (~L477–538, neighbour pick at L528)
- Test: `test_temp_buckets.py` (extend)

**Interfaces:**
- Consumes: candidate dicts with `range=(lo,hi)`, `width`, `yes_price` (from `classify_markets`).
- Produces: `select_entry_pair(candidates, corrected_temp, min_price, max_price)` selects the neighbour by in-bucket position of `corrected_temp` (high half → upper neighbour `F_hi`; low half → lower neighbour `F_lo − width`), keeping return shape `{"ok": True, "pair": [F, neighbour]}` and the existing floor/ceiling gates.

- [ ] **Step 1: Write the failing tests**

```python
# append to test_temp_buckets.py
import polymarket_dry_run as pdr

def _cand(lo, hi, width, price):
    return {"market": {}, "question": f"[{lo},{hi})", "range": (lo, hi),
            "width": width, "midpoint": (lo + hi) / 2, "yes_price": price}

def test_second_leg_high_position_picks_upper_neighbour():
    # corrected 37.8 sits in the HIGH half of [37,38) -> neighbour should be [38,39)
    cands = [_cand(36, 37, 1, 0.30), _cand(37, 38, 1, 0.20), _cand(38, 39, 1, 0.15)]
    out = pdr.select_entry_pair(cands, 37.8, min_price=0.12, max_price=0.35)
    assert out["ok"]
    assert out["pair"][0]["range"] == (37, 38)         # F
    assert out["pair"][1]["range"] == (38, 39)         # upper neighbour

def test_second_leg_low_position_picks_lower_neighbour():
    # corrected 37.2 sits in the LOW half of [37,38) -> neighbour should be [36,37)
    cands = [_cand(36, 37, 1, 0.15), _cand(37, 38, 1, 0.20), _cand(38, 39, 1, 0.30)]
    out = pdr.select_entry_pair(cands, 37.2, min_price=0.12, max_price=0.35)
    assert out["ok"]
    assert out["pair"][0]["range"] == (37, 38)
    assert out["pair"][1]["range"] == (36, 37)         # lower neighbour

def test_second_leg_skips_when_neighbour_below_floor():
    # low-position -> lower neighbour, but its price 0.05 < floor 0.12 -> not ok
    cands = [_cand(36, 37, 1, 0.05), _cand(37, 38, 1, 0.20)]
    out = pdr.select_entry_pair(cands, 37.2, min_price=0.12, max_price=0.35)
    assert not out["ok"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest test_temp_buckets.py -k second_leg -q`
Expected: FAIL (current rule picks neighbour by highest price, so high/low-position assertions fail).

- [ ] **Step 3: Replace the neighbour-selection logic**

In `select_entry_pair`, replace the highest-price pick (L528) with position-based selection. After `f_lo, f_hi = F['range']` and `width = F['width']`:

```python
    # Second leg by IN-BUCKET POSITION of the corrected forecast (validated 2026-06-23:
    # +25-27pp vs bias-sign). High half of F's interval -> upper neighbour; low half ->
    # lower neighbour. Falls back to whichever neighbour exists if only one is listed.
    pos = (corrected_temp - f_lo) / width if (f_lo is not None and width) else 0.5
    want_upper = pos >= 0.5
    upper = next((c for c in neighbours if c['range'][0] == f_hi), None)
    lower = next((c for c in neighbours if c['range'][0] == (f_lo - (width or 0))), None)
    neighbour = (upper or lower) if want_upper else (lower or upper)
    if neighbour is None:
        return {"ok": False, "reason": f"no neighbour bucket adjacent to F {F['range']}"}
```

(Keep the existing floor/ceiling gate block that follows, unchanged — it already rejects when `neighbour['yes_price'] < min_price`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_temp_buckets.py -k second_leg -q`
Expected: PASS.

- [ ] **Step 5: Run the full bucket suite for regressions**

Run: `python3 -m pytest test_temp_buckets.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add polymarket_dry_run.py test_temp_buckets.py
git commit -m "feat(ecmwf): in-bucket-position second leg (replaces highest-price neighbour)"
```

---

### Task 5: Set the matrix trailing window to 30 days

**Files:**
- Modify: `~/Library/LaunchAgents/com.sniff.provider-matrix-rebuild.plist` (the `--days` arg)
- Verify: `provider_backtest.py` (`--days` default already 30 after Task 1)

**Interfaces:**
- Consumes: nothing.
- Produces: the daily matrix rebuild uses a 30-day window.

- [ ] **Step 1: Confirm the backtest default is 30**

Run: `grep -n 'add_argument("--days"' provider_backtest.py`
Expected: `default=30` (committed in Task 1).

- [ ] **Step 2: Change the plist `--days` arg from 7 to 30**

Edit `~/Library/LaunchAgents/com.sniff.provider-matrix-rebuild.plist`: change the `<string>7</string>` immediately after `<string>--days</string>` to `<string>30</string>`.

- [ ] **Step 3: Reload the launchd job**

```bash
launchctl unload ~/Library/LaunchAgents/com.sniff.provider-matrix-rebuild.plist
launchctl load ~/Library/LaunchAgents/com.sniff.provider-matrix-rebuild.plist
```

- [ ] **Step 4: Verify by running the rebuild once and checking the window**

Run: `cd ~/Polymarket-Weather-Bot && python3 provider_backtest.py --days 30 --lead 1 --matrix`
Run: `python3 -c "import json; print(json.load(open('provider_matrix.json'))['window_days'])"`
Expected: `30`

- [ ] **Step 5: Commit (plist is outside the repo — note the change)**

The plist lives in `~/Library/LaunchAgents/`, not the repo. Commit the regenerated `provider_matrix.json` and document the plist change.

```bash
git add provider_matrix.json
git commit -m "config(ecmwf): matrix rebuild window 7d->30d (plist --days + regen matrix)"
```

---

### Task 6: Grade US cities against the settlement `outcomes` table

**Files:**
- Modify: `provider_backtest.py` (US actuals path) OR the offline tooling `CITIES` stations
- Test: `test_ground_truth.py` (create)

**Interfaces:**
- Consumes: `outcomes` table, `provider_backtest.CITIES`.
- Produces: US cities present in `outcomes` resolve actuals from the DB (settlement source); the two wrong stations (NYC, Dallas) no longer mis-grade.

- [ ] **Step 1: Write the failing test**

```python
# test_ground_truth.py
import datetime as dt
import provider_backtest as pb

US_IN_OUTCOMES = {"nyc","chicago","miami","dallas","seattle","atlanta","austin"}

def test_us_outcomes_cities_use_db_actuals():
    # For US cities present in `outcomes`, the backtest must read DB actuals,
    # not a hardcoded IEM CLI station that disagrees with settlement.
    assert pb.us_actuals_source("nyc") == "settlement_db"
    assert pb.us_actuals_source("dallas") == "settlement_db"

def test_us_iem_only_cities_flagged_unverified():
    assert pb.us_actuals_source("houston") == "iem_cli_unverified"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_ground_truth.py -q`
Expected: FAIL with `AttributeError: ... 'us_actuals_source'`.

- [ ] **Step 3: Add the source classifier and route US-in-outcomes to the DB**

Add to `provider_backtest.py`:

```python
US_IN_OUTCOMES = {"nyc","chicago","miami","dallas","seattle","atlanta","austin"}
_DB_LOC = {"nyc": "new_york"}   # provider_backtest city -> outcomes.location_id

def us_actuals_source(city: str) -> str:
    """Which ground-truth source a US city uses for scoring."""
    return "settlement_db" if city in US_IN_OUTCOMES else "iem_cli_unverified"
```

In the actuals-fetch site (where US currently calls `fetch_actuals(cfg["station"], ...)`), route `US_IN_OUTCOMES` cities through `fetch_actuals_db(_DB_LOC.get(city, city), start, end)` instead; leave the 4 IEM-only US cities on `fetch_actuals` but mark their rows `unverified`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest test_ground_truth.py -q`
Expected: PASS.

- [ ] **Step 5: Verify NYC no longer uses LaGuardia**

Run: `python3 -c "import provider_backtest as p; print(p.us_actuals_source('nyc'))"`
Expected: `settlement_db`

- [ ] **Step 6: Commit**

```bash
git add provider_backtest.py test_ground_truth.py
git commit -m "fix(ecmwf): grade US cities vs settlement outcomes DB, not wrong IEM station"
```

---

### Task 7: Read-only candidate-ladder logging

**Files:**
- Modify: `database_schema.py` (new `LadderSnapshot` model)
- Modify: `polymarket_dry_run.py` (write rows after `select_entry_pair`)
- Test: `test_ladder_logging.py` (create)

**Interfaces:**
- Consumes: the candidate list + chosen `pair` in `record_dry_run`.
- Produces: a `ladder_snapshots` table populated per scan; writing it MUST NOT change the selected pair.

- [ ] **Step 1: Write the failing test**

```python
# test_ladder_logging.py
import polymarket_dry_run as pdr

def _cand(lo, hi, w, p):
    return {"market": {}, "question": f"[{lo},{hi})", "range": (lo, hi),
            "width": w, "midpoint": (lo+hi)/2, "yes_price": p}

def test_ladder_rows_built_for_all_candidates():
    cands = [_cand(36,37,1,0.15), _cand(37,38,1,0.20), _cand(38,39,1,0.30)]
    F = cands[1]; neighbour = cands[2]
    rows = pdr.build_ladder_rows("paris", "max", "2026-06-24", cands, F, neighbour)
    assert len(rows) == 3
    f_row = next(r for r in rows if r["bucket_lo"] == 37)
    assert f_row["is_F"] is True and f_row["is_second_leg"] is False
    n_row = next(r for r in rows if r["bucket_lo"] == 38)
    assert n_row["is_second_leg"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest test_ladder_logging.py -q`
Expected: FAIL with `AttributeError: ... 'build_ladder_rows'`.

- [ ] **Step 3: Add the pure row-builder (no DB)**

```python
def build_ladder_rows(location_id, mode, market_date, candidates, F, neighbour):
    """Pure: build ladder-snapshot dicts for every candidate bucket. No DB, no side effects."""
    rows = []
    for c in candidates:
        lo, hi = c["range"]
        rows.append({
            "location_id": location_id, "mode": mode, "market_date": market_date,
            "bucket_lo": lo, "bucket_width": c.get("width"),
            "yes_price": c.get("yes_price"),
            "is_F": c is F, "is_second_leg": c is neighbour,
        })
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest test_ladder_logging.py -q`
Expected: PASS.

- [ ] **Step 5: Add the `LadderSnapshot` model**

In `database_schema.py`, add (matching the existing `Forecast`/`MarketState` style; ensure `Boolean` is in the SQLAlchemy import line):

```python
class LadderSnapshot(Base):
    __tablename__ = "ladder_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    captured_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    location_id = Column(String(32), index=True)
    mode = Column(String(16), default='max', index=True)
    market_date = Column(Date, index=True)
    bucket_lo = Column(Numeric(5, 1))
    bucket_width = Column(Numeric(4, 1))
    yes_price = Column(Numeric(5, 4))
    is_F = Column(Boolean, default=False)
    is_second_leg = Column(Boolean, default=False)
```

Add `from database_schema import LadderSnapshot` to `polymarket_dry_run.py`'s imports (alongside the other model imports).

- [ ] **Step 6: Wire the write into `record_dry_run` (after the pair is chosen, guarded)**

After `pair = selection['pair']` succeeds, build rows and persist them in a try/except that swallows errors so logging can never block a trade decision:

```python
    try:
        for r in build_ladder_rows(location_id, mode, str(target_date),
                                   candidates, pair[0], pair[1]):
            session.add(LadderSnapshot(**r))
    except Exception as e:
        print(f"   ⚠️  ladder logging skipped: {e}")
```

- [ ] **Step 7: Run the full suite + create the table**

Run: `python3 -c "from database_schema import init_database; init_database()"`
Run: `python3 -m pytest -q`
Expected: table created; all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add database_schema.py polymarket_dry_run.py test_ladder_logging.py
git commit -m "feat(ecmwf): read-only candidate-ladder logging for forward EV analysis"
```

---

### Task 8: End-to-end verification on recent days

**Files:**
- Use: `/tmp/offline_grid.py`, `/tmp/offline_secondleg.py` (existing offline tools)

**Interfaces:**
- Consumes: all prior tasks.
- Produces: confirmation the documented numbers reproduce and NYC now uses its matrix provider.

- [ ] **Step 1: Re-run the offline grid; confirm parity**

Run: `python3 /tmp/offline_grid.py 2>/dev/null`
Expected: US debiased@W30 ≈ 35%, non-US median@W30 ≈ 32.5% (within ±1pp).

- [ ] **Step 2: Re-run the second-leg backtest; confirm position rule**

Run: `python3 /tmp/offline_secondleg.py 2>/dev/null`
Expected: `position` rule P(either) ≈ 57–60%, beating `bias_sign`.

- [ ] **Step 3: Dry-run the live pipeline once; confirm NYC uses its matrix provider (no fallback)**

Run: `cd ~/Polymarket-Weather-Bot && python3 polymarket_dry_run.py 2>&1 | grep -iE "new_york|nyc|forecast_source|skipping" | head`
Expected: NYC line shows `provider_forecasts(ncep_nbm_conus)` (or skip if no row), NOT `forecasts table`.

- [ ] **Step 4: Final full-suite run**

Run: `python3 -m pytest -q`
Expected: PASS.

- [ ] **Step 5: No commit (verification only)** — record results in the task notes / experiment log per existing convention.

---

## Self-Review

Performed after drafting (see below for the actual review pass).
