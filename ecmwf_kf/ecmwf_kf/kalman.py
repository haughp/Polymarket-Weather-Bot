"""Online 2-parameter Kalman filter (dynamic linear model) for ECMWF bias correction.

State:        theta_t = [alpha_t, beta_t]'
Observation:  y_t = alpha_t + beta_t * ens_mean_t + v_t,  v_t ~ N(0, V_t)
              V_t = sigma0^2 + gamma * ens_var_t
Transition:   theta_t = theta_{t-1} + w_t,               w_t ~ N(0, W),  W = diag(s_a^2, s_b^2)

The corrected forecast at t uses the prior state (everything up to t-1), so it
is a genuine out-of-sample forecast and can be verified against y_t.

Run on a Phase 1 table:
  python -m ecmwf_kf.kalman aligned.csv --out kalman.csv --plot kalman.png
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class KalmanConfig:
    theta0: tuple[float, float] = (0.0, 1.0)
    P0: tuple[float, float] = (10.0, 1.0)            # diag of initial state covariance
    W: tuple[float, float] = (1e-4, 1e-5)            # diag of process noise
    gamma: float = 1.0                               # weight of ensemble variance in V_t
    sigma0_sq: float | None = None                   # None: estimate from the first window
    sigma0_window: int = 30


def estimate_sigma0_sq(y: np.ndarray, ens_mean: np.ndarray, window: int = 30) -> float:
    """sigma0^2 = var(y - ens_mean) over the first ``window`` observed rows."""
    err = y - ens_mean
    err = err[~np.isnan(err)][:window]
    if err.size < 2:
        raise ValueError("Need at least 2 observations in the first window to estimate sigma0^2")
    return float(np.var(err, ddof=1))


def run_kalman_filter(
    aligned: pd.DataFrame,
    config: KalmanConfig | None = None,
    obs_col: str = "obs",
    mean_col: str = "ens_mean",
    var_col: str = "ens_var",
) -> pd.DataFrame:
    """Filter a Phase 1 table and return one row per time step.

    Rows with a missing observation get a forecast but no update (the state
    carries forward), so future steps can be forecast before y_t arrives.
    """
    cfg = config or KalmanConfig()
    x = aligned[mean_col].to_numpy(dtype=float)
    s2 = aligned[var_col].to_numpy(dtype=float)
    y = aligned[obs_col].to_numpy(dtype=float) if obs_col in aligned else np.full(len(x), np.nan)
    if np.isnan(x).any() or np.isnan(s2).any():
        raise ValueError("ens_mean / ens_var contain NaN; drop those rows first")

    sigma0_sq = cfg.sigma0_sq
    if sigma0_sq is None:
        sigma0_sq = estimate_sigma0_sq(y, x, cfg.sigma0_window)

    n = len(x)
    theta = np.array(cfg.theta0, dtype=float)
    P = np.diag(np.asarray(cfg.P0, dtype=float))
    W = np.diag(np.asarray(cfg.W, dtype=float))
    I = np.eye(2)

    out = {k: np.full(n, np.nan) for k in (
        "corrected_forecast", "forecast_std", "innovation",
        "estimated_bias_alpha", "estimated_slope_beta",
        "kalman_gain", "kalman_gain_alpha", "kalman_gain_beta", "obs_noise_var",
    )}

    for t in range(n):
        # predict (random-walk state): theta_{t|t-1} = theta_{t-1|t-1}, P += W
        P = P + W
        H = np.array([1.0, x[t]])
        V = sigma0_sq + cfg.gamma * s2[t]
        y_hat = H @ theta
        S = H @ P @ H + V

        out["corrected_forecast"][t] = y_hat
        out["forecast_std"][t] = np.sqrt(S)
        out["obs_noise_var"][t] = V

        if not np.isnan(y[t]):
            e = y[t] - y_hat
            K = P @ H / S
            theta = theta + K * e
            P = (I - np.outer(K, H)) @ P
            P = 0.5 * (P + P.T)  # keep symmetric against round-off
            out["innovation"][t] = e
            out["kalman_gain_alpha"][t], out["kalman_gain_beta"][t] = K
            out["kalman_gain"][t] = H @ K  # share of the innovation absorbed by the forecast

        out["estimated_bias_alpha"][t], out["estimated_slope_beta"][t] = theta

    result = pd.DataFrame(
        {
            "timestamp": aligned.index,
            "actual_y": y,
            "ecmwf_mean": x,
            "ecmwf_var": s2,
            **out,
        }
    )
    result.attrs["sigma0_sq"] = sigma0_sq
    return result


def _metrics(err: np.ndarray) -> tuple[float, float]:
    return float(np.mean(np.abs(err))), float(np.sqrt(np.mean(err ** 2)))


def verification_summary(result: pd.DataFrame, skip: int = 0) -> pd.DataFrame:
    """MAE and RMSE of the raw ensemble mean vs the Kalman-corrected forecast.

    ``skip`` drops the first rows (the filter's spin-up) from the scores.
    """
    scored = result.iloc[skip:].dropna(subset=["actual_y"])
    rows = {}
    for label, col in (("raw_ecmwf_mean", "ecmwf_mean"), ("kalman_corrected", "corrected_forecast")):
        mae, rmse = _metrics(scored["actual_y"].to_numpy() - scored[col].to_numpy())
        rows[label] = {"MAE": mae, "RMSE": rmse, "N": len(scored)}
    summary = pd.DataFrame(rows).T
    raw, kf = summary.loc["raw_ecmwf_mean"], summary.loc["kalman_corrected"]
    summary.loc["improvement_%", ["MAE", "RMSE"]] = [
        100 * (1 - kf["MAE"] / raw["MAE"]) if raw["MAE"] else np.nan,
        100 * (1 - kf["RMSE"] / raw["RMSE"]) if raw["RMSE"] else np.nan,
    ]
    summary["N"] = summary["N"].astype("Int64")
    return summary


def plot_kalman(result: pd.DataFrame, path: str | None = None, title: str | None = None):
    """Actuals, raw ECMWF, corrected forecast, and the alpha/beta trajectories."""
    import matplotlib

    if path:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = pd.to_datetime(result["timestamp"])
    fig, (ax1, ax2, ax3) = plt.subplots(
        3, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1.2, 1.2]}
    )
    ax1.plot(t, result["actual_y"], "o", ms=3, color="black", label="Actual (y)")
    ax1.plot(t, result["ecmwf_mean"], color="tab:orange", lw=1.2, label="Raw ECMWF ens. mean")
    ax1.plot(t, result["corrected_forecast"], color="tab:blue", lw=1.5, label="Kalman corrected")
    ax1.fill_between(
        t,
        result["corrected_forecast"] - result["forecast_std"],
        result["corrected_forecast"] + result["forecast_std"],
        color="tab:blue", alpha=0.15, lw=0, label="±1σ (S_t)",
    )
    ax1.set_ylabel("Temperature (°C)")
    ax1.legend(loc="best", fontsize=8)
    ax1.set_title(title or "ECMWF ensemble Kalman-filter bias correction")

    ax2.plot(t, result["estimated_bias_alpha"], color="tab:red")
    ax2.axhline(0, color="grey", lw=0.6, ls="--")
    ax2.set_ylabel("α (bias)")
    ax3.plot(t, result["estimated_slope_beta"], color="tab:green")
    ax3.axhline(1, color="grey", lw=0.6, ls="--")
    ax3.set_ylabel("β (slope)")
    for ax in (ax1, ax2, ax3):
        ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    if path:
        fig.savefig(path, dpi=130)
        plt.close(fig)
    return fig


def load_aligned(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ecmwf_kf.kalman", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("aligned", help="Phase 1 output CSV (ens_mean, ens_var, obs)")
    p.add_argument("--out", default="kalman.csv")
    p.add_argument("--plot", help="save the diagnostic plot to this PNG")
    p.add_argument("--gamma", type=float, default=1.0, help="weight of ensemble variance in V_t")
    p.add_argument("--sigma0-sq", type=float, help="fix sigma0^2 instead of estimating it")
    p.add_argument("--sigma0-window", type=int, default=30)
    p.add_argument("--w-alpha", type=float, default=1e-4, help="process variance of alpha")
    p.add_argument("--w-beta", type=float, default=1e-5, help="process variance of beta")
    p.add_argument("--skip", type=int, default=None,
                   help="rows excluded from scores as spin-up (default: sigma0 window)")
    args = p.parse_args(argv)

    cfg = KalmanConfig(W=(args.w_alpha, args.w_beta), gamma=args.gamma,
                       sigma0_sq=args.sigma0_sq, sigma0_window=args.sigma0_window)
    result = run_kalman_filter(load_aligned(args.aligned), cfg)
    result.to_csv(args.out, index=False)

    print(f"sigma0^2 = {result.attrs['sigma0_sq']:.4f}   rows = {len(result)}   -> {args.out}")
    last = result.iloc[-1]
    print(f"final alpha = {last['estimated_bias_alpha']:+.3f}, beta = {last['estimated_slope_beta']:.4f}")
    print("\nVerification (all rows):")
    print(verification_summary(result).round(3).to_string())
    skip = args.sigma0_window if args.skip is None else args.skip
    if skip and len(result) > skip:
        print(f"\nVerification (excluding first {skip} spin-up rows):")
        print(verification_summary(result, skip=skip).round(3).to_string())
    if args.plot:
        plot_kalman(result, args.plot)
        print(f"\nplot -> {args.plot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
