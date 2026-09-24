"""Run the full pipeline on a folder of ECMWF ensemble files on your computer.

Scans a folder for GRIB/NetCDF ensemble files, extracts the bot's cities,
builds daily-max forecasts at a fixed lead time, pulls station observations
into a local archive, runs the Kalman filter and writes corrected forecasts.

  python -m ecmwf_kf.desktop --data-dir "C:/Users/me/Desktop/ECMWF"

Outputs (in --out-dir, default ./desktop_output):
  forecasts.json          latest corrected forecasts per city (°C and °F)
  verification.csv        raw vs corrected MAE/RMSE per city
  <city>_kalman.csv       full filter table
  <city>_kalman.png       diagnostic plot
  obs/<city>.csv          observation archive (grows every run)
  .cache/                 per-file point extractions (re-used while files are unchanged)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from .align import daily_aggregate
from .cli import CITIES
from .ensemble import ensemble_stats
from .ingest import load_gridded_points
from .kalman import KalmanConfig, plot_kalman, run_kalman_filter, verification_summary
from .observations import (
    fetch_iem_observations,
    fetch_nws_observations,
    load_observations_csv,
    update_observation_archive,
)

FILE_PATTERNS = ("*.grib", "*.grib2", "*.grb", "*.grb2", "*.nc")
LONG_COLUMNS = ["point", "init_time", "valid_time", "number", "value"]
_NO_INIT = pd.Timestamp("1970-01-01", tz="UTC")


def scan_files(data_dir: Path, patterns=FILE_PATTERNS) -> list[Path]:
    files = {f for pattern in patterns for f in data_dir.rglob(pattern) if f.is_file()}
    return sorted(files)


def _cache_key(path: Path, points: dict, param: str, method: str) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{param}|{method}|{sorted(points.items())}"
    return hashlib.sha1(raw.encode()).hexdigest()


def extract_points_cached(
    files: list[Path],
    points: dict[str, tuple[float, float]],
    param: str,
    method: str,
    cache_dir: Path,
) -> pd.DataFrame:
    """Point extraction for every file, cached per file while it is unchanged."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for i, path in enumerate(files, 1):
        cached = cache_dir / f"{_cache_key(path, points, param, method)}.csv"
        if cached.exists():
            part = pd.read_csv(cached, parse_dates=["init_time", "valid_time"])
        else:
            print(f"  [{i}/{len(files)}] reading {path.name}")
            try:
                part = load_gridded_points([path], points, param=param, method=method)
            except Exception as e:  # one bad file should not stop the run
                warnings.warn(f"skipping {path}: {e}")
                continue
            part.to_csv(cached, index=False)
        parts.append(part)
    if not parts:
        return pd.DataFrame(columns=LONG_COLUMNS)
    long = pd.concat([p for p in parts if not p.empty] or parts, ignore_index=True)
    for col in ("init_time", "valid_time"):
        long[col] = pd.to_datetime(long[col], utc=True)
    return long


def daily_lead_members(
    long: pd.DataFrame,
    tz: str,
    lead_days: int,
    how: str = "max",
    min_samples: int = 4,
) -> tuple[pd.DataFrame, pd.Series]:
    """Per-member daily aggregates at a fixed lead, one run per local date.

    lead_days = local target date - UTC date of the run (0 = same day,
    1 = tomorrow). Each member is aggregated over the local day before any
    statistics; days with fewer than ``min_samples`` values are dropped. When
    several runs share a lead (00z and 12z), the latest run is used.
    Returns (member frame indexed by date, run time per date).
    """
    df = long.dropna(subset=["value"]).copy()
    has_init = df["init_time"].notna().all() and len(df) > 0
    if not has_init:
        warnings.warn("files have no forecast run time; lead time cannot be enforced")
        df["init_time"] = _NO_INIT
    df["date"] = df["valid_time"].dt.tz_convert(tz).dt.tz_localize(None).dt.normalize()

    g = df.groupby(["init_time", "date", "number"])["value"]
    daily = g.agg(how).to_frame("value")
    daily["n"] = g.count()
    daily = daily[daily["n"] >= min_samples].reset_index()

    if has_init:
        run_date = daily["init_time"].dt.tz_convert("UTC").dt.tz_localize(None).dt.normalize()
        daily = daily[(daily["date"] - run_date).dt.days == lead_days]
    latest = daily.groupby("date")["init_time"].transform("max")
    daily = daily[daily["init_time"] == latest]

    members = daily.pivot(index="date", columns="number", values="value").sort_index()
    members.columns.name = "number"
    runs = daily.groupby("date")["init_time"].first()
    if not has_init:
        runs[:] = pd.NaT
    return members, runs


