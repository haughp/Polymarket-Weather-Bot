"""Unit tests for the (temperature) provider matrix builder: per-unit bias cap,
smallest-MAE selection, the nyc↔new_york alias, and °C scoring. Pure-function —
no DB or network.

    python3 -m pytest test_provider_matrix.py -v
"""

from provider_backtest import (
    build_matrix,
    score,
    max_abs_bias_for,
    max_mae_for,
    MAX_ABS_BIAS_F,
    MAX_ABS_BIAS_C,
    MAX_MAE_F,
    MAX_MAE_C,
    MATRIX_ALIASES,
)


def _m(mae_debiased, bias, n=7):
    """Minimal metric dict — build_matrix only reads mae_debiased, bias, n."""
    return {"mae_debiased": mae_debiased, "bias": bias, "n": n,
            "mae": mae_debiased}


# ── per-unit cap constant ──────────────────────────────────────────────────────
def test_cap_is_unit_aware():
    assert max_abs_bias_for("F") == MAX_ABS_BIAS_F == 4.0
    assert max_abs_bias_for("C") == MAX_ABS_BIAS_C == 2.0
    assert max_abs_bias_for("anything-else") == MAX_ABS_BIAS_F  # default F


def test_mae_cap_is_unit_aware():
    assert max_mae_for("F") == MAX_MAE_F == 1.5
    assert max_mae_for("C") == MAX_MAE_C == 1.0
    assert max_mae_for("anything-else") == MAX_MAE_F  # default F


# ── MAE gate: drop the over-cap provider, take the next-best ELIGIBLE one ───────
def test_us_mae_cap_substitutes_next_best_provider():
    # Best MAE (1.41) exceeds NO gate? It's under 1.5 — so to test substitution the
    # smallest-MAE provider must be OVER the cap. Here the lowest-MAE provider is over
    # 1.5°F and is dropped for the next-best provider that clears it.
    results = {"dallas": {"max": {
        "ecmwf_aifs025_single": _m(1.69, +1.75),  # smallest MAE but > 1.5°F → skipped
        "gfs_seamless":         _m(1.45, +0.30),   # next-best, under 1.5°F cap → wins
        "icon_seamless":        _m(2.10, -0.20),
    }}}
    out = build_matrix(results, incumbent={}, city_units={"dallas": "F"})
    assert out["dallas"]["max"]["provider"] == "gfs_seamless"


def test_us_no_provider_under_mae_cap_writes_no_cell():
    # Every provider exceeds 1.5°F debiased MAE → no eligible provider → no cell.
    results = {"nyc": {"max": {
        "gfs_seamless":         _m(1.67, -0.49),
        "ecmwf_ifs025":         _m(1.80, +0.20),
        "ecmwf_aifs025_single": _m(1.55, +0.10),
    }}}
    out = build_matrix(results, incumbent={}, city_units={"nyc": "F"})
    assert out.get("nyc", {}).get("max") is None


def test_nonus_mae_cap_is_tighter():
    # A °C provider at 1.05 mae_debiased is fine under 1.5°F but FAILS the 1.0°C cap.
    results = {"london": {"max": {
        "ecmwf_ifs025": _m(1.05, +0.50),  # > 1.0°C → skipped
        "gfs_seamless": _m(0.95, +0.40),  # under 1.0°C → wins
    }}}
    out = build_matrix(results, incumbent={}, city_units={"london": "C"})
    assert out["london"]["max"]["provider"] == "gfs_seamless"


# ── US (°F) selection: smallest MAE wins among providers under the 4°F cap ─────
def test_us_smallest_mae_under_cap_wins():
    results = {"miami": {"max": {
        "ecmwf_aifs025_single": _m(0.85, +4.97),  # best MAE but bias > 4°F → skipped
        "gfs_seamless":         _m(0.96, +1.49),  # next-best, under cap → wins
        "icon_seamless":        _m(1.20, -0.30),
    }}}
    out = build_matrix(results, incumbent={}, city_units={"miami": "F"})
    cell = out["miami"]["max"]
    assert cell["provider"] == "gfs_seamless"
    assert cell["unit"] == "F"
    assert cell["max_abs_bias"] == 4.0


def test_us_uncapped_provider_wins_when_clean():
    results = {"chicago": {"max": {
        "ecmwf_aifs025_single": _m(1.41, +1.36),  # smallest MAE, under cap
        "icon_seamless":        _m(1.67, -0.31),
    }}}
    out = build_matrix(results, incumbent={}, city_units={"chicago": "F"})
    assert out["chicago"]["max"]["provider"] == "ecmwf_aifs025_single"


# ── Non-US (°C) selection: 2°C cap is the tighter gate ─────────────────────────
def test_nonus_2c_cap_skips_provider_over_2c():
    results = {"london": {"max": {
        "ecmwf_ifs025":  _m(0.50, +2.5),  # best MAE but bias > 2°C → skipped
        "gfs_seamless":  _m(0.80, +1.5),  # under 2°C cap → wins
    }}}
    out = build_matrix(results, incumbent={}, city_units={"london": "C"})
    cell = out["london"]["max"]
    assert cell["provider"] == "gfs_seamless"
    assert cell["unit"] == "C"
    assert cell["max_abs_bias"] == 2.0


def test_nonus_no_clean_provider_writes_no_cell():
    results = {"madrid": {"max": {
        "ecmwf_ifs025": _m(0.50, +3.0),  # both over the 2°C cap
        "gfs_seamless": _m(0.60, -2.4),
    }}}
    out = build_matrix(results, incumbent={}, city_units={"madrid": "C"})
    assert out.get("madrid", {}).get("max") is None  # no eligible provider


def test_sample_gate_skips_thin_providers():
    results = {"tokyo": {"max": {
        "ecmwf_ifs025": _m(0.40, +0.5, n=4),  # best MAE but < 5 samples → skipped
        "gfs_seamless": _m(0.90, +0.5, n=6),  # enough samples → wins
    }}}
    out = build_matrix(results, incumbent={}, city_units={"tokyo": "C"})
    assert out["tokyo"]["max"]["provider"] == "gfs_seamless"


# ── alias: a computed nyc cell appears under new_york too ──────────────────────
def test_nyc_cell_aliased_to_new_york():
    assert MATRIX_ALIASES.get("nyc") == "new_york"
    # MAE under the 1.5°F cap so a cell is written and the alias can be asserted.
    results = {"nyc": {"max": {"gfs_seamless": _m(1.20, -0.49)}}}
    out = build_matrix(results, incumbent={}, city_units={"nyc": "F"})
    assert "new_york" in out
    assert out["new_york"]["max"]["provider"] == "gfs_seamless"
    # Same content under both keys.
    assert out["new_york"]["max"] == out["nyc"]["max"]


# ── score() works in °C (unit only changes the informational bucket width) ─────
def test_score_celsius_pairs_sane():
    # forecast vs actual in °C; errors of +1 each → MAE 1.0, bias +1.0.
    pairs = [(20.0, 21.0), (18.0, 19.0), (22.0, 23.0)]
    s = score(pairs, unit="C")
    assert round(s["mae"], 2) == 1.0
    assert round(s["bias"], 2) == 1.0
    assert round(s["mae_debiased"], 2) == 0.0  # constant offset removed
