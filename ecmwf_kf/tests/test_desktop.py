import json

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from ecmwf_kf.desktop import daily_lead_members, main
from ecmwf_kf.observations import parse_iem_csv, update_observation_archive


def seasonal_c(valid_utc):
    """'True' temperature: diurnal cycle peaking ~20Z, in °C."""
    hours = (valid_utc - pd.Timestamp("2026-01-01", tz="UTC")) / pd.Timedelta(hours=1)
    return 18 + 6 * np.sin(2 * np.pi * (np.asarray(hours) % 24 - 14) / 24)


def write_runs(folder, runs, steps=range(0, 78, 6), members=5, model_bias=-2.0):
    """One NetCDF per run, laid out like cfgrib output (time=run, step=lead)."""
    rng = np.random.default_rng(0)
    lats, lons = [41.0, 40.5], [-74.0, -73.5]
    for run in runs:
        step = pd.to_timedelta(list(steps), unit="h")
        valid = run + step
        base = seasonal_c(valid) + model_bias + 273.15
        data = base[None, :, None, None] + rng.normal(0, 0.5, (members, len(step), 1, 1)) + np.zeros((1, 1, 2, 2))
        ds = xr.Dataset(
            {"t2m": (("number", "step", "latitude", "longitude"), data, {"units": "K"})},
            coords={
                "number": np.arange(1, members + 1),
                "step": step,
                "latitude": lats,
                "longitude": lons,
                "time": run.tz_localize(None),
                "valid_time": ("step", valid.tz_localize(None)),
            },
        )
        ds.to_netcdf(folder / f"ens_{run:%Y%m%d%H}.nc")


def test_daily_lead_members_picks_lead_and_latest_run():
    rows = []
    for run, bump in ((pd.Timestamp("2026-09-01T00", tz="UTC"), 0.0),
                      (pd.Timestamp("2026-09-01T12", tz="UTC"), 10.0)):
        for h in range(0, 60, 6):
            valid = run + pd.Timedelta(hours=h)
            for n in (1, 2):
                rows.append({"init_time": run, "valid_time": valid, "number": n,
                             "value": float(h) + bump + n})
    long = pd.DataFrame(rows)
    members, runs = daily_lead_members(long, "UTC", lead_days=1, min_samples=4)
    # Sep 2 at lead 1 exists for both runs; the 12z run is newer and wins
    assert list(members.index) == [pd.Timestamp("2026-09-02")]
    assert runs.iloc[0] == pd.Timestamp("2026-09-01T12", tz="UTC")
    # Sep 2 is steps 12-30h of the 12z run; max is step 30 (+10 bump, member 2)
    assert members.loc[pd.Timestamp("2026-09-02"), 2] == 30.0 + 10.0 + 2


def test_parse_iem_csv():
    text = "station,valid,tmpf\nLGA,2026-09-20 00:51,68.00\nLGA,2026-09-20 01:51,\nLGA,2026-09-20 02:51,50.00\n"
    obs = parse_iem_csv(text)
    assert list(obs.round(2)) == [20.0, 10.0]
    assert obs.index[0] == pd.Timestamp("2026-09-20T00:51", tz="UTC")


def test_observation_archive_merges(tmp_path):
    path = tmp_path / "obs" / "nyc.csv"
    t = pd.date_range("2026-09-01", periods=3, freq="h", tz="UTC")
    update_observation_archive(path, pd.Series([1.0, 2.0, 3.0], index=t, name="obs"))
    merged = update_observation_archive(path, pd.Series([9.0, 4.0], index=t[2:].append(t[2:] + pd.Timedelta(hours=1)), name="obs"))
    assert list(merged) == [1.0, 2.0, 9.0, 4.0]


def test_desktop_end_to_end(tmp_path, capsys):
    data, out, obs_dir = tmp_path / "ECMWF", tmp_path / "out", tmp_path / "obs_in"
    (data / "sub").mkdir(parents=True)
    obs_dir.mkdir()
    today = pd.Timestamp.now(tz="UTC").normalize()
    runs = pd.date_range(end=today, periods=40, freq="D")
    write_runs(data / "sub", runs)

    hours = pd.date_range(runs[0], today - pd.Timedelta(hours=1), freq="h")
    temp_f = seasonal_c(hours) * 9 / 5 + 32
    pd.DataFrame({"time": hours.strftime("%Y-%m-%dT%H:%MZ"), "value": temp_f}).to_csv(
        obs_dir / "nyc.csv", index=False)

    args = ["--data-dir", str(data), "--out-dir", str(out), "--cities", "nyc",
            "--obs-source", "csv", "--obs-dir", str(obs_dir), "--skip", "10"]
    assert main(args) == 0
    first = capsys.readouterr().out
    assert "reading" in first

    payload = json.loads((out / "forecasts.json").read_text())
    upcoming = payload["cities"]["nyc"]
    tomorrow = (pd.Timestamp.now(tz="America/New_York").normalize() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    assert any(f["date"] == tomorrow for f in upcoming)
    assert all(f["kalman_applied"] for f in upcoming)
    # Learned correction = 2 °C model bias + the peak that 6-hourly steps miss
    # (samples at 18z reach sin(4/24 * 2pi) of the 20z maximum).
    expected = 2.0 + 6 * (1 - np.sin(2 * np.pi * 4 / 24))
    f = next(f for f in upcoming if f["date"] == tomorrow)
    assert f["corrected_c"] - f["raw_mean_c"] == pytest.approx(expected, abs=0.4)

    ver = pd.read_csv(out / "verification.csv")
    raw = ver.loc[ver.forecast == "raw_ecmwf_mean", "RMSE"].item()
    kf = ver.loc[ver.forecast == "kalman_corrected", "RMSE"].item()
    assert kf < 0.5 * raw
    assert (out / "nyc_kalman.png").exists() and (out / "obs" / "nyc.csv").exists()

    # second run re-uses the per-file cache
    assert main(args) == 0
    assert "reading" not in capsys.readouterr().out
