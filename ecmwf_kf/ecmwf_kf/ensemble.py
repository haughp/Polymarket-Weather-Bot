"""Ensemble summary statistics."""

from __future__ import annotations

import pandas as pd


def ensemble_stats(members: pd.DataFrame, min_members: int = 2) -> pd.DataFrame:
    """Per-time-step ensemble mean and unbiased spread.

    ``members`` has one row per valid time and one column per member.

        ens_mean_t = (1/M) * sum_i x_{i,t}
        ens_var_t  = 1/(M-1) * sum_i (x_{i,t} - ens_mean_t)^2

    M is counted per row, so a member missing at one time step is left out of
    that step only. Rows with fewer than ``min_members`` valid members are
    dropped (the sample variance needs at least two).
    """
    if min_members < 2:
        raise ValueError("min_members must be >= 2 for the sample variance")
    n = members.count(axis=1)
    stats = pd.DataFrame(
        {
            "ens_mean": members.mean(axis=1, skipna=True),
            "ens_var": members.var(axis=1, ddof=1, skipna=True),
            "n_members": n,
        }
    )
    return stats[n >= min_members]
