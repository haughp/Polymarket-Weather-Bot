# Weather Bot Strategy Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix sigma double-counting bug, add temperature dual-bucket entry logic, update precipitation date gate to 18th-20th, add precipitation adjacent-bucket buying, and build a modular Python CLOB execution module for the weather bot.

**Architecture:** Four independent changes to existing Python/TypeScript files plus one new file (`execution/weather_executor.py`). The executor wraps `py-clob-client-v2` with dry-run/live modes and is wired into the precipitation forward-test script, replacing inline CLOB code. Temperature changes are TypeScript-only in `src/strategy.ts`.

**Tech Stack:** Python 3.13, TypeScript, py-clob-client-v2, SQLAlchemy, ECMWF Open Data, Polymarket CLOB API

**Credential convention:** All Python execution code loads credentials from the shared sniff_test_polymarket `.env` at `/Users/padraighaughey/sniff_test_polymarket/.env`. Env var names follow the `POLY_*` convention used by that project (`POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_SECRET`, `POLY_PASSPHRASE`, `POLY_FUNDER_ADDRESS`, `POLY_CHAIN_ID`). The weather bot's own `.env` (`POLYMARKET_PRIVATE_KEY`, `SIGNATURE_TYPE`) is **not** used for execution.

---

## Security Check (do first, no commit)

- [ ] **Verify .env is gitignored**

```bash
grep -n "\.env" /Users/padraighaughey/Polymarket-Weather-Bot/.gitignore
```
Expected: a line containing `.env`. If missing, add it:
```bash
echo ".env" >> /Users/padraighaughey/Polymarket-Weather-Bot/.gitignore
```

> ⚠️ The `.env` contains a plaintext private key (`POLYMARKET_PRIVATE_KEY`). This key should be rotated if it has ever been committed to git history. Check: `git log --all --oneline -- .env` — if any commits appear, rotate the key immediately on Polymarket.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `execution/weather_executor.py` | **Create** | Modular CLOB executor: dry-run + live BUY YES, error taxonomy, credential loading |
| `test_weather_executor.py` | **Create** | Unit tests for WeatherExecutor dry-run behaviour |
| `precip_strategy_v1.py` | **Modify** line 189 | Remove sigma double-counting |
| `precip_forward_test_yes.py` | **Modify** | Date gate 18-20, adjacent-bucket selection, wire WeatherExecutor |
| `test_precip_forward.py` | **Create** | Tests for date gate and adjacent-bucket logic |
| `src/strategy.ts` | **Modify** lines 316-345, +constants | Dual-bucket selection by confidence band overlap |

---

## Task 1: Fix Sigma Double-Counting Bug

**Files:**
- Modify: `precip_strategy_v1.py:188-189`

**Context:** `bucket_probability_yes()` computes sigma from the stored p05/p95 band and then *widens it further* by `days_remaining`. But p05/p95 from `compute_confidence_band()` already captures days-remaining uncertainty (wider forecast → wider band). The extra widening double-counts uncertainty, inflating tail probabilities early in the month and suppressing edge detection.

- [ ] **Step 1: Write the failing test**

Create `test_precip_sigma_fix.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot && python -m pytest test_precip_sigma_fix.py -v
```
Expected: `FAILED test_precip_sigma_fix.py::test_sigma_not_inflated_by_days_remaining`

- [ ] **Step 3: Remove the sigma inflation line**

In `precip_strategy_v1.py`, locate lines 188-189:
```python
    sigma = max(0.01, (p95 - p05) / (2.0 * 1.645))
    sigma *= (1.0 + max(0, days_remaining) / 10.0)
```

Change to:
```python
    sigma = max(0.01, (p95 - p05) / (2.0 * 1.645))
```

