#!/bin/bash

cd /Users/padraighaughey/Polymarket-Weather-Bot

function start_service {
    NAME=$1
    CMD=$2
    LOG=$3

    echo "▶  $NAME"
    PIDS=$(pgrep -f "$CMD")
    if [ -n "$PIDS" ]; then
        echo "   ⚑  Stopping existing process(es): PID $PIDS"
        pkill -f "$CMD"
        sleep 1
    fi
    echo "   Log: $LOG"
    nohup $CMD >> $LOG 2>&1 &
    echo "   ✓  Running — PID $!"
    echo ""
}

echo "▶  Recompiling TypeScript Bot..."
npm run build
echo ""

echo "▶  Terminating old paper trading instances..."
pkill -f "dist/index.js" || true

start_service "scan_weather_markets.py --loop (Weather data collector)" "python3 scan_weather_markets.py --loop" "/tmp/weather_scanner.log"
start_service "weatherbot-ts --execute --interval 30 (TS LIVE trading)" "node dist/index.js --execute --interval 30" "/tmp/weather_bot_live.log"
start_service "run_ecmwf_loop.py (ECMWF 6h forecast + Polymarket tail market scan)" "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 run_ecmwf_loop.py" "/tmp/ecmwf_weather.log"

echo "▶  Terminating retired processes (polymarket-seed-agent & poll_metadata.py)"
pkill -f polymarket-seed-agent || true
pkill -f poll_metadata.py || true
echo ""

echo "▶  precip_forward_test_yes.py --execute (Precipitation LIVE execution)"
echo "   Triggering one-off live check (only executes on days 18-20)..."
python3 precip_forward_test_yes.py --execute
