import datetime
import sys, os
sys.path.insert(0, ".")

# Minimal stubs so we can import without DB/network
import types

stub = types.ModuleType("database_schema")
stub.PrecipStrategyTrade = object
stub.PrecipForecast = object
stub.PrecipOutcome = object
stub.PrecipStrategySignal = object
stub.init_database = lambda: None
sys.modules["database_schema"] = stub

stub2 = types.ModuleType("polymarket_precip_dry_run")
stub2.MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

def _parse(q):
    import re
    if "or below" in q:
        m = re.search(r"([\d.]+) inch", q)
        return (None, float(m.group(1))) if m else (None, None)
    if "or above" in q:
        m = re.search(r"([\d.]+) inch", q)
        return (float(m.group(1)), None) if m else (None, None)
    m = re.search(r"([\d.]+) and ([\d.]+) inch", q)
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)

stub2.parse_precip_range = _parse
sys.modules["polymarket_precip_dry_run"] = stub2

stub3 = types.ModuleType("precip_forecast_pipeline")
stub3.PRECIP_LOCATIONS = {
    "seattle": {"lat": 47.6, "lon": -122.3, "city_slug": "seattle",
                "data_source": "acis", "timezone": "America/Los_Angeles",
                "acis_station": "SEA", "native_units": "inches"},
    "nyc": {"lat": 40.7, "lon": -74.0, "city_slug": "new-york",
            "data_source": "acis", "timezone": "America/New_York",
            "acis_station": "NYC", "native_units": "inches"},
}
stub3.fetch_monthly_precip_forecast = lambda *a, **kw: None
sys.modules["precip_forecast_pipeline"] = stub3

from precip_forward_test_yes import (
    TRADE_DAY_WINDOW,
    _select_candidates_from_markets,
    Candidate,
)


def _mock_market(question, yes_ask=0.15):
    return {"question": question, "outcomePrices": f"[{yes_ask},0.85]",
            "clobTokenIds": '["tok_yes","tok_no"]', "id": "mid1"}


def test_trade_day_window_is_18_to_20():
    assert TRADE_DAY_WINDOW == (18, 20), f"Got {TRADE_DAY_WINDOW}"


def test_date_gate_blocks_day_17():
    d0, d1 = TRADE_DAY_WINDOW
    assert not (d0 <= 17 <= d1)


def test_date_gate_allows_day_19():
    d0, d1 = TRADE_DAY_WINDOW
    assert d0 <= 19 <= d1


def test_primary_bucket_only_when_no_band_crossing():
    markets = [
        _mock_market("Will precipitation be 1.0 inch or below?"),
        _mock_market("Will precipitation be between 1.0 and 1.5 inches?"),
        _mock_market("Will precipitation be between 1.5 and 2.0 inches?"),
        _mock_market("Will precipitation be 2.0 inches or above?"),
    ]
    fc = {
        "total_forecast": 1.25,
        "p05": 1.1,   # both within 1.0-1.5 bucket
        "p95": 1.4,
        "settlement_month": datetime.date(2026, 5, 1),
        "units": "inches",
    }
    cands = _select_candidates_from_markets("seattle", fc, markets)
    assert len(cands) == 1
    assert "1.0 and 1.5" in cands[0].question


def test_adjacent_lower_bucket_when_p05_crosses():
    markets = [
        _mock_market("Will precipitation be 1.0 inch or below?"),
        _mock_market("Will precipitation be between 1.0 and 1.5 inches?"),
        _mock_market("Will precipitation be between 1.5 and 2.0 inches?"),
    ]
    fc = {
        "total_forecast": 1.25,
        "p05": 0.8,   # p05 crosses into <=1.0 bucket
        "p95": 1.4,
        "settlement_month": datetime.date(2026, 5, 1),
        "units": "inches",
    }
    cands = _select_candidates_from_markets("seattle", fc, markets)
    assert len(cands) == 2
    questions = [c.question for c in cands]
    assert any("1.0 and 1.5" in q for q in questions), "Primary bucket missing"
    assert any("or below" in q for q in questions), "Lower adjacent missing"


def test_adjacent_upper_bucket_when_p95_crosses():
    markets = [
        _mock_market("Will precipitation be between 1.0 and 1.5 inches?"),
        _mock_market("Will precipitation be between 1.5 and 2.0 inches?"),
        _mock_market("Will precipitation be 2.0 inches or above?"),
    ]
    fc = {
        "total_forecast": 1.25,
        "p05": 1.1,
        "p95": 1.7,   # p95 crosses into 1.5-2.0 bucket
        "settlement_month": datetime.date(2026, 5, 1),
        "units": "inches",
    }
    cands = _select_candidates_from_markets("seattle", fc, markets)
    assert len(cands) == 2
    questions = [c.question for c in cands]
    assert any("1.0 and 1.5" in q for q in questions), "Primary bucket missing"
    assert any("1.5 and 2.0" in q for q in questions), "Upper adjacent missing"


def test_max_two_candidates_even_if_both_sides_cross():
    markets = [
        _mock_market("Will precipitation be 1.0 inch or below?"),
        _mock_market("Will precipitation be between 1.0 and 1.5 inches?"),
        _mock_market("Will precipitation be between 1.5 and 2.0 inches?"),
    ]
    fc = {
        "total_forecast": 1.25,
        "p05": 0.8,   # crosses lower (overlap = 1.0 - 0.8 = 0.2)
        "p95": 1.7,   # crosses upper (overlap = 1.7 - 1.5 = 0.2)
        "settlement_month": datetime.date(2026, 5, 1),
        "units": "inches",
    }
    cands = _select_candidates_from_markets("seattle", fc, markets)
    assert len(cands) == 2  # never more than 2
    questions = [c.question for c in cands]
    assert any("1.0 and 1.5" in q for q in questions), "Primary bucket missing"
    # Equal overlap (0.2 each) → tiebreak picks lower_adj (>= condition)
    assert any("or below" in q for q in questions), "Lower adjacent expected on equal overlap"
