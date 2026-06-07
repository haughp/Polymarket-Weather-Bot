#!/bin/bash
# Periodic sync script for weather bot trades
# Add to crontab: */5 * * * * /Users/padraighaughey/Polymarket-Weather-Bot/sync_trades_periodic.sh
# Or run manually: bash sync_trades_periodic.sh

cd /Users/padraighaughey/Polymarket-Weather-Bot

# Sync TS bot trades from simulation.json
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 sync_ts_bot_trades.py

# Run ECMWF --status to show latest trades (no DB write yet, just logging)
# The ECMWF bot writes to DB directly in run_ecmwf_loop.py

echo "[$(date)] Trade sync completed" >> /tmp/weather_bot_sync.log

# --- Once-daily: resolve snapshot actuals, then recompute city shadow→live promotions ---
# Guarded so the IEM backfill + promotion run at most once per calendar day (on the first
# 5-min tick after midnight), without needing a separate launchd agent.
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3
TODAY=$(date +%Y-%m-%d)
MARK=/tmp/weather_promotion_last_run
if [ "$(cat "$MARK" 2>/dev/null)" != "$TODAY" ]; then
  echo "[$(date)] Daily: snapshot actuals backfill + city promotion" >> /tmp/weather_bot_sync.log
  "$PY" backfill_snapshot_actuals.py >> /tmp/weather_promotion.log 2>&1
  "$PY" promote_cities.py            >> /tmp/weather_promotion.log 2>&1
  echo "$TODAY" > "$MARK"
fi
