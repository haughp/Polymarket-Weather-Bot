"""Unit tests for the precipitation provider matrix: bucket/Brier math,
provider selection (pooled fallback, hysteresis, eligibility), and the loader
fallback chain. Pure-function tests — no DB or network.

    python3 -m pytest test_precip_matrix.py -v
"""

import json
import math

import pytest

from precip_provider_backtest import (
    assign_bucket,
    ensemble_bucket_probs,
    brier_for_sample,
    build_matrix,
    MIN_SAMPLES,
    IMPROVEMENT_THRESHOLD,
)
import precip_matrix


# Standard 4-bucket ladder used across tests (native units, e.g. inches).
LADDER = [(None, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, None)]


# ── Bucket assignment ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (0.0, 0),    # below 1.0 → open-low bucket
    (0.99, 0),
    (1.0, 1),    # boundary belongs to the upper bucket [1,2)
    (1.5, 1),
    (2.0, 2),
    (2.999, 2),
    (3.0, 3),    # open-high bucket
    (10.0, 3),
])
def test_assign_bucket(value, expected):
    assert assign_bucket(value, LADDER) == expected


# ── Ensemble bucket probabilities ─────────────────────────────────────────────

def test_probs_sum_to_one():
    probs = ensemble_bucket_probs(1.0, 2.0, 3.0, LADDER)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)
    assert all(p >= 0 for p in probs)


def test_probs_mass_concentrates_near_median():
    # A tight band around 2.5 should put most mass in bucket [2,3).
    probs = ensemble_bucket_probs(2.3, 2.5, 2.7, LADDER)
    assert probs[2] == max(probs)


def test_probs_degenerate_band_uniform_fallback():
    # All three quantiles identical and on a boundary → no NaN, valid distribution.
    probs = ensemble_bucket_probs(2.0, 2.0, 2.0, LADDER)
    assert math.isclose(sum(probs), 1.0, abs_tol=1e-9)


# ── Brier ─────────────────────────────────────────────────────────────────────

def test_brier_perfect_is_zero():
    assert brier_for_sample([0.0, 1.0, 0.0, 0.0], 1) == 0.0


def test_brier_worst_is_two():
    # All mass on the wrong single bucket → (1-0)^2 + (0-1)^2 = 2.
    assert brier_for_sample([1.0, 0.0, 0.0, 0.0], 1) == pytest.approx(2.0)


def test_brier_uniform():
    probs = [0.25, 0.25, 0.25, 0.25]
    # sum (0.25-onehot)^2 = 3*0.0625 + 0.5625 = 0.75
    assert brier_for_sample(probs, 0) == pytest.approx(0.75)


# ── Selection (build_matrix) ──────────────────────────────────────────────────

def _metrics(brier, samples, mae=5.0, bias=0.0, hit=0.7):
    return {"brier": brier, "samples": samples, "mae_mm": mae, "bias_mm": bias, "bucket_hit": hit}


def test_independent_pick_when_mature(monkeypatch):
    # seattle has a mature, low-Brier provider → eligible independent pick.
    import precip_provider_backtest as ppb
    monkeypatch.setattr(ppb, "PRECIP_LOCATIONS", {
        "seattle": {"data_confidence": "high", "units": "inches"},
    })
    scores = {"seattle": {
        "ecmwf_ifs025": _metrics(0.30, MIN_SAMPLES + 2),
        "ecmwf_aifs025": _metrics(0.18, MIN_SAMPLES + 2),  # best Brier
    }}
    out = build_matrix(scores, incumbent={})
    assert out["seattle"]["provider"] == "ecmwf_aifs025"
    assert out["seattle"]["eligible"] is True
    assert out["seattle"]["pooled"] is False


def test_thin_city_falls_back_to_pooled_and_blocked(monkeypatch):
    import precip_provider_backtest as ppb
    monkeypatch.setattr(ppb, "PRECIP_LOCATIONS", {
        "seattle": {"data_confidence": "high", "units": "inches"},
        "nyc": {"data_confidence": "high", "units": "inches"},
    })
    # All cells thin (< MIN_SAMPLES) → no independent pick → pooled fallback.
    scores = {
        "seattle": {"ecmwf_aifs025": _metrics(0.20, 2), "gfs025": _metrics(0.40, 2)},
        "nyc": {"ecmwf_aifs025": _metrics(0.22, 2), "gfs025": _metrics(0.45, 2)},
    }
    out = build_matrix(scores, incumbent={})
    # Pooled winner is the lowest-mean-Brier provider across cities (aifs).
    assert out["seattle"]["provider"] == "ecmwf_aifs025"
    assert out["seattle"]["pooled"] is True
    assert out["seattle"]["eligible"] is False  # pooled/thin → observation-only


