import json

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from ecmwf_kf.align import align_forecast_observations, daily_aggregate
from ecmwf_kf.cli import parse_steps
from ecmwf_kf.ensemble import ensemble_stats
from ecmwf_kf.ingest import (
    extract_point,
    long_to_member_frame,
    parse_index,
    parse_open_meteo_ensemble,
    point_to_long_frame,
)
from ecmwf_kf.observations import parse_nws_observations


def utc_range(start, periods, freq="3h"):
    return pd.date_range(start, periods=periods, freq=freq, tz="UTC")


# --- ensemble statistics -------------------------------------------------


def test_ensemble_stats_matches_formulas():
    rng = np.random.default_rng(0)
    values = rng.normal(15, 2, size=(4, 51))
    members = pd.DataFrame(values, index=utc_range("2026-09-01", 4))
    stats = ensemble_stats(members)
    np.testing.assert_allclose(stats["ens_mean"], values.sum(axis=1) / 51)
    expected_var = ((values - values.mean(axis=1, keepdims=True)) ** 2).sum(axis=1) / 50
    np.testing.assert_allclose(stats["ens_var"], expected_var)
    assert (stats["n_members"] == 51).all()


def test_ensemble_stats_counts_members_per_row_and_drops_thin_rows():
    members = pd.DataFrame(
        [[1.0, 3.0, np.nan], [np.nan, 5.0, np.nan]], index=utc_range("2026-09-01", 2)
    )
    stats = ensemble_stats(members)
    assert len(stats) == 1
    assert stats["ens_mean"].iloc[0] == 2.0
    assert stats["ens_var"].iloc[0] == 2.0
    assert stats["n_members"].iloc[0] == 2


# --- spatial interpolation ------------------------------------------------


def linear_field(lats, lons):
    # f = 2*lat + 3*lon is reproduced exactly by bilinear interpolation
    return xr.DataArray(
        2 * np.asarray(lats)[:, None] + 3 * np.asarray(lons)[None, :],
        dims=("latitude", "longitude"),
        coords={"latitude": lats, "longitude": lons},
        attrs={"units": "K"},
    )


def test_bilinear_is_exact_on_linear_field_with_descending_lats():
    da = linear_field([42.0, 41.75, 41.5], [-88.0, -87.75, -87.5])
    out = extract_point(da, 41.9742, -87.9073)
    assert float(out) == pytest.approx(2 * 41.9742 + 3 * -87.9073)
    assert out.attrs["units"] == "K"


def test_nearest_and_0_360_longitudes():
    da = linear_field([41.0, 40.75, 40.5], [285.75, 286.0, 286.25])
    out = extract_point(da, 40.7772, -73.8726, method="nearest")  # -73.87 -> 286.13
    assert float(out) == 2 * 40.75 + 3 * 286.25
    assert float(extract_point(da, 40.7772, -73.8726)) == pytest.approx(
        2 * 40.7772 + 3 * (360 - 73.8726)
    )


def test_bilinear_outside_grid_raises():
    da = linear_field([1.0, 0.0], [0.0, 1.0])
    with pytest.raises(ValueError):
        extract_point(da, 5.0, 0.5)


# --- gridded -> member frame ----------------------------------------------


def test_gridded_ensemble_to_member_frame_converts_kelvin():
    lats, lons = [41.0, 40.0], [-74.0, -73.0]
    steps = pd.to_timedelta([0, 3], unit="h")
    run = pd.Timestamp("2026-09-23T00:00")
    data = 280.0 + np.arange(3)[:, None, None, None] + np.zeros((3, 2, 2, 2))
    pf = xr.DataArray(
        data,
        dims=("number", "step", "latitude", "longitude"),
        coords={
            "number": [1, 2, 3],
            "step": steps,
            "latitude": lats,
            "longitude": lons,
            "time": run,
            "valid_time": ("step", (run + steps).values),
        },
        attrs={"units": "K"},
    )
    cf = pf.isel(number=0).drop_vars("number").assign_coords(number=0) - 1.0
    cf.attrs = {"units": "K"}

    long = pd.concat(
        [point_to_long_frame(extract_point(d, 40.5, -73.5)) for d in (pf, cf)]
    )
    frame = long_to_member_frame(long)
    assert list(frame.columns) == [0, 1, 2, 3]
    assert list(frame.index) == list(pd.DatetimeIndex(run + steps).tz_localize("UTC"))
    np.testing.assert_allclose(frame.iloc[0].to_numpy(), [5.85, 6.85, 7.85, 8.85])


def test_point_frame_without_valid_time_uses_time_plus_step():
    da = xr.DataArray(
        [273.15, 274.15],
        dims=("time",),
        coords={"time": pd.to_datetime(["2026-09-01", "2026-09-02"]), "step": pd.Timedelta(hours=6)},
        attrs={"units": "K"},
    )
    long = point_to_long_frame(da)
    assert list(long["number"]) == [0, 0]
    assert long["valid_time"].iloc[0] == pd.Timestamp("2026-09-01T06:00", tz="UTC")
    np.testing.assert_allclose(long["value"], [0.0, 1.0])


# --- Open-Meteo / index / NWS parsers ---------------------------------------


