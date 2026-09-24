"""Load ECMWF ensemble forecasts as member frames.

A member frame is a DataFrame indexed by tz-aware UTC valid time, with one
column per ensemble member number (0 = control, 1..50 = perturbed) and values
in degrees Celsius.

Sources:
  * Open-Meteo ensemble API (JSON, already a point series)
  * GRIB2 / NetCDF files (gridded; interpolated to the target point)
  * ECMWF open data (downloads only the requested parameter from the
    multi-GB ensemble files using the published byte-range index)
"""

from __future__ import annotations

import json
import os
import re
import time
import warnings
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
import requests
import xarray as xr

OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ECMWF_AWS_URL = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
EXPECTED_MEMBERS = 51  # 1 control + 50 perturbed

_GRIB_SUFFIXES = {".grib", ".grib2", ".grb", ".grb2"}
# GRIB shortName -> variable name cfgrib gives it
_CFGRIB_NAMES = {"2t": "t2m", "2d": "d2m", "10u": "u10", "10v": "v10"}
_LAT_NAMES = ("latitude", "lat")
_LON_NAMES = ("longitude", "lon")


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------


def parse_open_meteo_ensemble(
    payload: dict, variable: str = "temperature_2m", model: str | None = None
) -> pd.DataFrame:
    """Turn an Open-Meteo ensemble API response into a member frame.

    The control run is the bare ``variable`` key; perturbed members are
    ``{variable}_memberNN``. Keys may carry a ``_{model}`` suffix when more
    than one model was requested.
    """
    hourly = payload.get("hourly")
    if not hourly or "time" not in hourly:
        raise ValueError("Open-Meteo response has no hourly data")

    suffix = f"(?:_{re.escape(model)})?" if model else ""
    pattern = re.compile(rf"^{re.escape(variable)}(?:_member(\d+))?{suffix}$")
    columns: dict[int, list] = {}
    for key, values in hourly.items():
        m = pattern.match(key)
        if m:
            columns[int(m.group(1) or 0)] = values
    if not columns:
        raise ValueError(f"No '{variable}' members in Open-Meteo response")

    # Times are wall-clock in the requested timezone; shift back to UTC.
    offset = pd.Timedelta(seconds=payload.get("utc_offset_seconds", 0))
    index = pd.DatetimeIndex(pd.to_datetime(hourly["time"]) - offset).tz_localize("UTC")
    frame = pd.DataFrame(columns, index=index, dtype=float).sort_index(axis=1)
    frame.index.name = "valid_time"
    frame.columns.name = "number"

    unit = payload.get("hourly_units", {}).get(variable, "°C")
    if "F" in unit:
        frame = (frame - 32.0) * 5.0 / 9.0
    return frame


def load_open_meteo_ensemble(
    lat: float,
    lon: float,
    model: str = "ecmwf_ifs025",
    variable: str = "temperature_2m",
    past_days: int = 0,
    forecast_days: int = 7,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Fetch the ECMWF ensemble for one point from Open-Meteo."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": variable,
        "models": model,
        "timezone": "GMT",
        "past_days": past_days,
        "forecast_days": forecast_days,
    }
    r = (session or requests).get(OPEN_METEO_ENSEMBLE_URL, params=params, timeout=60)
    payload = r.json()
    if r.status_code != 200 or payload.get("error"):
        raise RuntimeError(f"Open-Meteo error {r.status_code}: {payload.get('reason')}")
    return parse_open_meteo_ensemble(payload, variable=variable)


# ---------------------------------------------------------------------------
# Spatial interpolation
# ---------------------------------------------------------------------------


def _coord_name(da: xr.DataArray, candidates: Sequence[str]) -> str:
    for name in candidates:
        if name in da.dims:
            return name
    raise ValueError(f"None of {candidates} found in dims {da.dims}")


def _match_lon_convention(grid_lons: np.ndarray, lon: float) -> float:
    if grid_lons.max() > 180 and lon < 0:
        return lon + 360.0
    if grid_lons.min() < 0 and lon > 180:
        return lon - 360.0
    return lon


def _bracket(values: np.ndarray, x: float, axis: str) -> tuple[int, int, float]:
    """Indices of the two grid points around ``x`` and the weight of the second."""
    order = np.argsort(values)
    v = values[order]
    if not v[0] <= x <= v[-1]:
        raise ValueError(
            f"{axis}={x} is outside the grid [{v[0]}, {v[-1]}]; use nearest-neighbour"
        )
    j = int(np.clip(np.searchsorted(v, x, side="right"), 1, len(v) - 1))
    weight = (x - v[j - 1]) / (v[j] - v[j - 1])
    return int(order[j - 1]), int(order[j]), float(weight)


