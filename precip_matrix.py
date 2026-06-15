#!/usr/bin/env python3
"""Loader for the precipitation provider matrix (precip_provider_matrix.json).

Python analogue of src/matrix.ts (the temp matrix is TypeScript because its live
bot is TS; the precip path is entirely Python). Provides cached per-city
accessors with a fallback chain so the live pipeline degrades safely when the
matrix is cold/missing:

    matrix cell  →  MODEL_REGISTRY[city].model_name  →  'ecmwf_ec46'

Only ELIGIBLE cells return a provider/metrics; cells that are pooled, thin, or
low-confidence (HK/Seoul ERA5) are observation-only and treated as "no cell" by
the live path, so they never drive trades. is_pooled()/sample_count() expose the
raw state for the cold-start trade guard.
"""

from __future__ import annotations

import json
import os

MATRIX_PATH = os.path.join(os.path.dirname(__file__), "precip_provider_matrix.json")

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(MATRIX_PATH) as f:
                _cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _cache = {"cities": {}}
    return _cache


def reload_matrix() -> None:
    """Drop the cache (tests / after a rebuild)."""
    global _cache
    _cache = None


def _cell(city: str) -> dict | None:
    return _load().get("cities", {}).get(city)


def _eligible_cell(city: str) -> dict | None:
    """The cell only if it is eligible to drive live trading; else None."""
    cell = _cell(city)
    if cell and cell.get("eligible"):
        return cell
    return None


def get_precip_provider(city: str) -> str | None:
    """Eligible matrix provider for a city, else None (caller uses its default)."""
    cell = _eligible_cell(city)
    return cell.get("provider") if cell else None


def get_precip_brier(city: str) -> float | None:
    cell = _eligible_cell(city)
    return cell.get("brier") if cell else None


def get_precip_mae_mm(city: str) -> float | None:
    cell = _eligible_cell(city)
    return cell.get("mae_mm") if cell else None


def get_precip_bias_mm(city: str) -> float | None:
    cell = _eligible_cell(city)
    return cell.get("bias_mm") if cell else None


def is_pooled(city: str) -> bool:
    """True if the city's cell is a pooled (observation-only) pick."""
    cell = _cell(city)
    return bool(cell and cell.get("pooled"))


def sample_count(city: str) -> int:
    cell = _cell(city)
    return int(cell.get("samples", 0)) if cell else 0
