"""Time alignment of ensemble statistics with observations."""

from __future__ import annotations

import pandas as pd

STAT_COLUMNS = ["ens_mean", "ens_var", "n_members"]


def daily_aggregate(
    data: pd.DataFrame | pd.Series,
    how: str = "max",
    tz: str = "UTC",
    min_samples: int = 1,
) -> pd.DataFrame | pd.Series:
    """Aggregate each column over local calendar days in ``tz``.

    Apply this to the member frame *before* ``ensemble_stats`` so the spread
    reflects each member's own daily max (the max of the ensemble mean is not
    the mean of the members' maxima). Days with fewer than ``min_samples``
    values in a column are set to NaN, which guards against partial days.
    """
    local = data.tz_convert(tz)
    days = local.index.tz_localize(None).normalize()
    grouped = local.groupby(days)
    out = grouped.agg(how)
    out = out.where(grouped.count() >= min_samples)
    out.index.name = "date"
    return out


def align_forecast_observations(
    stats: pd.DataFrame,
    obs: pd.Series,
    tolerance: str | pd.Timedelta = "30min",
) -> pd.DataFrame:
    """Pair each forecast step with its observation and drop incomplete rows.

    Timestamp indexes are matched to the nearest observation within
    ``tolerance`` (station reports rarely land exactly on the model step,
    e.g. METARs at :51). Daily (date) indexes are matched exactly.
    """
    obs = obs.dropna().sort_index().rename("obs")
    stats = stats[STAT_COLUMNS].sort_index()

    if isinstance(stats.index, pd.DatetimeIndex) and stats.index.tz is not None:
        left = stats.rename_axis("valid_time").reset_index()
        right = pd.DataFrame({"obs_time": obs.index, "obs": obs.to_numpy()})
        right["obs_time"] = right["obs_time"].dt.tz_convert("UTC")
        left["valid_time"] = left["valid_time"].dt.tz_convert("UTC")
        merged = pd.merge_asof(
            left,
            right,
            left_on="valid_time",
            right_on="obs_time",
            direction="nearest",
            tolerance=pd.Timedelta(tolerance),
        ).set_index("valid_time")
    else:
        merged = stats.join(obs, how="inner")

    return merged.dropna(subset=["ens_mean", "ens_var", "obs"])
