import numpy as np
import pandas as pd
import pytest

from ecmwf_kf.kalman import (
    KalmanConfig,
    estimate_sigma0_sq,
    load_aligned,
    main,
    plot_kalman,
    run_kalman_filter,
    verification_summary,
)


def synthetic(n=400, alpha=2.0, beta=0.9, noise=0.8, seed=1):
    """Observations from a known bias/slope applied to a seasonal ensemble mean."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    ens_mean = 15 + 8 * np.sin(2 * np.pi * t / 120) + rng.normal(0, 2, n)
    ens_var = rng.uniform(0.2, 1.5, n)
    obs = alpha + beta * ens_mean + rng.normal(0, noise, n)
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"ens_mean": ens_mean, "ens_var": ens_var, "obs": obs}, index=idx)


def test_first_step_matches_hand_computed_equations():
    df = pd.DataFrame({"ens_mean": [20.0], "ens_var": [0.5], "obs": [23.0]})
    cfg = KalmanConfig(sigma0_sq=1.0, gamma=2.0)
    r = run_kalman_filter(df, cfg).iloc[0]

    P = np.diag([10.0, 1.0]) + np.diag([1e-4, 1e-5])
    H = np.array([1.0, 20.0])
    V = 1.0 + 2.0 * 0.5
    y_hat = H @ np.array([0.0, 1.0])
    S = H @ P @ H + V
    K = P @ H / S
    theta = np.array([0.0, 1.0]) + K * (23.0 - y_hat)

    assert r["corrected_forecast"] == pytest.approx(20.0)
    assert r["forecast_std"] == pytest.approx(np.sqrt(S))
    assert r["obs_noise_var"] == pytest.approx(V)
    assert r["kalman_gain_alpha"] == pytest.approx(K[0])
    assert r["kalman_gain_beta"] == pytest.approx(K[1])
    assert r["kalman_gain"] == pytest.approx(H @ K)
    assert r["estimated_bias_alpha"] == pytest.approx(theta[0])
    assert r["estimated_slope_beta"] == pytest.approx(theta[1])


def test_sigma0_from_first_window():
    y = np.array([1.0, 2.0, 4.0, 100.0, np.nan])
    x = np.zeros(5)
    assert estimate_sigma0_sq(y, x, window=3) == pytest.approx(np.var([1, 2, 4], ddof=1))
    with pytest.raises(ValueError):
        estimate_sigma0_sq(np.array([1.0, np.nan]), np.zeros(2), window=2)


def test_filter_learns_known_bias_and_beats_raw_forecast():
    df = synthetic(alpha=4.0, beta=0.9, noise=0.8)  # raw bias ~ +2.5 at x = 15
    result = run_kalman_filter(df)
    tail = result.iloc[-50:]
    # alpha and beta trade off along the mean of x, so check the implied correction
    implied = tail["estimated_bias_alpha"] + tail["estimated_slope_beta"] * 15.0
    assert implied.mean() == pytest.approx(4.0 + 0.9 * 15.0, abs=0.5)

    summary = verification_summary(result, skip=30)
    assert summary.loc["raw_ecmwf_mean", "RMSE"] > 2.0
    assert summary.loc["kalman_corrected", "RMSE"] < 1.15 * 0.8  # near the noise floor
    assert summary.loc["kalman_corrected", "MAE"] < summary.loc["raw_ecmwf_mean", "MAE"]
    assert summary.loc["raw_ecmwf_mean", "N"] == len(df) - 30


def test_forecast_is_out_of_sample():
    df = synthetic(n=60)
    base = run_kalman_filter(df)
    changed = df.copy()
    changed.iloc[40, changed.columns.get_loc("obs")] += 50.0
    after = run_kalman_filter(changed, KalmanConfig(sigma0_sq=base.attrs["sigma0_sq"]))
    # y_40 must not influence the forecast for t=40, only later ones
    assert after["corrected_forecast"].iloc[40] == pytest.approx(base["corrected_forecast"].iloc[40])
    assert after["corrected_forecast"].iloc[41] != pytest.approx(base["corrected_forecast"].iloc[41])


def test_missing_observation_skips_update():
    df = synthetic(n=40)
    df.iloc[35, df.columns.get_loc("obs")] = np.nan
    r = run_kalman_filter(df)
    assert np.isnan(r["innovation"].iloc[35]) and np.isnan(r["kalman_gain"].iloc[35])
    assert r["estimated_bias_alpha"].iloc[35] == r["estimated_bias_alpha"].iloc[34]
    assert not np.isnan(r["corrected_forecast"].iloc[35])


def test_output_columns():
    r = run_kalman_filter(synthetic(n=40))
    for col in ("timestamp", "actual_y", "ecmwf_mean", "corrected_forecast",
                "estimated_bias_alpha", "estimated_slope_beta", "kalman_gain"):
        assert col in r.columns


def test_cli_and_plot(tmp_path, capsys):
    aligned = tmp_path / "aligned.csv"
    synthetic(n=80).rename_axis("date").to_csv(aligned)
    out, png = tmp_path / "kf.csv", tmp_path / "kf.png"
    assert main([str(aligned), "--out", str(out), "--plot", str(png)]) == 0
    assert png.stat().st_size > 10_000
    assert len(pd.read_csv(out)) == 80
    assert "improvement_%" in capsys.readouterr().out
    assert isinstance(load_aligned(str(aligned)).index, pd.DatetimeIndex)
