import math, sys
sys.path.insert(0, ".")
from precip_strategy_v1 import bucket_probability_yes


def test_sigma_not_inflated_by_days_remaining():
    """Same p05/p95 should produce same bucket probability regardless of days_remaining."""
    p_early = bucket_probability_yes(2.5, 3.0, p05=2.0, p95=4.0, days_remaining=25)
    p_late  = bucket_probability_yes(2.5, 3.0, p05=2.0, p95=4.0, days_remaining=2)
    # Before fix: p_early << p_late (sigma inflated 3.5x for early)
    # After fix:  p_early == p_late (same band → same probability)
    assert abs(p_early - p_late) < 0.001, (
        f"Probabilities differ by days_remaining: early={p_early:.4f} late={p_late:.4f}"
    )


def test_probability_uses_stored_band():
    """P(2.5 < X < 3.0) with mu=3.0, p05=2.0, p95=4.0 should be non-trivial."""
    p = bucket_probability_yes(2.5, 3.0, p05=2.0, p95=4.0, days_remaining=0)
    assert 0.20 < p < 0.55, f"Unexpected probability: {p:.4f}"
