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

from polymarket_dry_run import (
    parse_temp_range,
    classify_markets,
    bucket_contains,
    select_entry_pair,
)


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


# ── Group 4: select_entry_pair — F + higher-priced neighbour ─────────────────
#
# Ported from weatherbot-ts src/selection.ts. Buy the forecast-centre bucket F
# (the bucket whose half-open interval contains the bias-corrected forecast)
# plus the single adjacent neighbour (F±width) the MARKET prices higher. F is
# exempt from the price floor (dropping the cheap-but-correct forecast bucket
# was the 2026-06-14 Dallas loss); the floor applies to the neighbour only, the
# ceiling to both legs. Selection uses the Gamma outcomePrices[0] (yes_price).

def _pmkt(question, yes_price):
    return {"question": question, "outcomePrices": f"[{yes_price},{round(1-yes_price,4)}]"}


def _candidates(*pairs):
    """pairs: (question, yes_price). Returns classify_markets-style candidates."""
    markets = [_pmkt(q, p) for q, p in pairs]
    # forecast irrelevant for building the candidate list; selection reads range/price
    return classify_markets(markets, 0.0)["candidates"]


GATE = {"min_price": 0.12, "max_price": 0.35}


def test_us_dallas_regression_F_kept_below_floor():
    # adj forecast 85.95 -> F = [84,86) ("between 84-85"); priced $0.05 < floor.
    # The old floor would drop it; select_entry_pair must KEEP F as leg 1.
    cands = _candidates(
        ("between 82-83°F", 0.08),
        ("between 84-85°F", 0.05),   # F — cheap but correct
        ("between 86-87°F", 0.19),   # F+1 (higher priced of the two neighbours)
        ("between 88-89°F", 0.28),
    )
    res = select_entry_pair(cands, 85.95, **GATE)
    assert res["ok"] is True
    assert res["pair"][0]["range"] == (84.0, 86.0)   # F first
    assert res["pair"][1]["range"] == (86.0, 88.0)   # higher-priced neighbour


def test_higher_priced_neighbour_wins():
    cands = _candidates(
        ("between 68-69°F", 0.15),   # below
        ("between 70-71°F", 0.05),   # F
        ("between 72-73°F", 0.20),   # above — pricier
    )
    res = select_entry_pair(cands, 70.5, **GATE)
    assert res["ok"] is True
    assert res["pair"][0]["range"] == (70.0, 72.0)
    assert res["pair"][1]["range"] == (72.0, 74.0)


def test_celsius_neighbour_step_is_one_degree():
    # °C buckets are 1° wide; F=[22,23), neighbours are [21,22) and [23,24).
    cands = _candidates(
        ("be 21°C", 0.15),
        ("be 22°C", 0.18),           # F (forecast 22.4)
        ("be 23°C", 0.25),           # above — pricier
    )
    res = select_entry_pair(cands, 22.4, **GATE)
    assert res["ok"] is True
    assert res["pair"][0]["range"] == (22.0, 23.0)
    assert res["pair"][1]["range"] == (23.0, 24.0)


def test_abort_when_chosen_neighbour_over_ceiling():
    cands = _candidates(
        ("between 68-69°F", 0.20),
        ("between 70-71°F", 0.10),   # F ok
        ("between 72-73°F", 0.42),   # pricier neighbour but > 0.35 ceiling
    )
    res = select_entry_pair(cands, 70.5, **GATE)
    assert res["ok"] is False


def test_abort_when_F_over_ceiling():
    cands = _candidates(
        ("between 68-69°F", 0.20),
        ("between 70-71°F", 0.50),   # F itself > ceiling
        ("between 72-73°F", 0.25),
    )
    res = select_entry_pair(cands, 70.5, **GATE)
    assert res["ok"] is False


def test_abort_when_neighbour_below_floor():
    cands = _candidates(
        ("between 68-69°F", 0.03),
        ("between 70-71°F", 0.10),   # F kept
        ("between 72-73°F", 0.05),   # higher of the two but still < floor
    )
    res = select_entry_pair(cands, 70.5, **GATE)
    assert res["ok"] is False


def test_abort_when_forecast_outside_all_buckets():
    cands = _candidates(("between 70-71°F", 0.20), ("between 72-73°F", 0.20))
    res = select_entry_pair(cands, 200.0, **GATE)
    assert res["ok"] is False


def test_tail_F_uses_single_neighbour():
    # forecast 80 -> F is the "or higher" tail; only a lower neighbour exists.
    cands = _candidates(
        ("between 74-75°F", 0.15),   # F-1 (above floor)
        ("76°F or higher", 0.20),    # F (tail)
    )
    res = select_entry_pair(cands, 80.0, **GATE)
    assert res["ok"] is True
    assert res["pair"][0]["range"] == (76.0, None)
    assert res["pair"][1]["range"] == (74.0, 76.0)


def test_abort_when_F_has_no_neighbour():
    cands = _candidates(("between 70-71°F", 0.20))   # F only, no neighbour
    res = select_entry_pair(cands, 70.5, **GATE)
    assert res["ok"] is False
