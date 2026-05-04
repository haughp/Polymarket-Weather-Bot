#!/usr/bin/env python3
"""
Forward test model: Buy YES on top forecast bucket for monthly precipitation.

Strategy v1:
- City scope: Seattle, NYC
- Timing window: day 18-20 of month (+-1 day fallback for pipeline downtime)
- Forecast model: ecmwf_ec46 (from precip_forecast_pipeline metadata)
- Primary trade: $1 BUY YES in bucket containing forecast total.
- Adjacent trade: if p05 or p95 crosses into an adjacent bucket, also buy
  that bucket. Maximum 2 buys per city per month execution.

Default mode is dry-run; pass --execute for live order submission.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
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


@dataclass
class Candidate:
    location_id: str
    settlement_month: datetime.date
    question: str
    low: float | None
    high: float | None
    yes_token: str
    best_ask: float
    yes_mid: float
    forecast_total: float
    units: str


def _clob_yes_token(market: dict) -> str | None:
    raw = market.get("clobTokenIds", "[]") or "[]"
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return str(ids[0]) if ids else None
    except Exception:
        return None


def _prices(market: dict) -> tuple[float | None, float | None]:
    raw = market.get("outcomePrices", "[0.5,0.5]") or "[0.5,0.5]"
    try:
        p = json.loads(raw) if isinstance(raw, str) else raw
        return float(p[0]), float(p[1])
    except Exception:
        return None, None


def _fetch_event_markets(city_slug: str, month_str: str) -> list[dict]:
    slugs_to_try = [f"precipitation-in-{city_slug}-in-{month_str}"]
    if city_slug == "nyc":
        slugs_to_try.extend([
            f"precipitation-in-new-york-in-{month_str}",
            f"precipitation-in-new-york-city-in-{month_str}",
        ])
    elif city_slug == "seattle":
        slugs_to_try.append(f"precipitation-in-seattle-wa-in-{month_str}")

    for slug in slugs_to_try:
        try:
            with httpx.Client(timeout=12) as client:
                r = client.get(GAMMA_EVENTS, params={"slug": slug})
                data = r.json()
                if data and data[0].get("markets"):
                    return data[0].get("markets", [])
        except Exception:
            continue
    return []


def _best_yes_ask(token_id_yes: str) -> float | None:
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{CLOB_BASE}/book", params={"token_id": token_id_yes})
            book = r.json()
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        return float(asks[0]["price"]) if asks else None
    except Exception:
        return None


def _select_candidates_from_markets(
    loc_id: str, fc: dict, markets: list[dict]
) -> list[Candidate]:
    """
    Return up to 2 Candidates from a list of Polymarket markets for a city/month:
      1. Primary: the bucket containing fc['total_forecast']
      2. Adjacent: the bucket that fc['p05'] or fc['p95'] crosses into (if any)

    When p05 crosses the lower boundary and p95 crosses the upper boundary,
    the adjacent bucket with the larger confidence-band overlap is chosen.
    """
    total = float(fc["total_forecast"])
    p05   = float(fc.get("p05", total))
    p95   = float(fc.get("p95", total))

    primary: Optional[Candidate] = None
    lower_adj: Optional[Candidate] = None
    upper_adj: Optional[Candidate] = None

    def _make_candidate(m: dict, low, high) -> Optional[Candidate]:
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
        if p05 < total and lo <= p05 <= hi and not (lo <= total <= hi) and lower_adj is None:
            lower_adj = _make_candidate(m, low, high)
            continue

        # Upper adjacent: bucket contains p95 but NOT total
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


def _already_traded_today(session, loc_id: str, smonth: datetime.date, question: str) -> bool:
    start = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    end = datetime.datetime.combine(datetime.date.today(), datetime.time.max)
    c = (
        session.query(PrecipStrategyTrade)
        .filter(
            PrecipStrategyTrade.mode == "forward_yes_v1",
            PrecipStrategyTrade.location_id == loc_id,
            PrecipStrategyTrade.settlement_month == smonth,
            PrecipStrategyTrade.question_text == question,
            PrecipStrategyTrade.timestamp >= start,
            PrecipStrategyTrade.timestamp <= end,
        )
        .count()
    )
    return c > 0


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


def main() -> None:
    ap = argparse.ArgumentParser(description="Forward test: $1 BUY YES on forecast bucket")
    ap.add_argument("--execute", action="store_true", help="Submit live orders via CLOB")
    args = ap.parse_args()
    run(execute=bool(args.execute))


if __name__ == "__main__":
    main()
