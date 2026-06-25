import datetime
import json
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, ".")
from database_schema import Base, TradeSimulation
from polymarket_dry_run import (
    _load_live_status,
    _is_live,
    _count_open_legs,
    _count_open_live_positions,
    _execute_leg,
    MAX_LEGS_PER_CITY_DATE,
    MAX_OPEN_LIVE_POSITIONS,
)


class _FakeExecutor:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def buy_yes_fok(self, token_id, price, size_usdc=None):
        self.calls.append((token_id, price, size_usdc))
        return self._result


class _FakeOrderResult:
    def __init__(self, success, order_id="ord1", error=None, fee_paid=0.0,
                 price_filled=None, dry_run=False):
        self.success = success
        self.order_id = order_id
        self.error = error
        self.fee_paid = fee_paid
        self.price_filled = price_filled
        self.dry_run = dry_run


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _add_leg(session, location_id, mode, market_date, market_side="F", dry_run=False):
    session.add(TradeSimulation(
        location_id=location_id,
        mode=mode,
        market_date=market_date,
        clob_token_id=f"tok-{location_id}-{mode}-{market_side}",
        question_text="q",
        market_side=market_side,
        order_type="YES",
        price=0.2,
        size=5.0,
        dry_run=dry_run,
    ))
    session.commit()


def test_is_live_missing_file_defaults_shadow(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "polymarket_dry_run._LIVE_STATUS_PATH", str(tmp_path / "missing.json")
    )
    status = _load_live_status()
    assert _is_live("hong_kong", "max", status) is False


def test_is_live_missing_key_defaults_shadow(tmp_path, monkeypatch):
    path = tmp_path / "ecmwf_live_status.json"
    path.write_text(json.dumps({"paris:min": {"status": "live"}}))
    monkeypatch.setattr("polymarket_dry_run._LIVE_STATUS_PATH", str(path))
    status = _load_live_status()
    assert _is_live("hong_kong", "max", status) is False


def test_is_live_explicit_shadow(tmp_path, monkeypatch):
    path = tmp_path / "ecmwf_live_status.json"
    path.write_text(json.dumps({"hong_kong:max": {"status": "shadow"}}))
    monkeypatch.setattr("polymarket_dry_run._LIVE_STATUS_PATH", str(path))
    status = _load_live_status()
    assert _is_live("hong_kong", "max", status) is False


def test_is_live_explicit_live():
    status = {"hong_kong:max": {"status": "live"}}
    assert _is_live("hong_kong", "max", status) is True


def test_is_live_does_not_cross_contaminate_modes():
    status = {"hong_kong:max": {"status": "live"}}
    assert _is_live("hong_kong", "min", status) is False


def test_count_open_legs_zero_when_none_held(session):
    assert _count_open_legs(session, "paris", "min", datetime.date(2026, 6, 20)) == 0


def test_count_open_legs_counts_existing_rows_for_city_mode_and_date(session):
    _add_leg(session, "paris", "min", datetime.date(2026, 6, 20), market_side="F")
    _add_leg(session, "paris", "min", datetime.date(2026, 6, 20), market_side="neighbour")
    assert _count_open_legs(session, "paris", "min", datetime.date(2026, 6, 20)) == 2


def test_count_open_legs_ignores_other_modes_cities_and_dates(session):
    _add_leg(session, "paris", "min", datetime.date(2026, 6, 20))
    _add_leg(session, "paris", "max", datetime.date(2026, 6, 20))  # different mode, should not count
    _add_leg(session, "lucknow", "max", datetime.date(2026, 6, 20))  # different city
    _add_leg(session, "paris", "min", datetime.date(2026, 6, 21))  # different date
    assert _count_open_legs(session, "paris", "min", datetime.date(2026, 6, 20)) == 1


def test_count_open_live_positions_counts_across_all_live_combos(session):
    _add_leg(session, "paris", "min", datetime.date(2026, 6, 20))
    _add_leg(session, "lucknow", "max", datetime.date(2026, 6, 20))
    _add_leg(session, "hong_kong", "max", datetime.date(2026, 6, 20))
    assert _count_open_live_positions(session) == 3


