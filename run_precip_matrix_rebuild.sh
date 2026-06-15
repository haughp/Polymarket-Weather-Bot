#!/bin/bash
# Precipitation provider matrix rebuild — launchd entrypoint (days 18/19/20).
#
# 1. capture_precip_ensemble.py : snapshot each provider's month-end forecast
#    (idempotent — day-19/20 reruns no-op if day-18 already captured).
# 2. precip_provider_backtest.py : re-aggregate captured forecasts vs resolved
#    outcomes and rewrite precip_provider_matrix.json.
#
# There is NO archived precip-forecast source, so the matrix fills forward only
# (~1 sample/city/provider/month) and is cold for several months by design.
set -uo pipefail

cd /Users/padraighaughey/Polymarket-Weather-Bot || exit 1
PY=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3

echo "=== precip matrix rebuild $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
"$PY" capture_precip_ensemble.py
"$PY" precip_provider_backtest.py --months 12 --matrix
echo "=== done ==="
