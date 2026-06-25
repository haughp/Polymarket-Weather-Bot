"""Unit tests for the pure logic in bucket_wr_window_sweep (no I/O)."""
import datetime

import bucket_wr_window_sweep as m


# ── bucketing (floor-aligned half-open, matching real markets) ───────────────

def test_width_for_units():
    assert m.width_for("C") == 1.0
    assert m.width_for("celsius") == 1.0
    assert m.width_for("F") == 2.0
    assert m.width_for("fahrenheit") == 2.0


def test_bucket_lo_celsius_integer_aligned():
    # °C buckets are 1° wide, integer-aligned: 30.0 and 30.9 both -> [30,31)
    assert m.bucket_lo(30.0, 1.0) == 30.0
    assert m.bucket_lo(30.9, 1.0) == 30.0
    assert m.bucket_lo(31.0, 1.0) == 31.0


def test_bucket_lo_fahrenheit_even_aligned():
    # °F buckets are 2° wide, even-aligned: 66 and 67 -> [66,68); 68 -> [68,70)
    assert m.bucket_lo(66.0, 2.0) == 66.0
    assert m.bucket_lo(67.9, 2.0) == 66.0
    assert m.bucket_lo(68.0, 2.0) == 68.0
    assert m.bucket_lo(65.0, 2.0) == 64.0


def test_in_bucket_half_open():
    assert m.in_bucket(30.0, 30.0, 1.0) is True      # lower edge inclusive
    assert m.in_bucket(30.99, 30.0, 1.0) is True
    assert m.in_bucket(31.0, 30.0, 1.0) is False     # upper edge exclusive
    assert m.in_bucket(29.99, 30.0, 1.0) is False


# ── in-bucket-position neighbour (commit 92aeb22 semantics) ──────────────────

def test_neighbour_high_half_picks_upper():
    # corrected 30.7 in [30,31): pos 0.7 >= 0.5 -> upper neighbour [31,32)
    assert m.neighbour_lo(30.7, 30.0, 1.0) == 31.0


def test_neighbour_low_half_picks_lower():
    # corrected 30.2 in [30,31): pos 0.2 < 0.5 -> lower neighbour [29,30)
    assert m.neighbour_lo(30.2, 30.0, 1.0) == 29.0


def test_neighbour_exact_midpoint_is_upper():
    # pos == 0.5 counts as HIGH half (>= midpoint) per production rule
    assert m.neighbour_lo(30.5, 30.0, 1.0) == 31.0


def test_neighbour_fahrenheit_high_half():
    # corrected 67.5 in [66,68): pos (67.5-66)/2 = 0.75 -> upper [68,70)
    assert m.neighbour_lo(67.5, 66.0, 2.0) == 68.0
    # corrected 66.5: pos 0.25 -> lower [64,66)
    assert m.neighbour_lo(66.5, 66.0, 2.0) == 64.0


# ── walk-forward trailing bias (point-in-time, no lookahead) ─────────────────

def _err_series(start: datetime.date, errs: list[float]) -> dict:
    return {start + datetime.timedelta(days=i): e for i, e in enumerate(errs)}


def test_trailing_bias_window_and_mean():
    start = datetime.date(2026, 6, 1)
    errs = _err_series(start, [1.0, 2.0, 3.0, 4.0, 5.0])  # Jun 1..5
    target = datetime.date(2026, 6, 6)
    # 3d window -> Jun 3,4,5 errors = (3+4+5)/3 = 4.0
    assert m.trailing_bias(errs, target, 3, min_samples=1) == 4.0
    # 5d window -> Jun 1..5 = 3.0
    assert m.trailing_bias(errs, target, 5, min_samples=1) == 3.0


def test_trailing_bias_excludes_target_day_no_lookahead():
    start = datetime.date(2026, 6, 1)
    errs = _err_series(start, [1.0, 1.0, 1.0, 99.0])  # Jun 4 = 99 (the target)
    target = datetime.date(2026, 6, 4)
    # window must NOT include Jun 4 itself -> mean of Jun 1,2,3 = 1.0
    assert m.trailing_bias(errs, target, 7, min_samples=1) == 1.0


def test_trailing_bias_respects_min_samples():
    start = datetime.date(2026, 6, 1)
    errs = _err_series(start, [1.0, 2.0])  # only 2 prior days
    target = datetime.date(2026, 6, 3)
    assert m.trailing_bias(errs, target, 7, min_samples=5) is None
    assert m.trailing_bias(errs, target, 7, min_samples=2) == 1.5


# ── evaluate_day: apply bias, bucket, settle both legs ───────────────────────

def test_evaluate_day_f_win_neighbour_loss():
    # raw 29.6, bias +1.0 -> corrected 30.6 in [30,31) high half -> neighbour [31,32)
    # actual 30.4 -> F [30,31) WIN, neighbour [31,32) LOSS
    win_f, win_n, derr = m.evaluate_day(29.6, 30.4, 1.0, 1.0)
    assert win_f is True
    assert win_n is False
    assert round(derr, 2) == 0.20  # |30.6 - 30.4|


def test_evaluate_day_neighbour_win():
    # corrected 30.6 -> neighbour [31,32); actual 31.5 -> F loss, neighbour win
    win_f, win_n, _ = m.evaluate_day(29.6, 31.5, 1.0, 1.0)
    assert win_f is False
    assert win_n is True


# ── sweep aggregation: 1-bucket vs 2-bucket leg counting ─────────────────────

def test_sweep_leg_counting_matches_dashboard():
    # Build a series where, post-warmup, F always wins and neighbour always loses.
    # 1bkt_WR should be 100%; 2bkt_WR should be 50% (1 of 2 legs per day).
    start = datetime.date(2026, 6, 1)
    series = []
    for i in range(40):
        d = start + datetime.timedelta(days=i)
        # forecast == actual == 30.4 every day: bias ~0, corrected 30.4 -> F [30,31) win,
        # pos 0.4 -> lower neighbour [29,30) -> loss.
        series.append((d, 30.4, 30.4))
    swept = m.sweep_city_mode(series, unit="C", min_samples=5, windows=(7,))
    fin = m.finalize(swept[7])
    assert fin["one_bucket_wr"] == 1.0
    assert fin["two_bucket_wr"] == 0.5
    assert fin["n"] > 0


def test_short_window_populates_despite_high_min_samples():
    # Regression: a 3d window must not require 5 samples (impossible in 3 days).
    # Effective floor scales to min(min_samples, window), so 3d populates.
    start = datetime.date(2026, 6, 1)
    series = [(start + datetime.timedelta(days=i), 30.4, 30.4) for i in range(20)]
    swept = m.sweep_city_mode(series, unit="C", min_samples=5, windows=(3, 30))
    assert m.finalize(swept[3])["n"] > 0       # 3d no longer empty
    assert m.finalize(swept[30])["n"] > 0
