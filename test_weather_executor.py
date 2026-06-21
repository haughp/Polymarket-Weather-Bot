import pytest
import os, sys
sys.path.insert(0, ".")
from execution.weather_executor import WeatherExecutor, OrderResult


def test_dry_run_buy_yes_succeeds():
    ex = WeatherExecutor(trade_size_usdc=1.0, dry_run=True)
    result = ex.buy_yes(token_id="abc123def456", price=0.10)
    assert result.success is True
    assert result.dry_run is True
    assert result.shares == pytest.approx(10.0, abs=0.01)
    assert result.price_filled == pytest.approx(0.10)
    assert result.fee_paid == 0.0
    assert result.error is None


def test_dry_run_buy_yes_size_override():
    ex = WeatherExecutor(trade_size_usdc=1.0, dry_run=True)
    result = ex.buy_yes(token_id="tok1", price=0.25, size_usdc=2.0)
    assert result.shares == pytest.approx(8.0, abs=0.01)
    assert result.size_usdc == pytest.approx(2.0)


def test_dry_run_buy_yes_zero_price_returns_zero_shares():
    ex = WeatherExecutor(dry_run=True)
    result = ex.buy_yes(token_id="tok1", price=0.0)
    assert result.shares == 0.0
    assert result.success is True


def test_live_mode_raises_without_credentials(monkeypatch):
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="POLY_PRIVATE_KEY"):
        WeatherExecutor(dry_run=False)


def test_classify_error_terminal():
    ex = WeatherExecutor(dry_run=True)
    assert ex._classify_error("unauthorized access") == "terminal"
    assert ex._classify_error("insufficient balance in wallet") == "terminal"
    assert ex._classify_error("fok_order_not_filled") == "terminal"


def test_classify_error_retry():
    ex = WeatherExecutor(dry_run=True)
    assert ex._classify_error("connection refused") == "retry"
    assert ex._classify_error("request timeout") == "retry"
    assert ex._classify_error("rate limit exceeded") == "retry"


def test_classify_error_unknown():
    ex = WeatherExecutor(dry_run=True)
    assert ex._classify_error("something unexpected happened") == "unknown"


class _FakeClobClient:
    def __init__(self, post_order_response, create_order_raises=None):
        self._post_order_response = post_order_response
        self._create_order_raises = create_order_raises
        self.posted_order_types = []

    def update_balance_allowance(self, params):
        pass

    def create_order(self, args):
        if self._create_order_raises:
            raise self._create_order_raises
        return {"_signed": True, "token_id": args.token_id, "price": args.price, "size": args.size}

    def post_order(self, signed_order, order_type):
        self.posted_order_types.append(order_type)
        return self._post_order_response


def _live_executor_with_fake_client(monkeypatch, fake_client):
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0xdeadbeef")
    ex = WeatherExecutor(trade_size_usdc=1.0, dry_run=False)
    ex._client = fake_client
    return ex


def test_dry_run_buy_yes_fok_simulates_fill():
    ex = WeatherExecutor(trade_size_usdc=1.0, dry_run=True)
    result = ex.buy_yes_fok(token_id="abc123def456", price=0.10)
    assert result.success is True
    assert result.dry_run is True
    assert result.shares == pytest.approx(10.0, abs=0.01)


def test_live_buy_yes_fok_filled(monkeypatch):
    from py_clob_client_v2.clob_types import OrderType
    fake_client = _FakeClobClient(post_order_response={
        "orderID": "0xorder1", "status": "MATCHED", "price": "0.10",
    })
    ex = _live_executor_with_fake_client(monkeypatch, fake_client)
    result = ex.buy_yes_fok(token_id="tok1", price=0.10)
    assert result.success is True
    assert result.dry_run is False
    assert result.order_id == "0xorder1"
    assert fake_client.posted_order_types == [OrderType.FOK]


def test_live_buy_yes_fok_not_filled_is_terminal_no_retry(monkeypatch):
    fake_client = _FakeClobClient(post_order_response={
        "orderID": "0xorder2", "status": "CANCELLED", "price": "0.10",
    })
    ex = _live_executor_with_fake_client(monkeypatch, fake_client)
    result = ex.buy_yes_fok(token_id="tok1", price=0.10)
    assert result.success is False
    assert result.error is not None
    # FOK either fills or cancels — no retry-on-fill, so post_order is called exactly once
    assert len(fake_client.posted_order_types) == 1
