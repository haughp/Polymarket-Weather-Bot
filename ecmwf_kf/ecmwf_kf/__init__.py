"""ECMWF ensemble Kalman-filter bias correction.

Phase 1 (ingestion/alignment) lives in ingest, observations, ensemble and
align; phases 2-4 (filter, verification, plot) in kalman.

Every loader returns a *member frame*: a DataFrame indexed by UTC valid time
with one column per ensemble member, values in degrees Celsius.
"""

from .align import align_forecast_observations, daily_aggregate
from .ensemble import ensemble_stats
from .ingest import (
    download_ecmwf_open_data,
    extract_point,
    load_gridded_members,
    load_open_meteo_ensemble,
    parse_open_meteo_ensemble,
)
from .kalman import KalmanConfig, plot_kalman, run_kalman_filter, verification_summary
from .observations import fetch_nws_observations, load_observations_csv

__all__ = [
    "KalmanConfig",
    "align_forecast_observations",
    "daily_aggregate",
    "download_ecmwf_open_data",
    "ensemble_stats",
    "extract_point",
    "fetch_nws_observations",
    "load_gridded_members",
    "load_observations_csv",
    "load_open_meteo_ensemble",
    "parse_open_meteo_ensemble",
    "plot_kalman",
    "run_kalman_filter",
    "verification_summary",
]
