# Plan: Re-enable ECMWF Pipeline After Stale Disable

## Context

The daily review raised `ecmwf_pipeline_dead / ecmwf_grib2_stale / ecmwf_db_no_new_run_19h`. Investigation reveals this is **not a crash** — it is an **intentional disable that should have been reverted**.

**What happened:**
1. The ECMWF pipeline ran its last cycle on 2026-05-21 13:57, logged "Next wake in 1.0h", and cleanly exited.
2. Commit `0edbf61` ("Phase 1: Emergency stabilization") commented out the start block in `restart_all.sh` (lines 338–354) and replaced it with an active kill (lines 356–358).
3. The stale disable comment says "broken, ~95% loss rate" — but the rebuild on 2026-05-21 **already fixed** this: dropped confidence bands, switched to simple top-2 closest midpoints, verified WR 27.6% on 7-day backtest (see memory: `project_ecmwf_rebuild_2026_05_21.md`).
4. Result: every time `restart_all.sh` runs, it kills the ECMWF process and does not start it back up.

**Secondary issue:** The `daily_review_ecmwf.md` skill still checks for a GRIB2 file and confidence bands — both are stale artifacts from the pre-rebuild architecture. The rebuilt code uses Open-Meteo HTTP API (no GRIB2) and writes `lower_band=NULL, upper_band=NULL` to the DB.

---

## Fix 1 — Re-enable in `restart_all.sh`

**File:** `/Users/padraighaughey/sniff_test_polymarket/restart_all.sh` (lines 331–359)

Replace the entire block with:

```bash
# ── 20. ECMWF Weather Pipeline Loop ──────────────────────────────────────────
# Rebuilt 2026-05-21: top-2 closest midpoints, no confidence bands.
# WR 27.6% on 7-day backtest. Re-enabled after erroneous emergency disable.
echo "▶  run_ecmwf_loop.py (ECMWF 6h forecast + Polymarket tail market scan)"
kill_existing "run_ecmwf_loop.py"
touch "$LOG_ECMWF"
echo "   Log: $LOG_ECMWF"
nohup caffeinate -is bash -c "cd '$WEATHER_BOT_DIR' && exec '$FRAMEWORK_PYTHON' '$WEATHER_BOT_DIR/run_ecmwf_loop.py'" \
    >> "$LOG_ECMWF" 2>&1 &
ECMWF_PID=$!
sleep 2
if kill -0 "$ECMWF_PID" 2>/dev/null; then
    echo "   ✓  Running — PID $ECMWF_PID"
    PID_ECMWF=$ECMWF_PID
else
    echo "   ✗  Process exited immediately — check log:"
    echo "      tail -20 $LOG_ECMWF"
    tail -20 "$LOG_ECMWF" | sed 's/^/      | /'
    PID_ECMWF=""
fi
echo ""
```

Also update the summary section (line ~383) — change:
```
  run_ecmwf_loop.py         — DISABLED (broken ~95% loss rate; fix post-stabilization)
```
to:
```
  run_ecmwf_loop.py         — PID $PID_ECMWF (ECMWF 6h forecast, top-2 midpoints)
```

---

## Fix 2 — Start the daemon now (without waiting for restart_all.sh)

After applying Fix 1, start the process immediately:

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot
kill_existing "run_ecmwf_loop.py"   # or: pkill -f run_ecmwf_loop.py
touch /tmp/ecmwf_weather.log
nohup caffeinate -is bash -c "cd /Users/padraighaughey/Polymarket-Weather-Bot && exec /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 /Users/padraighaughey/Polymarket-Weather-Bot/run_ecmwf_loop.py" \
    >> /tmp/ecmwf_weather.log 2>&1 &
echo "PID: $!"
```

Then confirm it ran Stage 1:
```bash
sleep 10 && tail -20 /tmp/ecmwf_weather.log
```

---

## Fix 3 — Update `daily_review_ecmwf.md` (stale checks)

**File:** `/Users/padraighaughey/sniff_test_polymarket/.claude/skills/trading/weather/daily_review_ecmwf.md`

Two checks reference the old architecture and should be updated:

**Check 1 (GRIB2 freshness)** — the `stat ecmwf_2t.grib2` command is wrong; the rebuilt bot has no GRIB2 file. Replace with a DB-only freshness check:
```sql
SELECT max(ecmwf_run) AS latest_run, now() - max(ecmwf_run) AS age FROM public.forecasts;
```
Pass: `age < 7h` (6h cadence + 1h tolerance).

**Check 5 (Confidence band alignment)** — confidence bands are gone (`lower_band=NULL, upper_band=NULL`). Replace with a midpoint-distance check:
```sql
SELECT market_side, count(*), round(avg(abs(forecast_temp - price_yes*100)),1) AS avg_dist_cents
FROM public.dry_run_trades
WHERE created_at > now() - interval '24 hours'
GROUP BY market_side;
```
Pass: `avg_dist_cents < 3.0` (top-2 selection should stay close to forecast).

---

## Verification

1. `tail -f /tmp/ecmwf_weather.log` — confirm Stage 1 (forecast fetch) completes within 2 minutes
2. `psql -d gmgn_trading -c "SELECT count(*), max(ecmwf_run) FROM public.forecasts;"` — confirm 40 rows written with fresh `ecmwf_run`
3. Check Stage 2 runs and logs `top2_closest` dry-run entries (or skips with entry-window gate message)
4. Confirm the process survives to "Next wake in 1.0h" log line and stays alive
5. Run `restart_all.sh` and confirm ECMWF PID is non-empty in the summary output
