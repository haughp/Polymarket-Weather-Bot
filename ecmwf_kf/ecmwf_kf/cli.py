"""Phase 1 pipeline: ECMWF ensemble + observations -> aligned training table.

Examples:
  # Open-Meteo ensemble + NWS station obs, daily max in local time
  python -m ecmwf_kf --city nyc --source open-meteo --past-days 30 --daily max

  # ECMWF open data (AWS mirror), hourly steps, obs from CSV
  python -m ecmwf_kf --city nyc --source ecmwf-aws --run 2026092300 \\
      --steps 0-72/3 --obs-csv klga.csv --out aligned.csv

  # Local GRIB2/NetCDF files
  python -m ecmwf_kf --lat 40.78 --lon -73.87 --source grib \\
      --files data/*.grib2 --obs-csv obs.csv --interp nearest
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from .align import align_forecast_observations, daily_aggregate
from .ensemble import ensemble_stats
from .ingest import download_ecmwf_open_data, load_gridded_members, load_open_meteo_ensemble
from .observations import fetch_nws_observations, load_observations_csv

# Mirrors LOCATIONS / STATION_IDS in src/nws.ts
CITIES = {
    "nyc": (40.7772, -73.8726, "KLGA", "America/New_York"),
    "chicago": (41.9742, -87.9073, "KORD", "America/Chicago"),
    "miami": (25.7959, -80.2870, "KMIA", "America/New_York"),
    "dallas": (32.8471, -96.8518, "KDAL", "America/Chicago"),
    "seattle": (47.4502, -122.3088, "KSEA", "America/Los_Angeles"),
    "atlanta": (33.6407, -84.4277, "KATL", "America/New_York"),
}


def parse_steps(spec: str) -> list[int]:
    """'0-72/3' -> [0, 3, ..., 72]; '0,6,12' -> [0, 6, 12]."""
    steps: list[int] = []
    for part in spec.split(","):
        rng, _, inc = part.partition("/")
        lo, _, hi = rng.partition("-")
        steps += list(range(int(lo), int(hi or lo) + 1, int(inc or 1)))
    return steps


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ecmwf_kf", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    loc = p.add_argument_group("location")
    loc.add_argument("--city", choices=sorted(CITIES), help="use the bot's coordinates/station")
    loc.add_argument("--lat", type=float)
    loc.add_argument("--lon", type=float)
    loc.add_argument("--tz", help="local timezone for --daily (default: city tz or UTC)")

    fc = p.add_argument_group("forecast")
    fc.add_argument("--source", choices=["open-meteo", "grib", "ecmwf-aws"], default="open-meteo")
    fc.add_argument("--past-days", type=int, default=30, help="open-meteo: days of history")
    fc.add_argument("--forecast-days", type=int, default=7, help="open-meteo: days ahead")
    fc.add_argument("--model", default="ecmwf_ifs025", help="open-meteo ensemble model")
    fc.add_argument("--files", nargs="+", help="grib: GRIB2/NetCDF files")
    fc.add_argument("--param", default="2t", help="grib/ecmwf-aws: parameter (2t, mx2t3, ...)")
    fc.add_argument("--run", help="ecmwf-aws: run as YYYYMMDDHH, e.g. 2026092300")
    fc.add_argument("--steps", default="0-72/3", help="ecmwf-aws: lead-time steps in hours")
    fc.add_argument("--cache-dir", default=".cache/ecmwf")
    fc.add_argument("--interp", choices=["bilinear", "nearest"], default="bilinear")

    ob = p.add_argument_group("observations")
    ob.add_argument("--obs-csv", help="CSV with time,value columns")
    ob.add_argument("--obs-time-col", default="time")
    ob.add_argument("--obs-value-col", default="value")
    ob.add_argument("--obs-units", choices=["C", "F", "K"], default="C")
    ob.add_argument("--nws-station", help="fetch obs from api.weather.gov (default: city station)")

    al = p.add_argument_group("alignment")
    al.add_argument("--daily", choices=["max", "min", "mean"],
                    help="aggregate members and obs to local days before stats")
    al.add_argument("--min-samples", type=int, default=1,
                    help="--daily: minimum values per day, else the day is dropped")
    al.add_argument("--tolerance", default="30min", help="max forecast/obs time offset")
    al.add_argument("--out", default="aligned.csv")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    station, tz = None, args.tz or "UTC"
    if args.city:
        lat, lon, station, city_tz = CITIES[args.city]
        tz = args.tz or city_tz
    else:
        lat, lon = args.lat, args.lon
    if lat is None or lon is None:
        sys.exit("Give --city or both --lat and --lon")
    station = args.nws_station or station

    # 1. ensemble members at the point
    if args.source == "open-meteo":
        members = load_open_meteo_ensemble(lat, lon, model=args.model,
                                           past_days=args.past_days,
                                           forecast_days=args.forecast_days)
    elif args.source == "grib":
        if not args.files:
            sys.exit("--source grib needs --files")
        members = load_gridded_members(args.files, lat, lon, param=args.param, method=args.interp)
    else:
        if not args.run:
            sys.exit("--source ecmwf-aws needs --run YYYYMMDDHH")
        run = pd.to_datetime(args.run, format="%Y%m%d%H")
        files = download_ecmwf_open_data(run, parse_steps(args.steps), param=args.param,
                                         cache_dir=args.cache_dir)
        members = load_gridded_members(files, lat, lon, param=args.param, method=args.interp)
    print(f"forecast: {members.shape[1]} members x {members.shape[0]} time steps "
          f"({members.index.min()} .. {members.index.max()})")

    # 2. observations
    if args.obs_csv:
        obs = load_observations_csv(args.obs_csv, args.obs_time_col, args.obs_value_col,
                                    units=args.obs_units)
    elif station:
        obs = fetch_nws_observations(station, members.index.min() - pd.Timedelta(hours=1),
                                     members.index.max() + pd.Timedelta(hours=1))
    else:
        sys.exit("Give --obs-csv, --nws-station or --city")
    print(f"observations: {len(obs)} records")

    # 3. aggregate (optional), summarise, align
    if args.daily:
        members = daily_aggregate(members, args.daily, tz, args.min_samples)
        obs = daily_aggregate(obs, args.daily, tz, args.min_samples)
    stats = ensemble_stats(members)
    aligned = align_forecast_observations(stats, obs, tolerance=args.tolerance)

    aligned.to_csv(args.out)
    if aligned.empty:
        print("No overlapping forecast/observation rows.")
        return 1
    err = aligned["obs"] - aligned["ens_mean"]
    print(f"aligned rows: {len(aligned)} -> {args.out}")
    print(f"raw bias (obs - mean): {err.mean():+.2f} °C, RMSE {((err ** 2).mean()) ** 0.5:.2f} °C, "
          f"mean spread {aligned['ens_var'].mean() ** 0.5:.2f} °C")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
