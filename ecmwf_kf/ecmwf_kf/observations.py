"""Ground-truth observations y_t as a UTC-indexed Series named ``obs`` (°C)."""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import requests

NWS_API = "https://api.weather.gov"
IEM_ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
USER_AGENT = "weatherbot-ts/1.0"
# NWS quality-control flags: X = rejected, Q = questioned
_BAD_QC = {"X", "Q"}


def _finish(times, values) -> pd.Series:
    obs = pd.Series(values, index=pd.DatetimeIndex(times), name="obs", dtype=float)
    obs.index.name = "time"
    obs = obs.dropna().sort_index()
    return obs[~obs.index.duplicated(keep="first")]


def load_observations_csv(
    path: str,
    time_col: str = "time",
    value_col: str = "value",
    units: str = "C",
    tz: str = "UTC",
) -> pd.Series:
    """Read a CSV of observations. Naive timestamps are taken to be in ``tz``."""
    df = pd.read_csv(path)
    times = pd.to_datetime(df[time_col])
    if times.dt.tz is None:
        times = times.dt.tz_localize(tz)
    values = pd.to_numeric(df[value_col], errors="coerce")
    if units.upper() == "F":
        values = (values - 32.0) * 5.0 / 9.0
    elif units.upper() == "K":
        values = values - 273.15
    return _finish(times.dt.tz_convert("UTC"), values.to_numpy())


def parse_nws_observations(payload: dict) -> tuple[list, list]:
    times, values = [], []
    for feature in payload.get("features", []):
        props = feature.get("properties", {})
        temp = props.get("temperature") or {}
        value = temp.get("value")
        if value is None or temp.get("qualityControl") in _BAD_QC:
            continue
        times.append(pd.Timestamp(props["timestamp"]).tz_convert("UTC"))
        values.append(float(value))
    return times, values


def fetch_nws_observations(
    station: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    session: requests.Session | None = None,
    max_pages: int = 50,
) -> pd.Series:
    """Station temperature observations from api.weather.gov (°C)."""
    session = session or requests.Session()
    url = f"{NWS_API}/stations/{station}/observations"
    params: dict | None = {
        "start": pd.Timestamp(start).tz_convert("UTC").isoformat(),
        "end": pd.Timestamp(end).tz_convert("UTC").isoformat(),
    }
    times, values = [], []
    for _ in range(max_pages):
        r = session.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        payload = r.json()
        t, v = parse_nws_observations(payload)
        if not t:
            break
        times += t
        values += v
        url = (payload.get("pagination") or {}).get("next")
        params = None
        if not url:
            break
    return _finish(times, values)


def parse_iem_csv(text: str) -> pd.Series:
    """Parse an IEM ASOS CSV (station,valid,tmpf) into °C observations."""
    df = pd.read_csv(io.StringIO(text), comment="#")
    if df.empty or "tmpf" not in df.columns:
        return _finish([], [])
    times = pd.to_datetime(df["valid"]).dt.tz_localize("UTC")
    temp_c = (pd.to_numeric(df["tmpf"], errors="coerce") - 32.0) * 5.0 / 9.0
    return _finish(times, temp_c.to_numpy())


def fetch_iem_observations(
    station: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    session: requests.Session | None = None,
) -> pd.Series:
    """Hourly + special METAR temperatures from the Iowa Environmental Mesonet.

    Unlike api.weather.gov (about a week of history) IEM keeps the full
    archive, so it can backfill months of observations in one request.
    """
    sid = station[1:] if len(station) == 4 and station.startswith("K") else station
    start = pd.Timestamp(start).tz_convert("UTC")
    end = pd.Timestamp(end).tz_convert("UTC") + pd.Timedelta(days=1)
    params = [
        ("station", sid), ("data", "tmpf"), ("tz", "Etc/UTC"), ("format", "onlycomma"),
        ("latlon", "no"), ("missing", "empty"), ("trace", "empty"),
        ("report_type", "3"), ("report_type", "4"),
        ("year1", start.year), ("month1", start.month), ("day1", start.day),
        ("year2", end.year), ("month2", end.month), ("day2", end.day),
    ]
    r = (session or requests).get(IEM_ASOS, params=params, timeout=120)
    r.raise_for_status()
    return parse_iem_csv(r.text)


def update_observation_archive(path: str | Path, new: pd.Series) -> pd.Series:
    """Merge new observations into a local CSV archive (time,value in °C).

    Newer values win on duplicate timestamps. Returns the full archive.
    """
    path = Path(path)
    if path.exists():
        old = load_observations_csv(str(path))
        merged = pd.concat([old, new])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = new.sort_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.rename("value").rename_axis("time").to_csv(path)
    return merged.rename("obs")
