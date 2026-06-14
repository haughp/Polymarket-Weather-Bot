"""
TDD spec for the temperature-bucket midpoint / band fix.

Polymarket labels a temperature bucket by its LOWER bound with a fixed width:
  - US (°F): 2°-wide. "between 62-63°F" => YES on a reading of 62 or 63
    => true interval [62, 64) => midpoint 63.
  - Non-US (°C): 1°-wide, single value. "be 13°C" => YES on [13, 14) => midpoint 13.5.
  - Tails ("X°F or below" / "X°F or higher") use the bound directly, inclusive.

parse_temp_range now returns a (lo, hi, width) triple where hi is the EXCLUSIVE
upper bound for finite buckets, and containment is half-open [lo, hi).
"""

import pytest

from polymarket_dry_run import parse_temp_range, classify_markets, bucket_contains


# ── Group 1: parse_temp_range truth table ───────────────────────────────────

@pytest.mark.parametrize("question, expected", [
    ("Will the highest temperature in Atlanta be between 62-63°F on May 11?", (62.0, 64.0, 2.0)),
    ("Will the highest temperature in Dallas be 62-63°F on May 11?",          (62.0, 64.0, 2.0)),  # bare form
    ("Will the highest temperature in London be 13°C on May 11?",            (13.0, 14.0, 1.0)),
    ("Will the highest temperature in Tokyo be 26°C on May 11?",             (26.0, 27.0, 1.0)),
    ("Will the highest temperature in NYC be 75°F or below on May 11?",      (None, 75.0, None)),
    ("Will the highest temperature in NYC be 80°F or higher on May 11?",     (80.0, None, None)),
    ("This has no temperature in it",                                        (None, None, None)),
])
def test_parse_temp_range(question, expected):
    assert parse_temp_range(question) == expected


# ── Group 2: midpoint via classify_markets ──────────────────────────────────

def _mkt(question):
    return {"question": question, "outcomePrices": "[0.5,0.5]"}


@pytest.mark.parametrize("question, expected_mid", [
    ("Will the highest temperature in Atlanta be between 62-63°F on May 11?", 63.0),
    ("Will the highest temperature in London be 13°C on May 11?",            13.5),
    ("Will the highest temperature in Tokyo be 26°C on May 11?",             26.5),
    ("Will the highest temperature in NYC be 75°F or below on May 11?",      75.0),
    ("Will the highest temperature in NYC be 80°F or higher on May 11?",     80.0),
])
def test_classify_midpoint(question, expected_mid):
    # forecast value is irrelevant to the midpoint itself; pick 0.0
    result = classify_markets([_mkt(question)], 0.0)
    assert result["candidates"], "market should parse to a candidate"
    assert result["candidates"][0]["midpoint"] == expected_mid


# ── Group 3: bucket_contains half-open semantics ────────────────────────────

@pytest.mark.parametrize("question, actual, expected", [
    # US 2°F bucket [62, 64)
    ("between 62-63°F", 62.0, True),
    ("between 62-63°F", 63.9, True),
    ("between 62-63°F", 64.0, False),   # inclusive-bug guard: 64 belongs to next bucket
    ("between 62-63°F", 61.9, False),
    # Celsius 1°C bucket [13, 14)
    ("be 13°C", 13.0, True),
    ("be 13°C", 13.9, True),
    ("be 13°C", 14.0, False),
    ("be 13°C", 12.9, False),           # kills old round(actual)==13
    ("be 13°C", 12.5, False),           # old round(12.5) path is gone
    # tails stay inclusive on the bound
    ("75°F or below", 75.0, True),
    ("75°F or below", 75.1, False),
    ("80°F or higher", 80.0, True),
    ("80°F or higher", 79.9, False),
])
def test_bucket_contains(question, actual, expected):
    lo, hi, width = parse_temp_range(question)
    assert bucket_contains(lo, hi, width, actual) is expected
