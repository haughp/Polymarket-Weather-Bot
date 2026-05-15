#!/bin/bash
cd "$(dirname "$0")"
export PYTHONPATH="/Library/Frameworks/Python.framework/Versions/3.13/lib/python3.13/site-packages"
python3 scripts/merge_weather_data.py