def extract_point(
    da: xr.DataArray, lat: float, lon: float, method: str = "bilinear"
) -> xr.DataArray:
    """Interpolate a gridded field to a point; lat/lon dims are removed.

    ``method`` is ``"bilinear"`` or ``"nearest"``. Only the 2x2 neighbourhood
    is read, so this stays cheap on lazily-loaded global fields.
    """
    lat_name = _coord_name(da, _LAT_NAMES)
    lon_name = _coord_name(da, _LON_NAMES)
    lats = np.asarray(da[lat_name].values, dtype=float)
    lons = np.asarray(da[lon_name].values, dtype=float)
    lon = _match_lon_convention(lons, lon)

    if method == "nearest":
        out = da.isel(
            {lat_name: int(np.abs(lats - lat).argmin()), lon_name: int(np.abs(lons - lon).argmin())}
        )
    elif method == "bilinear":
        i0, i1, wy = _bracket(lats, lat, "latitude")
        k0, k1, wx = _bracket(lons, lon, "longitude")
        box = da.isel({lat_name: [i0, i1], lon_name: [k0, k1]})
        weights = xr.DataArray(
            [[(1 - wy) * (1 - wx), (1 - wy) * wx], [wy * (1 - wx), wy * wx]],
            dims=(lat_name, lon_name),
        )
        out = (box.drop_vars([lat_name, lon_name]) * weights).sum(
            dim=[lat_name, lon_name], skipna=False
        )
        out.attrs = da.attrs
    else:
        raise ValueError(f"Unknown interpolation method '{method}'")
    return out.drop_vars([c for c in (lat_name, lon_name) if c in out.coords])


# ---------------------------------------------------------------------------
# GRIB2 / NetCDF
# ---------------------------------------------------------------------------


def _open_datasets(path: Path) -> list[xr.Dataset]:
    if path.suffix.lower() in _GRIB_SUFFIXES:
        import cfgrib  # optional dependency

        # cfgrib splits control (cf) and perturbed (pf) members into separate
        # datasets because their GRIB keys differ.
        return cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
    return [xr.open_dataset(path)]


def _pick_variable(ds: xr.Dataset, param: str | None) -> xr.DataArray | None:
    if param:
        for name in (param, _CFGRIB_NAMES.get(param, param)):
            if name in ds.data_vars:
                return ds[name]
        return None
    gridded = [
        v for v in ds.data_vars.values()
        if any(d in v.dims for d in _LAT_NAMES) and any(d in v.dims for d in _LON_NAMES)
    ]
    if len(gridded) != 1:
        raise ValueError(
            f"Pass param=...; dataset has {[v.name for v in gridded]} gridded variables"
        )
    return gridded[0]


def _utc(values) -> pd.Series:
    t = pd.to_datetime(pd.Series(values))
    return t.dt.tz_localize("UTC") if t.dt.tz is None else t.dt.tz_convert("UTC")


def point_to_long_frame(da: xr.DataArray) -> pd.DataFrame:
    """Flatten a point DataArray to rows of (init_time, valid_time, number, value).

    ``init_time`` is the forecast run (NaT when the file does not say); it
    keeps forecasts from different runs apart when their valid times overlap.
    """
    if "number" not in da.dims:
        number = int(da["number"].values) if "number" in da.coords else 0
        da = da.drop_vars("number", errors="ignore").expand_dims(number=[number])

    extra = [d for d in da.dims if d not in ("number", "step", "time", "valid_time")]
    if extra:
        raise ValueError(f"Unexpected extra dimensions {extra}; select a single level first")

    df = da.to_dataframe(name="value").reset_index()
    if "step" in df.columns and "time" in df.columns:
        init = df["time"]  # GRIB convention: time = run, step = lead
    elif "forecast_reference_time" in df.columns:
        init = df["forecast_reference_time"]
    else:
        init = pd.Series(pd.NaT, index=df.index)

    if "valid_time" not in df.columns:
        if "time" not in df.columns:
            raise ValueError("Cannot find a time coordinate (valid_time/time)")
        step = pd.to_timedelta(df["step"]) if "step" in df.columns else pd.Timedelta(0)
        df["valid_time"] = pd.to_datetime(df["time"]) + step

    df["valid_time"] = _utc(df["valid_time"]).to_numpy()
    df["init_time"] = _utc(init).to_numpy()

    units = da.attrs.get("units", da.attrs.get("GRIB_units", ""))
    if units in ("K", "kelvin", "Kelvin"):
        df["value"] = df["value"] - 273.15
    return df[["init_time", "valid_time", "number", "value"]]


def long_to_member_frame(long: pd.DataFrame) -> pd.DataFrame:
    """Pivot to a member frame. Where runs overlap, the latest run wins."""
    if "init_time" in long.columns and long["init_time"].notna().any():
        latest = long.groupby("valid_time")["init_time"].transform("max")
        long = long[(long["init_time"] == latest) | latest.isna()]
    frame = long.pivot_table(
        index="valid_time", columns="number", values="value", aggfunc="first"
    ).sort_index()
    frame.columns = frame.columns.astype(int)
    frame.columns.name = "number"
    return frame