def test_execute_leg_shadow_combo_does_not_call_executor(session):
    executor = _FakeExecutor(_FakeOrderResult(success=True))
    eligible, dry_run, order_id, success, error, fee_paid = _execute_leg(
        session, executor, location_id="hong_kong", mode="max",
        market_date=datetime.date(2026, 6, 20), token_id="tok1", price=0.2,
        size_usdc=5.0, status={},
    )
    assert eligible is False
    assert dry_run is None
    assert executor.calls == []
    assert order_id is None
    assert success is None


def test_execute_leg_live_combo_calls_executor_and_returns_result(session):
    # Live combo + executor in LIVE mode (dry_run=False) → a real order row.
    executor = _FakeExecutor(_FakeOrderResult(success=True, order_id="0xabc",
                                              fee_paid=0.001, dry_run=False))
    status = {"paris:min": {"status": "live"}}
    eligible, dry_run, order_id, success, error, fee_paid = _execute_leg(
        session, executor, location_id="paris", mode="min",
        market_date=datetime.date(2026, 6, 20), token_id="tok1", price=0.2,
        size_usdc=5.0, status=status,
    )
    assert eligible is True
    assert dry_run is False           # executor's own verdict → real money moved
    assert executor.calls == [("tok1", 0.2, 5.0)]
    assert order_id == "0xabc"
    assert success is True
    assert error is None
    assert fee_paid == 0.001


def test_execute_leg_eligible_combo_but_executor_in_dry_mode_is_not_real_order(session):
    # The bug guard: combo is in the live allowlist, BUT ECMWF_LIVE_TRADING is off,
    # so the executor simulates (dry_run=True, dry-* order_id, fee=0). The recorded
    # row MUST be dry_run=True — never stamped live just because the allowlist said so.
    executor = _FakeExecutor(_FakeOrderResult(success=True, order_id="dry-123",
                                              fee_paid=0.0, dry_run=True))
    status = {"paris:min": {"status": "live"}}
    eligible, dry_run, order_id, success, error, fee_paid = _execute_leg(
        session, executor, location_id="paris", mode="min",
        market_date=datetime.date(2026, 6, 20), token_id="tok1", price=0.2,
        size_usdc=5.0, status=status,
    )
    assert eligible is True
    assert dry_run is True             # simulated despite live allowlist → not real money
    assert order_id == "dry-123"
    assert fee_paid == 0.0


def test_execute_leg_live_combo_propagates_failure(session):
    executor = _FakeExecutor(_FakeOrderResult(success=False, order_id="0xdef",
                                              error="fok_order_not_filled", dry_run=False))
    status = {"paris:min": {"status": "live"}}
    eligible, dry_run, order_id, success, error, fee_paid = _execute_leg(
        session, executor, location_id="paris", mode="min",
        market_date=datetime.date(2026, 6, 20), token_id="tok1", price=0.2,
        size_usdc=5.0, status=status,
    )
    assert eligible is True
    assert success is False
    assert error == "fok_order_not_filled"


def test_execute_leg_blocks_live_order_when_already_holding_max_legs(session):
    _add_leg(session, "paris", "min", datetime.date(2026, 6, 20), market_side="F")
    _add_leg(session, "paris", "min", datetime.date(2026, 6, 20), market_side="neighbour")
    assert MAX_LEGS_PER_CITY_DATE == 2
    executor = _FakeExecutor(_FakeOrderResult(success=True))
    status = {"paris:min": {"status": "live"}}
    eligible, dry_run, order_id, success, error, fee_paid = _execute_leg(
        session, executor, location_id="paris", mode="min",
        market_date=datetime.date(2026, 6, 20), token_id="tok3", price=0.2,
        size_usdc=5.0, status=status,
    )
    assert executor.calls == []
    assert success is False
    assert error == "already_holding_max_legs"


def test_execute_leg_blocks_live_order_when_global_cap_reached(session):
    for i in range(MAX_OPEN_LIVE_POSITIONS):
        _add_leg(session, f"city{i}", "max", datetime.date(2026, 6, 20), market_side="F")
    executor = _FakeExecutor(_FakeOrderResult(success=True))
    status = {"paris:min": {"status": "live"}}
    eligible, dry_run, order_id, success, error, fee_paid = _execute_leg(
        session, executor, location_id="paris", mode="min",
        market_date=datetime.date(2026, 6, 20), token_id="tokN", price=0.2,
        size_usdc=5.0, status=status,
    )
    assert executor.calls == []
    assert success is False
    assert error == "max_open_live_positions_reached"