def test_low_confidence_city_never_eligible(monkeypatch):
    import precip_provider_backtest as ppb
    monkeypatch.setattr(ppb, "PRECIP_LOCATIONS", {
        "hong_kong": {"data_confidence": "low", "units": "mm"},
    })
    # Even a mature, low-Brier ERA5 city must not be eligible.
    scores = {"hong_kong": {"ecmwf_ifs025": _metrics(0.10, MIN_SAMPLES + 5)}}
    out = build_matrix(scores, incumbent={})
    assert out["hong_kong"]["eligible"] is False
    assert out["hong_kong"].get("data_confidence") == "low"


def test_hysteresis_keeps_incumbent_on_marginal_gain(monkeypatch):
    import precip_provider_backtest as ppb
    monkeypatch.setattr(ppb, "PRECIP_LOCATIONS", {
        "seattle": {"data_confidence": "high", "units": "inches"},
    })
    # Challenger only ~5% better than incumbent (< 10% threshold) → keep incumbent.
    scores = {"seattle": {
        "gfs025": _metrics(0.200, MIN_SAMPLES + 2),         # incumbent
        "ecmwf_aifs025": _metrics(0.190, MIN_SAMPLES + 2),  # marginal challenger
    }}
    incumbent = {"seattle": {"provider": "gfs025", "brier": 0.200}}
    out = build_matrix(scores, incumbent)
    assert out["seattle"]["provider"] == "gfs025"  # not switched


def test_hysteresis_switches_on_large_gain(monkeypatch):
    import precip_provider_backtest as ppb
    monkeypatch.setattr(ppb, "PRECIP_LOCATIONS", {
        "seattle": {"data_confidence": "high", "units": "inches"},
    })
    scores = {"seattle": {
        "gfs025": _metrics(0.200, MIN_SAMPLES + 2),
        "ecmwf_aifs025": _metrics(0.150, MIN_SAMPLES + 2),  # 25% better → switch
    }}
    incumbent = {"seattle": {"provider": "gfs025", "brier": 0.200}}
    out = build_matrix(scores, incumbent)
    assert out["seattle"]["provider"] == "ecmwf_aifs025"


# ── Loader fallback chain ─────────────────────────────────────────────────────

def test_loader_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(precip_matrix, "MATRIX_PATH", str(tmp_path / "nope.json"))
    precip_matrix.reload_matrix()
    assert precip_matrix.get_precip_provider("seattle") is None
    assert precip_matrix.get_precip_brier("seattle") is None
    assert precip_matrix.is_pooled("seattle") is False
    assert precip_matrix.sample_count("seattle") == 0


def test_loader_returns_eligible_cell_only(tmp_path, monkeypatch):
    payload = {"cities": {
        "seattle": {"provider": "ecmwf_aifs025", "brier": 0.18, "mae_mm": 5.0,
                    "bias_mm": -1.0, "bucket_hit": 0.7, "samples": 9,
                    "pooled": False, "eligible": True},
        "nyc": {"provider": "gfs025", "brier": 0.30, "samples": 3,
                "pooled": True, "eligible": False},  # observation-only
    }}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(payload))
    monkeypatch.setattr(precip_matrix, "MATRIX_PATH", str(p))
    precip_matrix.reload_matrix()

    # Eligible cell → provider/metrics returned.
    assert precip_matrix.get_precip_provider("seattle") == "ecmwf_aifs025"
    assert precip_matrix.get_precip_brier("seattle") == 0.18
    # Ineligible (pooled) cell → accessors return None, but raw state is exposed.
    assert precip_matrix.get_precip_provider("nyc") is None
    assert precip_matrix.get_precip_brier("nyc") is None
    assert precip_matrix.is_pooled("nyc") is True
    assert precip_matrix.sample_count("nyc") == 3


def teardown_module(module):
    # Restore loader cache to the real on-disk matrix for any later imports.
    precip_matrix.reload_matrix()
