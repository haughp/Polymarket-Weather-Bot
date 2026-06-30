"""Pure bucket-grid + binomial-CI helpers for the agreement sweet-spot backfill."""
import math


def forecast_bucket(value: float, unit: str) -> tuple[float, float, float]:
    """Floor-aligned Polymarket bucket [lo, hi) containing `value`.
    US °F = 2° even-aligned; non-US °C = 1°. Matches the live grid."""
    width = 1.0 if unit == "C" else 2.0
    lo = width * math.floor(value / width)
    return (lo, lo + width, width)


def same_bucket(a: tuple, b: tuple) -> bool:
    """True iff two (lo, hi) buckets are the same. Finite buckets compare by
    lower bound; tail/other buckets compare the full tuple."""
    if a[0] is not None and b[0] is not None:
        return a[0] == b[0]
    return tuple(a) == tuple(b)


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))
