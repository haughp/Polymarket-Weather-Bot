"""ECMWF ensemble ingestion and observation alignment (Phase 1 of the
ensemble Kalman-filter bias-correction pipeline).

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
from .observations import fetch_nws_observations, load_observations_csv

__all__ = [
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
]
