import math
from sweetspot_buckets import forecast_bucket, same_bucket, wilson_ci


def test_forecast_bucket_us_even_aligned():
    assert forecast_bucket(73.4, "F") == (72.0, 74.0, 2.0)
    assert forecast_bucket(74.0, "F") == (74.0, 76.0, 2.0)  # 74 starts the next bucket
    assert forecast_bucket(71.9, "F") == (70.0, 72.0, 2.0)


def test_forecast_bucket_c_single_degree():
    assert forecast_bucket(26.3, "C") == (26.0, 27.0, 1.0)
    assert forecast_bucket(27.0, "C") == (27.0, 28.0, 1.0)


def test_same_bucket():
    assert same_bucket((72.0, 74.0), (72.0, 74.0)) is True
    assert same_bucket((72.0, 74.0), (74.0, 76.0)) is False
    assert same_bucket((None, 50.0), (None, 50.0)) is True


def test_wilson_ci_bounds_and_empty():
    lo, hi = wilson_ci(8, 10)
    assert 0.0 < lo < 0.8 < hi < 1.0
    assert wilson_ci(0, 0) == (0.0, 0.0)
