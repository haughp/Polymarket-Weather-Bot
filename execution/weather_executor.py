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
import math
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
    Credentials loaded from /Users/padraighaughey/sniff_test_polymarket/.env
    using POLY_* variable names (same as sniff_test_polymarket project).

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
        "min size",
        "invalid amount",
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
            signature_type=0,
            funder=funder,
        )

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

        size_usdc: override session default for this order.
        In dry_run mode, simulates instant fill and returns success=True.
        """
        size = size_usdc if size_usdc is not None else self._size
        if self._dry_run:
            return self._simulate_buy(token_id, price, size)
        return self._live_buy(token_id, price, size)

    # ── Internal helpers ───────────────────────────────────────────

    def _simulate_buy(self, token_id: str, price: float, size: float) -> OrderResult:
        # Ceiling to 2dp mirrors the CLOB's internal floor; prevents amount < min $1
        shares = math.ceil(size / price * 100) / 100 if price > 0 else 0.0
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
        err_lower = err_str.lower()
        for fragment in self._TERMINAL_ERRORS:
            if fragment in err_lower:
                return "terminal"
        for fragment in self._RETRY_ERRORS:
            if fragment in err_lower:
                return "retry"
        return "unknown"

    def _live_buy(self, token_id: str, price: float, size: float) -> OrderResult:
        """Live GTC BUY order via py-clob-client-v2 with collateral sync and one retry."""
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

                # Ceiling to 2dp mirrors the CLOB's internal floor; prevents amount < min $1
                shares = math.ceil(size / price * 100) / 100 if price > 0 else 0.0
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
