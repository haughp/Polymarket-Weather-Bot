#!/usr/bin/env bash
# Run the ECMWF Kalman pipeline on a folder of ensemble files (macOS / Linux).
#   ./run_desktop.sh ~/Desktop/ECMWF [extra ecmwf_kf.desktop options...]
set -euo pipefail
cd "$(dirname "$0")"
DATA_DIR="${1:?usage: $0 DATA_DIR [options]}"; shift
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m ecmwf_kf.desktop --data-dir "$DATA_DIR" --out-dir desktop_output "$@"