def load_gridded_points(
    paths: Iterable[str | Path],
    points: dict[str, tuple[float, float]],
    param: str | None = "2t",
    method: str = "bilinear",
) -> pd.DataFrame:
    """Open each file once and extract several points.

    Returns long rows (point, init_time, valid_time, number, value).
    """
    parts = []
    for path in map(Path, paths):
        for ds in _open_datasets(path):
            da = _pick_variable(ds, param)
            if da is None:
                continue
            for name, (lat, lon) in points.items():
                long = point_to_long_frame(extract_point(da, lat, lon, method))
                long.insert(0, "point", name)
                parts.append(long)
    if not parts:
        return pd.DataFrame(columns=["point", "init_time", "valid_time", "number", "value"])
    return pd.concat(parts, ignore_index=True)


def load_gridded_members(
    paths: Iterable[str | Path],
    lat: float,
    lon: float,
    param: str | None = "2t",
    method: str = "bilinear",
) -> pd.DataFrame:
    """Load GRIB2/NetCDF ensemble files and interpolate them to one point."""
    long = load_gridded_points(paths, {"p": (lat, lon)}, param=param, method=method)
    if long.empty:
        raise ValueError(f"No '{param}' fields found in the given files")
    return long_to_member_frame(long.drop(columns="point"))


# ---------------------------------------------------------------------------
# ECMWF open data (byte-range download)
# ---------------------------------------------------------------------------


def parse_index(text: str, param: str) -> list[dict]:
    """Records for ``param`` (control + perturbed) from an ECMWF .index file."""
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("param") == param and rec.get("type") in ("cf", "pf"):
            records.append(rec)
    return sorted(records, key=lambda r: int(r.get("number", 0)))


def _get(
    session: requests.Session,
    url: str,
    headers: dict | None = None,
    expect_grib: bool = False,
    max_retries: int = 10,
) -> bytes:
    """GET with exponential backoff (capped at 60 s) on S3 throttling (503 / SlowDown)."""
    delay = 2.0
    for attempt in range(max_retries + 1):
        r = session.get(url, headers=headers, timeout=120)
        if r.status_code == 404:
            raise FileNotFoundError(f"{url} not found (run not published yet, or expired)")
        throttled = r.status_code in (429, 503) or b"<Code>SlowDown</Code>" in r.content[:512]
        if not throttled:
            r.raise_for_status()
            if expect_grib and not r.content.startswith(b"GRIB"):
                raise ValueError(f"Response from {url} is not a GRIB message")
            return r.content
        if attempt == max_retries:
            break
        time.sleep(delay)
        delay = min(delay * 2, 60.0)
    raise RuntimeError(f"Still throttled after {max_retries} retries: {url}")


def download_ecmwf_open_data(
    run: pd.Timestamp,
    steps: Sequence[int],
    param: str = "2t",
    cache_dir: str | Path = ".cache/ecmwf",
    base_url: str = ECMWF_AWS_URL,
    model: str = "ifs",
    resol: str = "0p25",
    request_interval: float = 0.2,
    session: requests.Session | None = None,
) -> list[Path]:
    """Download one parameter of the ECMWF ensemble (stream ``enfo``).

    Each ensemble file holds every parameter for every member (~6 GB per
    step), so only the byte ranges listed for ``param`` in the matching
    ``.index`` file are fetched (~0.65 MB per member per step for 2t).
    Returns one cached GRIB2 file per step, holding all members.
    """
    run = pd.Timestamp(run)
    session = session or requests.Session()
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    files = []
    for step in steps:
        target = cache / f"{run:%Y%m%d%H}-{step}h-enfo-{param}.grib2"
        if target.exists():
            files.append(target)
            continue

        stem = (
            f"{base_url}/{run:%Y%m%d}/{run:%H}z/{model}/{resol}/enfo/"
            f"{run:%Y%m%d%H}0000-{step}h-enfo-ef"
        )
        records = parse_index(_get(session, stem + ".index").decode(), param)
        if not records:
            raise ValueError(f"No '{param}' records in {stem}.index")
        if len(records) != EXPECTED_MEMBERS:
            warnings.warn(f"step {step}h: {len(records)} members for '{param}' (expected {EXPECTED_MEMBERS})")

        # Resume an interrupted step: keep the members already written in full.
        tmp = target.with_suffix(".part")
        kept = 0
        if tmp.exists():
            have = tmp.stat().st_size
            while records and kept + int(records[0]["_length"]) <= have:
                kept += int(records.pop(0)["_length"])
            os.truncate(tmp, kept)
        with open(tmp, "ab") as out:
            for rec in records:
                start = int(rec["_offset"])
                end = start + int(rec["_length"]) - 1
                out.write(
                    _get(session, stem + ".grib2", headers={"Range": f"bytes={start}-{end}"}, expect_grib=True)
                )
                time.sleep(request_interval)
        tmp.rename(target)
        files.append(target)
    return files
