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
