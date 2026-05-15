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