def test_parse_open_meteo_ensemble():
    payload = {
        "utc_offset_seconds": 0,
        "hourly_units": {"temperature_2m": "°C"},
        "hourly": {
            "time": ["2026-09-01T00:00", "2026-09-01T01:00"],
            "temperature_2m": [10.0, 11.0],
            "temperature_2m_member01": [12.0, 13.0],
            "temperature_2m_member02": [14.0, None],
            "relative_humidity_2m": [50, 60],
        },
    }
    frame = parse_open_meteo_ensemble(payload)
    assert list(frame.columns) == [0, 1, 2]
    assert frame.index[0] == pd.Timestamp("2026-09-01T00:00", tz="UTC")
    assert np.isnan(frame.loc[frame.index[1], 2])


def test_parse_open_meteo_fahrenheit_and_offset():
    payload = {
        "utc_offset_seconds": -4 * 3600,
        "hourly_units": {"temperature_2m": "°F"},
        "hourly": {"time": ["2026-09-01T20:00"], "temperature_2m": [212.0]},
    }
    frame = parse_open_meteo_ensemble(payload)
    assert frame.index[0] == pd.Timestamp("2026-09-02T00:00", tz="UTC")
    assert frame.iloc[0, 0] == pytest.approx(100.0)


def test_parse_index_selects_param_members():
    lines = [
        {"type": "pf", "number": "2", "param": "2t", "_offset": 20, "_length": 5},
        {"type": "pf", "number": "1", "param": "2t", "_offset": 10, "_length": 5},
        {"type": "cf", "param": "2t", "_offset": 0, "_length": 5},
        {"type": "pf", "number": "1", "param": "mx2t3", "_offset": 30, "_length": 5},
        {"type": "em", "param": "2t", "_offset": 40, "_length": 5},
    ]
    recs = parse_index("\n".join(json.dumps(l) for l in lines) + "\n", "2t")
    assert [r["_offset"] for r in recs] == [0, 10, 20]


def test_parse_nws_observations_drops_nulls_and_rejected():
    feat = lambda ts, v, qc="V": {
        "properties": {"timestamp": ts, "temperature": {"value": v, "qualityControl": qc}}
    }
    payload = {
        "features": [
            feat("2026-09-01T12:51:00+00:00", 20.0),
            feat("2026-09-01T13:51:00+00:00", None),
            feat("2026-09-01T14:51:00+00:00", 99.0, "X"),
        ]
    }
    times, values = parse_nws_observations(payload)
    assert values == [20.0]


# --- alignment ----------------------------------------------------------------


def test_align_nearest_within_tolerance_and_drop_missing():
    stats = ensemble_stats(
        pd.DataFrame(
            [[10.0, 12.0], [11.0, 13.0], [12.0, 14.0]],
            index=utc_range("2026-09-01T00:00", 3),
        )
    )
    obs = pd.Series(
        [10.5, 99.0],
        index=pd.DatetimeIndex(["2026-08-31T23:51", "2026-09-01T04:00"], tz="UTC"),
        name="obs",
    )
    aligned = align_forecast_observations(stats, obs, tolerance="30min")
    assert list(aligned.index) == [pd.Timestamp("2026-09-01T00:00", tz="UTC")]
    assert aligned["obs"].iloc[0] == 10.5
    assert aligned["obs_time"].iloc[0] == pd.Timestamp("2026-08-31T23:51", tz="UTC")


def test_daily_max_is_per_member_before_stats():
    idx = pd.DatetimeIndex(["2026-09-01T16:00", "2026-09-01T20:00"], tz="UTC")
    members = pd.DataFrame({1: [30.0, 20.0], 2: [20.0, 30.0]}, index=idx)
    daily = daily_aggregate(members, "max", tz="America/New_York")
    stats = ensemble_stats(daily)
    # each member peaks at 30; max of the hourly mean would only be 25
    assert stats["ens_mean"].iloc[0] == 30.0
    assert stats["ens_var"].iloc[0] == 0.0


def test_daily_aggregate_uses_local_day_and_min_samples():
    idx = pd.DatetimeIndex(
        ["2026-09-02T01:00", "2026-09-02T03:00", "2026-09-02T14:00"], tz="UTC"
    )
    obs = pd.Series([25.0, 22.0, 18.0], index=idx, name="obs")
    daily = daily_aggregate(obs, "max", tz="America/New_York", min_samples=2)
    # 01:00Z and 03:00Z are Sep 1 evening in New York (UTC-4); Sep 2 has one sample
    assert daily.loc[pd.Timestamp("2026-09-01")] == 25.0
    assert np.isnan(daily.loc[pd.Timestamp("2026-09-02")])


def test_align_daily_exact_join():
    dates = pd.to_datetime(["2026-09-01", "2026-09-02"]).rename("date")
    stats = pd.DataFrame(
        {"ens_mean": [20.0, 21.0], "ens_var": [1.0, 1.0], "n_members": [51, 51]}, index=dates
    )
    obs = pd.Series([19.0], index=dates[1:], name="obs")
    aligned = align_forecast_observations(stats, obs)
    assert list(aligned.index) == [pd.Timestamp("2026-09-02")]


def test_parse_steps():
    assert parse_steps("0-9/3") == [0, 3, 6, 9]
    assert parse_steps("0,6,144-150/6") == [0, 6, 144, 150]