(Delete the `sigma *=` line entirely.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot && python -m pytest test_precip_sigma_fix.py -v
```
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add precip_strategy_v1.py test_precip_sigma_fix.py
git commit -m "fix: remove sigma double-counting in bucket_probability_yes

p05/p95 from compute_confidence_band() already captures days_remaining
uncertainty (wider forecast window → wider band). Widening sigma again
by days_remaining double-counts uncertainty and suppresses early-month
signals."
```

---

## Task 2: Build Python WeatherExecutor

**Files:**
- Create: `execution/__init__.py`
- Create: `execution/weather_executor.py`
- Create: `test_weather_executor.py`

The executor is a thin wrapper around py-clob-client-v2. Dry-run mode simulates fills without any network calls. Live mode places GTC limit BUY orders at the specified price. Error taxonomy distinguishes terminal errors (no retry) from transient ones (one retry).

- [ ] **Step 1: Create the execution package**

```bash
mkdir -p /Users/padraighaughey/Polymarket-Weather-Bot/execution
touch /Users/padraighaughey/Polymarket-Weather-Bot/execution/__init__.py
```

- [ ] **Step 2: Write the failing tests**

Create `test_weather_executor.py`:
```python
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


def test_live_mode_raises_without_credentials():
    # Temporarily remove POLY_PRIVATE_KEY if set
    old = os.environ.pop("POLY_PRIVATE_KEY", None)
    try:
        with pytest.raises(RuntimeError, match="POLY_PRIVATE_KEY"):
            WeatherExecutor(dry_run=False)
    finally:
        if old:
            os.environ["POLY_PRIVATE_KEY"] = old


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
```

- [ ] **Step 3: Run to verify tests fail**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot && python -m pytest test_weather_executor.py -v
```
Expected: `ERROR` — `execution.weather_executor` module not found.

- [ ] **Step 4: Create the executor**

Create `execution/weather_executor.py`:
```python
"""
execution/weather_executor.py — Polymarket CLOB order placement for Weather Bot.

Supports dry-run (default) and live modes.

Credentials are loaded from the shared sniff_test_polymarket .env file:
  /Users/padraighaughey/sniff_test_polymarket/.env

Environment variables (same names as sniff_test_polymarket):
  POLY_PRIVATE_KEY    — Ethereum private key (hex, with or without 0x prefix)
  POLY_API_KEY        — CLOB L2 API key (derived once, cached in .env)
  POLY_SECRET         — CLOB L2 API secret
  POLY_PASSPHRASE     — CLOB L2 API passphrase
  POLY_FUNDER_ADDRESS — Proxy/funder wallet address (optional)
  POLY_CHAIN_ID       — 137 (mainnet, default) | 80002 (Amoy testnet)

In dry_run mode, no order is placed. The executor simulates a fill at the
current market price and returns success=True with dry_run=True.

py-clob-client-v2 is a lazy import — not required in dry_run mode.
Install: pip install py-clob-client-v2
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load shared credentials from sniff_test_polymarket .env (won't override existing env vars)
_SHARED_ENV = Path("/Users/padraighaughey/sniff_test_polymarket/.env")
load_dotenv(_SHARED_ENV, override=False)

log = logging.getLogger("weather.executor")

POLY_HOST = "https://clob.polymarket.com"
CHAIN_ID_MAINNET = 137
CHAIN_ID_TESTNET = 80002
TAKER_FEE_RATE = 0.0002


@dataclass
class OrderResult:
    """Result of a CLOB BUY order attempt."""
    success: bool
    order_id: Optional[str]
    token_id: str
    side: str           # "BUY_YES"
    size_usdc: float
    shares: float
    price_filled: float
    fee_paid: float
    dry_run: bool
    error: Optional[str] = None


class WeatherExecutor:
    """
    Thin wrapper around py-clob-client-v2 for weather market order placement.

    Stateless across trades. Caller responsible for logging via DB.
    Credentials read from environment at init (live) or first order (lazy).
    Credentials are loaded from /Users/padraighaughey/sniff_test_polymarket/.env
    using the same POLY_* variable names as sniff_test_polymarket.

    Usage:
        ex = WeatherExecutor(trade_size_usdc=1.0, dry_run=True)
        result = ex.buy_yes(token_id="...", price=0.12)
        if result.success:
            # record in DB
    """

    _TERMINAL_ERRORS = (
        "unauthorized",
        "forbidden",
        "insufficient balance",
        "not enough balance",
        "allowance",
        "invalid order",
        "bad request",
        "fok_order_not_filled",
        "not filled",
        "invalid token",
    )
    _RETRY_ERRORS = (
        "connection",
        "timeout",
        "internal server error",
        "service unavailable",
        "rate limit",
        "too many requests",
    )

    def __init__(
        self,
        trade_size_usdc: float = 1.0,
        dry_run: bool = True,
        chain_id: Optional[int] = None,
    ) -> None:
        self._size = trade_size_usdc
        self._dry_run = dry_run
        self._chain_id = chain_id or int(
            os.environ.get("POLY_CHAIN_ID", CHAIN_ID_MAINNET)
        )
        self._client = None

        if dry_run:
            log.info(
                "WeatherExecutor ready (DRY RUN, size=%.2f USDC)", trade_size_usdc
            )
        else:
            self._check_credentials()
            log.info(
                "WeatherExecutor ready (LIVE, size=%.2f USDC, chain=%d)",
                trade_size_usdc,
                self._chain_id,
            )

    # ── Credential validation ──────────────────────────────────────

    def _check_credentials(self) -> None:
        pk = os.environ.get("POLY_PRIVATE_KEY", "").strip()
        if not pk:
            raise RuntimeError(
                "POLY_PRIVATE_KEY not set in environment.\n"
                "Expected in /Users/padraighaughey/sniff_test_polymarket/.env"
            )

    def _init_client(self) -> None:
        """Lazy-init py-clob-client-v2 on first live order."""
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds
        except ImportError:
            raise RuntimeError(
                "py-clob-client-v2 not installed.\n"
                "Run: pip install py-clob-client-v2"
            )

        pk = os.environ.get("POLY_PRIVATE_KEY", "").strip()
        funder = os.environ.get("POLY_FUNDER_ADDRESS", "").strip() or None

        self._client = ClobClient(
            POLY_HOST,
            key=pk,
            chain_id=self._chain_id,
            signature_type=0,   # EOA — matches sniff_test_polymarket pattern
            funder=funder,
        )

        # Use cached L2 creds if available (fast path, no derivation needed)
        api_key = os.environ.get("POLY_API_KEY", "").strip()
        secret = os.environ.get("POLY_SECRET", "").strip()
        passphrase = os.environ.get("POLY_PASSPHRASE", "").strip()
        if api_key and secret and passphrase:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=secret,
                api_passphrase=passphrase,
            )
            log.info("ClobClient initialised with cached L2 creds (chain_id=%d)", self._chain_id)
        else:
            try:
                creds = self._client.derive_api_key()
            except Exception:
                creds = self._client.create_api_key()
            log.info("ClobClient ready via derived L2 creds (chain_id=%d)", self._chain_id)

        self._client.set_api_creds(creds)

    # ── Public interface ───────────────────────────────────────────

    def buy_yes(
        self,
        token_id: str,
        price: float,
        size_usdc: Optional[float] = None,
    ) -> OrderResult:
        """
        Place a GTC limit BUY YES order at `price`.

        size_usdc: override session default (TRADE_SIZE_USD) for this order.
        In dry_run mode, simulates instant fill and returns success=True.
        """
        size = size_usdc if size_usdc is not None else self._size
        if self._dry_run:
            return self._simulate_buy(token_id, price, size)
        return self._live_buy(token_id, price, size)

    # ── Internal helpers ───────────────────────────────────────────

    def _simulate_buy(self, token_id: str, price: float, size: float) -> OrderResult:
        shares = round(size / price, 4) if price > 0 else 0.0
        log.info(
            "DRY RUN BUY: token=...%s @ %.4f  $%.2f  shares=%.4f",
            token_id[-8:], price, size, shares,
        )
        return OrderResult(
            success=True,
            order_id=f"dry-{int(time.time())}",
            token_id=token_id,
            side="BUY_YES",
            size_usdc=size,
            shares=shares,
            price_filled=price,
            fee_paid=0.0,
            dry_run=True,
        )

    def _classify_error(self, err_str: str) -> str:
        """Returns 'terminal', 'retry', or 'unknown' for a lower-cased error string."""
        for fragment in self._TERMINAL_ERRORS:
            if fragment in err_str:
                return "terminal"
        for fragment in self._RETRY_ERRORS:
            if fragment in err_str:
                return "retry"
        return "unknown"

    def _live_buy(self, token_id: str, price: float, size: float) -> OrderResult:
        """Live GTC BUY order via py-clob-client-v2 with collateral sync and retry."""
        from py_clob_client_v2.clob_types import (
            OrderArgs,
            OrderType,
            BalanceAllowanceParams,
            AssetType,
        )

        t_sent = time.time()
        MAX_RETRIES = 1
        RETRY_SLEEP = 1.5
        attempt = 0
        last_error = ""

        while attempt <= MAX_RETRIES:
            attempt += 1
            try:
                if self._client is None:
                    self._init_client()

                # Sync USDC.e collateral balance with CLOB ledger (attempt 1 only).
                # Without this, the CLOB may see stale balance and reject the BUY.
                if attempt == 1:
                    try:
                        self._client.update_balance_allowance(
                            BalanceAllowanceParams(
                                asset_type=AssetType.COLLATERAL,
                                signature_type=0,
                            )
                        )
                        log.debug("Collateral sync OK  token=...%s", token_id[-8:])
                    except Exception as sync_exc:
                        log.warning("Collateral sync failed (proceeding): %s", sync_exc)

                shares = round(size / price, 4) if price > 0 else 0.0
                args = OrderArgs(
                    token_id=token_id,
                    price=round(price, 4),
                    size=shares,
                    side="BUY",
                )
                signed_order = self._client.create_order(args)
                resp = self._client.post_order(signed_order, OrderType.GTC)
                t_fill = time.time()

                order_id = resp.get("orderID") or resp.get("id", "unknown")
                status = resp.get("status", "")
                price_filled = float(resp.get("price", price))
                fee_paid = size * TAKER_FEE_RATE
                latency_ms = (t_fill - t_sent) * 1000

                log.info(
                    "BUY ORDER [%s]  token=...%s @ %.4f  shares=%.4f  "
                    "fee=%.4f  latency=%.0fms  status=%s",
                    order_id[:12], token_id[-8:], price_filled,
                    shares, fee_paid, latency_ms, status,
                )

                return OrderResult(
                    success=True,
                    order_id=order_id,
                    token_id=token_id,
                    side="BUY_YES",
                    size_usdc=size,
                    shares=shares,
                    price_filled=price_filled,
                    fee_paid=fee_paid,
                    dry_run=False,
                )

            except Exception as e:
                err_str = str(e).lower()
                error_class = self._classify_error(err_str)

                if type(e).__name__ == "PolyApiException":
                    sc = getattr(e, "status_code", "API_ERR")
                    msg = getattr(e, "error_message", str(e))
                    clean_err = f"PolyApiException[{sc}]: {msg}"
                else:
                    clean_err = str(e)

                if error_class == "terminal":
                    log.error("BUY terminal error (no retry): %s", clean_err)
                    last_error = clean_err
                    break
                elif error_class == "retry" and attempt <= MAX_RETRIES:
                    log.warning(
                        "BUY transient error (retry in %.1fs): %s",
                        RETRY_SLEEP, clean_err,
                    )
                    last_error = clean_err
                    time.sleep(RETRY_SLEEP)
                    continue
                else:
                    log.error("BUY failed  class=%s: %s", error_class, clean_err)
                    last_error = clean_err
                    break

        return OrderResult(
            success=False,
            order_id=None,
            token_id=token_id,
            side="BUY_YES",
            size_usdc=size,
            shares=0.0,
            price_filled=price,
            fee_paid=0.0,
            dry_run=False,
            error=last_error,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot && python -m pytest test_weather_executor.py -v
```
Expected: `7 passed`

- [ ] **Step 6: Commit**

```bash
git add execution/__init__.py execution/weather_executor.py test_weather_executor.py
git commit -m "feat: add WeatherExecutor for dry-run/live CLOB order placement

Modelled on sniff_test_polymarket polymarket_executor.py pattern.
Wraps py-clob-client-v2 with dry-run simulation, collateral sync,
error taxonomy (terminal vs retry), and GTC limit buy."
```

---

## Task 3: Update Precipitation Date Gate and Adjacent-Bucket Selection

**Files:**
- Modify: `precip_forward_test_yes.py` (constants, `_find_bucket_candidates`, `run`)
- Create: `test_precip_forward.py`

**Changes:**
1. `TRUST_WINDOWS` → unified `TRADE_DAY_WINDOW = (18, 20)` for all cities
2. New `_find_bucket_candidates()` returns up to 2 `Candidate` objects (primary + adjacent when p05/p95 crosses bucket boundary)
3. Wire `WeatherExecutor` instead of inline ClobClient

- [ ] **Step 1: Write the failing tests**

Create `test_precip_forward.py`:
```python
import datetime
import sys, os
sys.path.insert(0, ".")

# Minimal stubs so we can import without DB/network
import types
stub = types.ModuleType("database_schema")
stub.PrecipStrategyTrade = object
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
        "p05": 0.8,   # p05 crosses into ≤1.0 bucket
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
        "p05": 0.8,   # crosses lower
        "p95": 1.7,   # crosses upper
        "settlement_month": datetime.date(2026, 5, 1),
        "units": "inches",
    }
    cands = _select_candidates_from_markets("seattle", fc, markets)
    assert len(cands) == 2  # never more than 2
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot && python -m pytest test_precip_forward.py -v
```
Expected: `ImportError` or `FAILED` on `TRADE_DAY_WINDOW` and `_select_candidates_from_markets` not existing yet.

- [ ] **Step 3: Update `precip_forward_test_yes.py`**

Replace the top section of `precip_forward_test_yes.py` (lines 1-44) with the updated version:

```python
#!/usr/bin/env python3
"""
Forward test model: Buy YES on top forecast bucket for monthly precipitation.

Strategy v1:
- City scope: Seattle, NYC
- Timing window: day 18-20 of month (±1 day fallback for pipeline downtime)
- Forecast model: ecmwf_ec46 (from precip_forecast_pipeline metadata)
- Primary trade: $1 BUY YES in bucket containing forecast total.
- Adjacent trade: if p05 or p95 crosses into an adjacent bucket, also buy
  that bucket. Maximum 2 buys per city per month execution.

Default mode is dry-run; pass --execute for live order submission.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from dataclasses import dataclass
from typing import Optional

import httpx

from database_schema import PrecipStrategyTrade, init_database
from execution.weather_executor import WeatherExecutor
from polymarket_precip_dry_run import MONTHS, parse_precip_range
from precip_forecast_pipeline import PRECIP_LOCATIONS, fetch_monthly_precip_forecast

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"

# Day-of-month window for trade entry (18th is primary; 18-20 covers pipeline downtime)
TRADE_DAY_WINDOW = (18, 20)
TARGET_CITIES = ("seattle", "nyc")
TRADE_SIZE_USD = 1.0
MAX_YES_ENTRY_PRICE = 0.50
```

- [ ] **Step 4: Add `_select_candidates_from_markets` and update `_find_bucket_candidates`**

Replace the existing `_find_bucket_candidate` function (starting around line 108) with:

```python
def _select_candidates_from_markets(
    loc_id: str, fc: dict, markets: list[dict]
) -> list["Candidate"]:
    """
    From a list of Polymarket markets for a city/month, return up to 2 Candidates:
      1. Primary: the bucket containing fc['total_forecast']
      2. Adjacent: the bucket that fc['p05'] or fc['p95'] crosses into (if any)

    When p05 crosses the lower boundary and p95 crosses the upper boundary,
    the adjacent bucket with the larger confidence-band overlap is chosen.
    At most 2 candidates are returned.
    """
    total = float(fc["total_forecast"])
    p05   = float(fc.get("p05", total))
    p95   = float(fc.get("p95", total))

    primary: Optional[Candidate] = None
    lower_adj: Optional[Candidate] = None
    upper_adj: Optional[Candidate] = None

    def _make_candidate(m: dict, low, high) -> Optional["Candidate"]:
        ytok = _clob_yes_token(m)
        if not ytok:
            return None
        y_mid, _ = _prices(m)
        ask = _best_yes_ask(ytok)
        if ask is None and y_mid is None:
            return None
        px = ask if ask is not None else float(y_mid)
        return Candidate(
            location_id=loc_id,
            settlement_month=fc["settlement_month"],
            question=m.get("question", ""),
            low=low,
            high=high,
            yes_token=ytok,
            best_ask=float(px),
            yes_mid=float(y_mid if y_mid is not None else px),
            forecast_total=total,
            units=fc.get("units", "inches"),
        )

    for m in markets:
        q = m.get("question", "")
        low, high = parse_precip_range(q)
        if low is None and high is None:
            continue
        lo = 0.0 if low is None else low
        hi = float("inf") if high is None else high

        # Primary: bucket that contains total_forecast
        if lo <= total <= hi and primary is None:
            primary = _make_candidate(m, low, high)
            continue

        # Lower adjacent: bucket contains p05 but NOT total
        # (p05 has drifted into a lower bucket than the primary)
        if p05 < total and lo <= p05 <= hi and not (lo <= total <= hi) and lower_adj is None:
            lower_adj = _make_candidate(m, low, high)
            continue

        # Upper adjacent: bucket contains p95 but NOT total
        # (p95 has drifted into a higher bucket than the primary)
        if p95 > total and lo <= p95 <= hi and not (lo <= total <= hi) and upper_adj is None:
            upper_adj = _make_candidate(m, low, high)
            continue

    if primary is None:
        return []

    candidates = [primary]

    # When both sides cross, pick the adjacent with greater band overlap
    if lower_adj is not None and upper_adj is not None:
        p_lo = primary.low if primary.low is not None else 0.0
        p_hi = primary.high if primary.high is not None else float("inf")
        lower_overlap = max(0.0, p_lo - p05)
        upper_overlap = max(0.0, p95 - p_hi)
        candidates.append(lower_adj if lower_overlap >= upper_overlap else upper_adj)
    elif lower_adj is not None:
        candidates.append(lower_adj)
    elif upper_adj is not None:
        candidates.append(upper_adj)

    return candidates


def _find_bucket_candidates(loc_id: str, fc: dict) -> list[Candidate]:
    """Fetch live markets for city/month and return up to 2 Candidates."""
    obs = PRECIP_LOCATIONS[loc_id]
    month_str = MONTHS[fc["settlement_month"].month - 1]
    markets = _fetch_event_markets(obs["city_slug"], month_str)
    if not markets:
        return []
    return _select_candidates_from_markets(loc_id, fc, markets)
```

- [ ] **Step 5: Update the `run()` function**

Replace the `run()` function body in `precip_forward_test_yes.py`:

```python
def run(execute: bool = False) -> None:
    session = init_database()
    executor = WeatherExecutor(
        trade_size_usdc=TRADE_SIZE_USD,
        dry_run=not execute,
    )

    placed = 0
    blocked = 0
    today = datetime.date.today()

    for loc_id in TARGET_CITIES:
        fc = fetch_monthly_precip_forecast(loc_id, today=today)
        if fc is None:
            print(f"{loc_id}: no forecast")
            blocked += 1
            continue
        if fc.get("model_name") and fc.get("model_name") != "ecmwf_ec46":
            print(f"{loc_id}: model mismatch ({fc.get('model_name')})")
            blocked += 1
            continue

        d0, d1 = TRADE_DAY_WINDOW
        if not (d0 <= today.day <= d1):
            print(f"{loc_id}: day {today.day} outside trade window [{d0},{d1}]")
            blocked += 1
            continue

        candidates = _find_bucket_candidates(loc_id, fc)
        if not candidates:
            print(f"{loc_id}: no matching market bucket found")
            blocked += 1
            continue

        for cand in candidates:
            if _already_traded_today(session, loc_id, cand.settlement_month, cand.question):
                print(f"{loc_id}: duplicate blocked for today — {cand.question[:50]}")
                blocked += 1
                continue

            price = max(0.01, min(0.99, cand.best_ask))
            block_reason: Optional[str] = None

            if price >= MAX_YES_ENTRY_PRICE:
                block_reason = f"yes_price_gate:{price:.4f}>={MAX_YES_ENTRY_PRICE}"

            order_id: Optional[str] = None
            if block_reason is None:
                result = executor.buy_yes(cand.yes_token, price)
                if result.success:
                    order_id = result.order_id
                else:
                    block_reason = f"execution_error:{result.error}"

            session.add(
                PrecipStrategyTrade(
                    mode="forward_yes_v1",
                    location_id=loc_id,
                    settlement_month=cand.settlement_month,
                    question_text=cand.question,
                    clob_token_no=cand.yes_token,
                    side="YES",
                    size_usd=TRADE_SIZE_USD,
                    limit_price=price,
                    fill_price=price if block_reason is None else None,
                    p_no=None,
                    fair_no=None,
                    edge_no=None,
                    days_remaining=fc.get("days_remaining"),
                    data_source=f"{fc.get('source')}|{fc.get('model_name')}",
                    blocked_reason=block_reason,
                )
            )
            session.commit()

            if block_reason is None:
                placed += 1
                print(
                    f"{loc_id}: BUY YES  ${TRADE_SIZE_USD:.2f} @ {price:.4f}  "
                    f"bucket='{cand.question}'  fc={cand.forecast_total:.2f} {cand.units}  "
                    f"order={order_id}"
                )
            else:
                blocked += 1
                print(
                    f"{loc_id}: blocked={block_reason}  bucket='{cand.question}'  px={price:.4f}"
                )

    session.close()
    print(f"\nforward_yes_v1 complete: placed={placed} blocked={blocked} execute={execute}")
```

- [ ] **Step 6: Remove the now-unused `_make_clob_client` function**

Delete lines containing `_make_clob_client` (roughly lines 146-158 in the original file) and remove the top-level imports of `py_clob_client` since the executor handles those:
```python
# Remove these imports from the top of the file:
# from py_clob_client.client import ClobClient
# from py_clob_client.clob_types import OrderArgs, OrderType
# from py_clob_client.order_builder.constants import BUY
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot && python -m pytest test_precip_forward.py -v
```
Expected: `8 passed`

- [ ] **Step 8: Smoke test dry-run mode**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot && python precip_forward_test_yes.py
```
Expected: output like `seattle: day X outside trade window [18,20]` (if not on 18th-20th) or `seattle: no forecast`. No errors, no CLOB calls.

- [ ] **Step 9: Commit**

```bash
git add precip_forward_test_yes.py test_precip_forward.py
git commit -m "feat: update precipitation strategy to 18-20 date window with adjacent-bucket buying

- Replace city-specific trust windows (16-20/16-25) with unified day 18-20
- Add _select_candidates_from_markets() to return primary + 1 adjacent
  bucket when p05 or p95 crosses a bucket boundary
- Wire WeatherExecutor (dry-run/live) replacing inline ClobClient
- Mode label updated to forward_yes_v1"
```

---

## Task 4: Temperature Dual-Bucket Entry in strategy.ts

**Files:**
- Modify: `src/strategy.ts` (add constant, helper, replace single-bucket loop)

**Logic:** The ECMWF ±1°C confidence band equals ±1.8°F. For each Polymarket market bucket, score it by the fraction of the band [forecastTemp−1.8, forecastTemp+1.8] that falls within the bucket. Sort by score descending and buy the top 2 where YES < 0.20 (the `DUAL_BUCKET_MAX_PRICE` constant). Each bucket counts as an independent trade against `MAX_TRADES_PER_RUN`.

- [ ] **Step 1: Add constants and helper function to `src/strategy.ts`**

Find the constants block near the top of `src/strategy.ts` (around line 50, after imports and type definitions). Add after the existing constants:

```typescript
// ECMWF ±1°C confidence band converted to Fahrenheit
const CONFIDENCE_BAND_F = 1.8;

// Maximum YES price to enter on any bucket (including adjacent ones)
const DUAL_BUCKET_MAX_PRICE = 0.20;

/**
 * Fraction of the confidence band [bandMin, bandMax] that overlaps with
 * the bucket range [rangeMin, rangeMax]. Used to rank buckets by probability.
 * Infinity bounds are replaced with ±50 for overlap math.
 */
function bandOverlap(
  rangeMin: number,
  rangeMax: number,
  bandMin: number,
  bandMax: number
): number {
  const lo = Math.max(rangeMin, bandMin);
  const hi = Math.min(rangeMax, bandMax);
  if (hi <= lo) return 0;
  const bandWidth = bandMax - bandMin;
  return bandWidth > 0 ? (hi - lo) / bandWidth : 0;
}
```

- [ ] **Step 2: Replace single-bucket match with top-2 scored selection**

Find the current bucket-matching block in `src/strategy.ts` (lines ~316-350):
```typescript
      let matched:
        | {
            market: PolymarketMarket;
            question: string;
            price: number;
            range: [number, number];
          }
        | null = null;

      for (const market of event.markets ?? []) {
        const question = market.question ?? "";
        const rng = parseTempRange(question);
        if (rng && rng[0] <= forecastTemp && forecastTemp <= rng[1]) {
          try {
            const pricesStr = market.outcomePrices ?? "[0.5,0.5]";
            const prices = JSON.parse(pricesStr) as number[];
            const yesPrice = Number(prices[0]);
            if (!isFinite(yesPrice)) continue;
            matched = {
              market,
              question,
              price: yesPrice,
              range: rng
            };
          } catch {
            continue;
          }
          break;
        }
      }

      if (!matched) {
        skip(`No bucket found for ${forecastTemp}°F`);
        continue;
      }
```

Replace with:

```typescript
      interface ScoredBucket {
        market: PolymarketMarket;
        question: string;
        price: number;
        range: [number, number];
        score: number;
      }

      const bandLow  = forecastTemp - CONFIDENCE_BAND_F;
      const bandHigh = forecastTemp + CONFIDENCE_BAND_F;

      const scoredBuckets: ScoredBucket[] = [];

      for (const market of event.markets ?? []) {
        const question = market.question ?? "";
        const rng = parseTempRange(question);
        if (!rng) continue;

        // Map sentinel bounds (-999 / 999) to band-edge for overlap math
        const rMin = rng[0] === -999 ? bandLow - 10 : rng[0];
        const rMax = rng[1] ===  999 ? bandHigh + 10 : rng[1];

        const score = bandOverlap(rMin, rMax, bandLow, bandHigh);
        if (score <= 0) continue;

        try {
          const pricesStr = market.outcomePrices ?? "[0.5,0.5]";
          const prices = JSON.parse(pricesStr) as number[];
          const yesPrice = Number(prices[0]);
          if (!isFinite(yesPrice)) continue;
          if (yesPrice > DUAL_BUCKET_MAX_PRICE) continue;
          scoredBuckets.push({ market, question, price: yesPrice, range: rng, score });
        } catch {
          continue;
        }
      }

      // Sort by overlap score descending; take top 2 most likely buckets
      scoredBuckets.sort((a, b) => b.score - a.score);
      const topBuckets = scoredBuckets.slice(0, 2);

      if (topBuckets.length === 0) {
        skip(`No bucket found within ±${CONFIDENCE_BAND_F}°F of ${forecastTemp}°F at price ≤ $${DUAL_BUCKET_MAX_PRICE}`);
        continue;
      }
```

- [ ] **Step 3: Replace the single-bucket entry block with a loop over topBuckets**

Find the block from `const price = matched.price;` down to the entry order/simulation logic (approximately lines 352-475). This block processes one `matched` bucket. Replace `matched` references with a `for (const matched of topBuckets)` loop.

The existing structure is:
```typescript
      const price = matched.price;
      const marketId = matched.market.id;
      const question = matched.question;
      // ... display panel ...
      if (price > config.entry_threshold) { skip(...); continue; }
      // ... sizing ...
      // ... paper/execute entry ...
```

Replace with (wrapping the existing single-bucket block):
```typescript
      for (const matched of topBuckets) {
        const price = matched.price;
        const marketId = matched.market.id;
        const question = matched.question;
        const tone = priceTone(price, DUAL_BUCKET_MAX_PRICE, config.exit_threshold);
        console.log(
          panel(
            `Matched Bucket • ${shortQuestion(question, 52)}`,
            [
              stat("Forecast temp", `${forecastTemp}°F`, "cyan"),
              stat("Band", `[${bandLow.toFixed(1)}, ${bandHigh.toFixed(1)}]°F`, "blue"),
              stat("Overlap score", `${(matched.score * 100).toFixed(0)}%`, "cyan"),
              stat("YES price", `$${price.toFixed(3)}`, tone),
              stat("Entry gate", `< $${DUAL_BUCKET_MAX_PRICE.toFixed(2)}`, "green"),
              `${C.DIM("Market odds")}   ${progressBar(price, 1, 26, tone)}`
            ],
            tone
          )
        );

        // Note: DUAL_BUCKET_MAX_PRICE gate already applied during scoring above.
        // Skip duplicate position in same market (e.g. if re-run within same interval).
        if (marketId in positions) {
          skip(`Already have position in ${shortQuestion(question, 40)}`);
          continue;
        }

        if (tradeCount >= config.max_trades_per_run) {
          skip(`Max trades per run (${config.max_trades_per_run}) reached`);
          break;
        }

        const basePositionSize = Number((balance * POSITION_PCT).toFixed(2));
        const minOrderUsd =
          mode === "execute" ? MIN_EXECUTE_ORDER_USD : MIN_PAPER_ORDER_USD;
        const positionSize = Number(Math.max(basePositionSize, minOrderUsd).toFixed(2));

        if (balance < minOrderUsd) {
          skip(
            `Balance $${balance.toFixed(2)} below minimum order $${minOrderUsd.toFixed(2)}`
          );
          break;
        }

        const shares = positionSize / price;
        // ... (keep existing paper / execute entry code, substituting matched references) ...
        tradeCount += 1;
      }  // end topBuckets loop
```

> **Note:** The inner paper/execute entry code block (creating position object, calling CLOB, updating `sim`, etc.) is unchanged — it just moves inside the `for (const matched of topBuckets)` loop. Reference the existing lines ~393-480 for the full block; copy it verbatim inside the new loop, replacing any `matched` variable references which are already captured by the loop variable.

- [ ] **Step 4: Build TypeScript to verify compilation**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot && npm run build 2>&1 | tail -20
```
Expected: `0 errors` in tsc output. If there are errors, fix type mismatches (most common: `ScoredBucket` type needs to be declared at module scope if referenced outside the loop).

- [ ] **Step 5: Smoke test in dry-run mode**

```bash
cd /Users/padraighaughey/Polymarket-Weather-Bot && node dist/index.js --interval 0 2>&1 | head -60
```
Expected: strategy runs, shows "Matched Bucket" panels with "Overlap score" line, potentially shows 2 buckets per city/date where the confidence band spans multiple markets. No CLOB calls (dry-run).

- [ ] **Step 6: Commit**

```bash
git add src/strategy.ts
git commit -m "feat: buy top-2 temperature buckets by ECMWF confidence band overlap

Replace single-bucket point match with scored selection:
- Score each bucket by overlap with ±1.8°F confidence band (ECMWF ±1°C)
- Sort by score descending, buy top 2 where YES < $0.20
- Each bucket counted independently against MAX_TRADES_PER_RUN
- Adds CONFIDENCE_BAND_F and DUAL_BUCKET_MAX_PRICE constants"
```

---

## Post-Implementation Checklist

- [ ] Run all tests end-to-end: `python -m pytest test_weather_executor.py test_precip_forward.py test_precip_sigma_fix.py -v`
- [ ] Verify `dist/index.js --live --interval 30` (the running paper-trading process) still operates correctly after `npm run build`
- [ ] Verify `run_ecmwf_loop.py` still imports correctly: `python -c "import run_ecmwf_loop"`
- [ ] Check simulation.json open positions haven't been corrupted
- [ ] Confirm `precip_forward_test_yes.py` (dry-run) executes without error: `python precip_forward_test_yes.py`
- [ ] Confirm nothing is live: check `precip_forward_test_yes.py` only activates on days 18-20 and never sends orders without explicit `--execute`

---

## Notes

**On the running Node.js process (PID 6846):** `dist/index.js --live` maps to "paper" mode in `src/index.ts:102` (`const paper = Boolean(argv.live)`). Confirmed safe — no CLOB orders placed.

**On `precip_forward_test_yes.py` hardcoded `CHAIN_ID = 137`:** This is mainnet but is only reached when `--execute` is passed. The TRUST_WINDOWS change and WeatherExecutor wiring do not change this behavior.

**On Seoul/Hong Kong ERA5 bias:** These cities remain excluded from `TARGET_CITIES`. Do not add them until a primary data source (KMA/HKO API) replaces ERA5 for accumulated precipitation.

**On SDK version:** `precip_forward_test_yes.py` currently imports `py_clob_client` (v1). After Task 3, this import is removed and replaced by `WeatherExecutor` which uses `py_clob_client_v2`. Ensure v2 is installed: `pip install py-clob-client-v2`.