def load_city_observations(
    city: str,
    station: str,
    start: pd.Timestamp,
    source: str,
    obs_dir: Path,
    obs_csv_dir: Path | None,
    obs_units: str,
) -> pd.Series:
    """Observations for a city, merged into the local archive obs/<city>.csv."""
    archive = obs_dir / f"{city}.csv"
    if source == "csv":
        path = (obs_csv_dir or Path(".")) / f"{city}.csv"
        if not path.exists():
            warnings.warn(f"{city}: no observation file {path}")
            return pd.Series(dtype=float, name="obs")
        return update_observation_archive(archive, load_observations_csv(str(path), units=obs_units))

    now = pd.Timestamp.now(tz="UTC")
    have = load_observations_csv(str(archive)) if archive.exists() else pd.Series(dtype=float)
    if len(have) and have.index.min() <= start + pd.Timedelta(days=1):
        fetch_from = have.index.max() - pd.Timedelta(days=2)  # top up, re-reading late reports
    else:
        fetch_from = start  # first run, or forecasts now reach further back than the archive
    try:
        if source == "iem":
            new = fetch_iem_observations(station, fetch_from, now)
        else:
            new = fetch_nws_observations(station, max(fetch_from, now - pd.Timedelta(days=7)), now)
    except Exception as e:
        warnings.warn(f"{city}: could not fetch observations ({e}); using the local archive")
        return have.rename("obs")
    return update_observation_archive(archive, new)


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def run_city(
    city: str,
    long: pd.DataFrame,
    args: argparse.Namespace,
    out_dir: Path,
) -> tuple[list[dict], pd.DataFrame | None]:
    lat, lon, station, tz = CITIES[city]
    members, runs = daily_lead_members(long, tz, args.lead_days, args.daily, args.min_samples)
    if members.empty:
        warnings.warn(f"{city}: no complete days at lead {args.lead_days} in the files")
        return [], None
    stats = ensemble_stats(members)

    obs = load_city_observations(
        city, station, pd.Timestamp(stats.index.min()).tz_localize(tz).tz_convert("UTC") - pd.Timedelta(days=1),
        args.obs_source, out_dir / "obs", args.obs_dir, args.obs_units,
    )
    today = pd.Timestamp.now(tz=tz).tz_localize(None).normalize()
    obs_daily = pd.Series(dtype=float)
    if len(obs):
        obs_daily = daily_aggregate(obs, args.daily, tz, args.obs_min_samples)
        obs_daily = obs_daily[obs_daily.index < today]  # today's max is not final yet
    table = stats.join(obs_daily.rename("obs"), how="left")

    n_obs = int(table["obs"].notna().sum())
    cfg = KalmanConfig(W=(args.w_alpha, args.w_beta), gamma=args.gamma)
    if n_obs >= 2:
        result = run_kalman_filter(table, cfg)
    else:
        warnings.warn(f"{city}: only {n_obs} observed days; showing the raw forecast uncorrected")
        result = pd.DataFrame({
            "timestamp": table.index, "actual_y": table["obs"].to_numpy(),
            "ecmwf_mean": table["ens_mean"].to_numpy(), "ecmwf_var": table["ens_var"].to_numpy(),
            "corrected_forecast": table["ens_mean"].to_numpy(),
            "forecast_std": np.sqrt(table["ens_var"].to_numpy()),
        })
    result.insert(1, "run", runs.reindex(table.index).to_numpy())
    result.insert(2, "n_members", table["n_members"].to_numpy())
    result.to_csv(out_dir / f"{city}_kalman.csv", index=False)

    summary = None
    if n_obs >= 2:
        skip = args.skip if n_obs > args.skip else 0
        summary = verification_summary(result, skip=skip)
        if args.plot:
            plot_kalman(result, str(out_dir / f"{city}_kalman.png"),
                        title=f"{city}: daily {args.daily}, lead {args.lead_days} d")

    upcoming = result[pd.to_datetime(result["timestamp"]) >= today]
    forecasts = [
        {
            "date": pd.Timestamp(r["timestamp"]).strftime("%Y-%m-%d"),
            "run": None if pd.isna(r["run"]) else pd.Timestamp(r["run"]).isoformat(),
            "n_members": int(r["n_members"]),
            "raw_mean_c": round(float(r["ecmwf_mean"]), 2),
            "corrected_c": round(float(r["corrected_forecast"]), 2),
            "std_c": round(float(r["forecast_std"]), 2),
            "raw_mean_f": round(_c_to_f(r["ecmwf_mean"]), 1),
            "corrected_f": round(_c_to_f(r["corrected_forecast"]), 1),
            "std_f": round(float(r["forecast_std"]) * 9.0 / 5.0, 2),
            "kalman_applied": n_obs >= 2,
        }
        for _, r in upcoming.iterrows()
    ]
    print(f"{city}: {len(result)} days, {n_obs} observed"
          + (f", RMSE raw {summary.loc['raw_ecmwf_mean', 'RMSE']:.2f} -> "
             f"corrected {summary.loc['kalman_corrected', 'RMSE']:.2f} °C" if summary is not None else ""))
    for f in forecasts:
        print(f"    {f['date']}: corrected {f['corrected_f']}°F ± {f['std_f']} (raw {f['raw_mean_f']}°F)")
    return forecasts, summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ecmwf_kf.desktop", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", required=True, type=Path, help="folder with ECMWF ensemble files")
    p.add_argument("--out-dir", type=Path, default=Path("desktop_output"))
    p.add_argument("--cities", default=",".join(CITIES), help="comma-separated bot city keys")
    p.add_argument("--param", default="2t", help="GRIB parameter: 2t, mx2t3, mx2t6, ...")
    p.add_argument("--interp", choices=["bilinear", "nearest"], default="bilinear")
    p.add_argument("--lead-days", type=int, default=1, help="0 = same day, 1 = tomorrow, ...")
    p.add_argument("--daily", choices=["max", "min", "mean"], default="max")
    p.add_argument("--min-samples", type=int, default=4,
                   help="forecast values needed per member per day (4 for 6-hourly steps)")
    p.add_argument("--obs-source", choices=["iem", "nws", "csv"], default="iem")
    p.add_argument("--obs-dir", type=Path, help="--obs-source csv: folder with <city>.csv (time,value)")
    p.add_argument("--obs-units", choices=["C", "F", "K"], default="F")
    p.add_argument("--obs-min-samples", type=int, default=18,
                   help="observations needed for a day's max to count (~24 hourly METARs/day)")
    p.add_argument("--gamma", type=float, default=1.0)
    p.add_argument("--w-alpha", type=float, default=1e-4)
    p.add_argument("--w-beta", type=float, default=1e-5)
    p.add_argument("--skip", type=int, default=30, help="spin-up rows left out of the scores")
    p.add_argument("--no-plot", dest="plot", action="store_false")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cities = [c.strip() for c in args.cities.split(",") if c.strip()]
    unknown = [c for c in cities if c not in CITIES]
    if unknown:
        raise SystemExit(f"Unknown cities {unknown}; choose from {sorted(CITIES)}")
    if not args.data_dir.is_dir():
        raise SystemExit(f"--data-dir {args.data_dir} is not a folder")
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    files = scan_files(args.data_dir)
    print(f"{len(files)} ensemble files in {args.data_dir}")
    if not files:
        raise SystemExit("No GRIB/NetCDF files found")
    points = {c: CITIES[c][:2] for c in cities}
    long = extract_points_cached(files, points, args.param, args.interp, out_dir / ".cache")
    if long.empty:
        raise SystemExit(f"No '{args.param}' fields found in the files")

    all_forecasts, summaries = {}, []
    for city in cities:
        city_long = long[long["point"] == city]
        forecasts, summary = run_city(city, city_long, args, out_dir)
        all_forecasts[city] = forecasts
        if summary is not None:
            s = summary.drop(index="improvement_%").reset_index(names="forecast")
            s.insert(0, "city", city)
            summaries.append(s)

    payload = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "param": args.param,
        "daily": args.daily,
        "lead_days": args.lead_days,
        "cities": all_forecasts,
    }
    (out_dir / "forecasts.json").write_text(json.dumps(payload, indent=2))
    if summaries:
        pd.concat(summaries).to_csv(out_dir / "verification.csv", index=False)
    print(f"\nwrote {out_dir / 'forecasts.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
