#!/usr/bin/env python3
"""Polymarket precipitation strategy v1: scan, backtest, calibrate."""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import func

from database_schema import (
    PrecipForecast,
    PrecipOutcome,
    PrecipStrategySignal,
    PrecipStrategyTrade,
    init_database,
)
from polymarket_precip_dry_run import parse_precip_range
from precip_forecast_pipeline import PRECIP_LOCATIONS

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BASE = "https://clob.polymarket.com"
MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
THRESHOLDS = [0.90, 0.93, 0.95, 0.97]
PRICE_CAPS = [0.25, 0.30, 0.35, 0.40]


@dataclass
class StrategyConfig:
    confidence_threshold: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.95"))
    target_ev: float = float(os.getenv("TARGET_EV", "0.10"))
    hard_price_cap: float = float(os.getenv("HARD_PRICE_CAP", "0.30"))
    trade_size_usd: float = float(os.getenv("TRADE_SIZE_USD", "5.0"))
    max_positions_per_city_month: int = int(os.getenv("MAX_POSITIONS_PER_CITY_MONTH", "1"))
    daily_loss_stop_usd: float = float(os.getenv("DAILY_LOSS_STOP_USD", "20"))
    kill_switch: bool = os.getenv("KILL_SWITCH", "false").lower() in {"1", "true", "yes"}
    allowed_cities: tuple[str, ...] = tuple(
        s.strip() for s in os.getenv("ALLOWED_CITIES", "seattle,nyc,london").split(",") if s.strip()
    )
    http_min_delay_s: float = float(os.getenv("HTTP_MIN_DELAY_S", "0.15"))


class RateLimitedClient:
    def __init__(self, timeout: int = 20, min_delay_s: float = 0.15):
        self.client = httpx.Client(timeout=timeout)
        self.min_delay_s = min_delay_s
        self.last_ts = 0.0
        self.request_count = 0
        self.rate_limit_hits = 0

    def close(self) -> None:
        self.client.close()

    def _pace(self) -> None:
        now = time.time()
        delta = now - self.last_ts
        if delta < self.min_delay_s:
            time.sleep(self.min_delay_s - delta)

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        for attempt in range(6):
            self._pace()
            self.request_count += 1
            try:
                r = self.client.get(url, params=params)
                self.last_ts = time.time()
                if r.status_code == 429:
                    self.rate_limit_hits += 1
                    retry_after = r.headers.get("Retry-After")
                    wait_s = float(retry_after) if retry_after else min(60.0, 2 ** attempt)
                    time.sleep(wait_s)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(min(15.0, 2 ** attempt))
        return []


def parse_prices(raw: Any) -> tuple[float | None, float | None]:
    try:
        p = json.loads(raw) if isinstance(raw, str) else raw
        if not p or len(p) < 2:
            return None, None
        return float(p[0]), float(p[1])
    except Exception:
        return None, None


def no_token_id(market: dict) -> str | None:
    raw = market.get("clobTokenIds", "[]") or "[]"
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return str(ids[1]) if len(ids) > 1 else None
    except Exception:
        return None


def marketable_no_price(rl: RateLimitedClient, token_no: str | None) -> float | None:
    if not token_no:
        return None
    try:
        book = rl.get_json(f"{CLOB_BASE}/book", params={"token_id": token_no})
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        return float(asks[0]["price"]) if asks else None
    except Exception:
        return None


def discover_closed_events(rl: RateLimitedClient) -> list[dict]:
    events: list[dict] = []
    seen: set[str] = set()
    for loc in PRECIP_LOCATIONS.values():
        city_slug = loc["city_slug"]
        for month_name in MONTHS:
            slug = f"precipitation-in-{city_slug}-in-{month_name}"
            try:
                rows = rl.get_json(GAMMA_EVENTS, params={"slug": slug, "closed": "true"})
            except Exception:
                rows = []
            if not isinstance(rows, list):
                continue
            for ev in rows:
                key = str(ev.get("id") or ev.get("slug") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                events.append(ev)
    return events


def discover_open_event_markets(rl: RateLimitedClient, city_slug: str, month_name: str) -> list[dict]:
    slugs_to_try = [f"precipitation-in-{city_slug}-in-{month_name}"]
    if city_slug == "nyc":
        slugs_to_try.extend([f"precipitation-in-new-york-in-{month_name}", f"precipitation-in-new-york-city-in-{month_name}"])
    elif city_slug == "seattle":
        slugs_to_try.append(f"precipitation-in-seattle-wa-in-{month_name}")

    for slug in slugs_to_try:
        try:
            rows = rl.get_json(GAMMA_EVENTS, params={"slug": slug})
            if isinstance(rows, list) and rows:
                return rows[0].get("markets", []) or []
        except Exception:
            continue
            
    return []


def settlement_month(ev: dict) -> datetime.date | None:
    slug = (ev.get("slug") or "").lower()
    m_idx = None
    for idx, m_name in enumerate(MONTHS, start=1):
        if f"-in-{m_name}" in slug:
            m_idx = idx
            break
    if m_idx is None:
        return None
    for k in ("endDate", "end_date", "startDate", "createdAt"):
        v = ev.get(k)
        if not v:
            continue
        try:
            year = datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).year
            return datetime.date(year, m_idx, 1)
        except Exception:
            continue
    return None


def bucket_probability_yes(low: float | None, high: float | None, p05: float, p95: float, days_remaining: int) -> float:
    if p95 <= p05:
        x = p05
        return 1.0 if (low is None or x >= low) and (high is None or x <= high) else 0.0
    mu = (p05 + p95) / 2.0
    sigma = max(0.01, (p95 - p05) / (2.0 * 1.645))

    def cdf(x: float) -> float:
        z = (x - mu) / (sigma * math.sqrt(2.0))
        return 0.5 * (1.0 + math.erf(z))

    lo = float("-inf") if low is None else low
    hi = float("inf") if high is None else high
    p_yes = max(0.0, min(1.0, cdf(hi) - cdf(lo)))
    return p_yes


def load_latest_forecasts(session, allowed_cities: tuple[str, ...]) -> list[PrecipForecast]:
    latest_subq = (
        session.query(
            PrecipForecast.location_id,
            func.max(PrecipForecast.created_at).label("max_ts"),
        )
        .group_by(PrecipForecast.location_id)
        .subquery()
    )
    rows = (
        session.query(PrecipForecast)
        .join(
            latest_subq,
            (PrecipForecast.location_id == latest_subq.c.location_id)
            & (PrecipForecast.created_at == latest_subq.c.max_ts),
        )
        .filter(PrecipForecast.location_id.in_(allowed_cities))
        .all()
    )
    return rows


def would_block_for_risk(session, cfg: StrategyConfig, loc_id: str, smonth: datetime.date) -> str | None:
    if cfg.kill_switch:
        return "kill_switch"
    open_count = (
        session.query(PrecipStrategyTrade)
        .filter_by(mode="scan", location_id=loc_id, settlement_month=smonth)
        .count()
    )
    if open_count >= cfg.max_positions_per_city_month:
        return "max_positions_city_month"
    start_day = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
    end_day = datetime.datetime.combine(datetime.date.today(), datetime.time.max)
    pnl = (
        session.query(func.coalesce(func.sum(PrecipStrategyTrade.realized_pnl), 0.0))
        .filter(
            PrecipStrategyTrade.mode == "scan",
            PrecipStrategyTrade.timestamp >= start_day,
            PrecipStrategyTrade.timestamp <= end_day,
        )
        .scalar()
        or 0.0
    )
    if float(pnl) <= -abs(cfg.daily_loss_stop_usd):
        return "daily_loss_stop"
    return None


def run_scan(cfg: StrategyConfig) -> None:
    session = init_database()
    rl = RateLimitedClient(min_delay_s=cfg.http_min_delay_s)
    try:
        forecasts = load_latest_forecasts(session, cfg.allowed_cities)
        if not forecasts:
            print("❌ No latest precip forecasts found for allowed cities.")
            return
        inserted_signals = 0
        inserted_trades = 0
        for fc in forecasts:
            obs = PRECIP_LOCATIONS.get(fc.location_id)
            if not obs:
                continue
            month_name = MONTHS[fc.settlement_month.month - 1]
            markets = discover_open_event_markets(rl, obs["city_slug"], month_name)
            for mkt in markets:
                q = mkt.get("question") or ""
                low, high = parse_precip_range(q)
                if low is None and high is None:
                    continue
                yes_p, no_p = parse_prices(mkt.get("outcomePrices"))
                if no_p is None:
                    continue
                p_yes = bucket_probability_yes(low, high, float(fc.p05), float(fc.p95), int(fc.days_remaining or 0))
                p_no = 1.0 - p_yes
                fair_no = p_no
                edge_no = fair_no - float(no_p)
                max_entry_no = fair_no / (1.0 + cfg.target_ev)
                blocked = None
                if p_no < cfg.confidence_threshold:
                    blocked = "below_confidence"
                elif float(no_p) > max_entry_no:
                    blocked = "no_price_above_ev_cap"
                elif float(no_p) > cfg.hard_price_cap:
                    blocked = "no_price_above_hard_cap"
                else:
                    blocked = would_block_for_risk(session, cfg, fc.location_id, fc.settlement_month)

                token_no = no_token_id(mkt)
                sig = PrecipStrategySignal(
                    mode="scan",
                    location_id=fc.location_id,
                    settlement_month=fc.settlement_month,
                    forecast_id=fc.id,
                    question_text=q,
                    clob_token_no=token_no,
                    bucket_low=low,
                    bucket_high=high,
                    market_yes=yes_p,
                    market_no=no_p,
                    p_no=round(p_no, 5),
                    fair_no=round(fair_no, 5),
                    edge_no=round(edge_no, 5),
                    max_entry_no=round(max_entry_no, 5),
                    days_remaining=fc.days_remaining,
                    data_source=fc.source,
                    blocked_reason=blocked,
                )
                session.add(sig)
                session.flush()
                inserted_signals += 1

                if blocked is None:
                    entry_cap = min(max_entry_no, cfg.hard_price_cap)
                    mkt_ask = marketable_no_price(rl, token_no)
                    fill_price = mkt_ask if mkt_ask is not None and mkt_ask <= entry_cap else None
                    trade_blocked = None if fill_price is not None else "no_ask_within_cap"
                    trade = PrecipStrategyTrade(
                        mode="scan",
                        location_id=fc.location_id,
                        settlement_month=fc.settlement_month,
                        signal_id=sig.id,
                        question_text=q,
                        clob_token_no=token_no,
                        side="NO",
                        size_usd=cfg.trade_size_usd,
                        limit_price=round(entry_cap, 5),
                        fill_price=round(fill_price, 5) if fill_price is not None else None,
                        p_no=round(p_no, 5),
                        fair_no=round(fair_no, 5),
                        edge_no=round(edge_no, 5),
                        days_remaining=fc.days_remaining,
                        data_source=fc.source,
                        blocked_reason=trade_blocked,
                    )
                    session.add(trade)
                    inserted_trades += 1

        session.commit()
        print(f"✅ scan complete: signals={inserted_signals} trades={inserted_trades}")
        print(f"ℹ️ http requests={rl.request_count} rate_limit_hits={rl.rate_limit_hits}")
    finally:
        rl.close()
        session.close()


def fetch_resolved_markets_for_backtest(rl: RateLimitedClient) -> list[tuple[str, datetime.date, str, bool]]:
    out: list[tuple[str, datetime.date, str, bool]] = []
    events = discover_closed_events(rl)
    city_by_slug = {v["city_slug"]: k for k, v in PRECIP_LOCATIONS.items()}
    for ev in events:
        sm = settlement_month(ev)
        if sm is None:
            continue
        ev_slug = (ev.get("slug") or "").lower()
        loc_id = None
        for cslug, lid in city_by_slug.items():
            if cslug in ev_slug:
                loc_id = lid
                break
        if loc_id is None:
            continue
        for m in ev.get("markets", []):
            yes_p, _ = parse_prices(m.get("outcomePrices"))
            if yes_p is None:
                continue
            yes_won = yes_p > 0.5
            out.append((loc_id, sm, m.get("question") or "", yes_won))
    return out


def run_backtest(cfg: StrategyConfig, months_back: int) -> None:
    session = init_database()
    rl = RateLimitedClient(min_delay_s=cfg.http_min_delay_s)
    try:
        session.query(PrecipStrategyTrade).filter(PrecipStrategyTrade.mode == "backtest").delete()
        session.query(PrecipStrategySignal).filter(PrecipStrategySignal.mode == "backtest").delete()
        session.commit()
        cutoff_month = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
        rows = fetch_resolved_markets_for_backtest(rl)
        trades = 0
        wins = 0
        city_counts: dict[str, int] = {}
        by_threshold = {t: {"wins": 0, "total": 0} for t in THRESHOLDS}
        by_price_cap = {c: {"wins": 0, "total": 0} for c in PRICE_CAPS}
        by_days_bucket = {"0-7": [0, 0], "8-14": [0, 0], "15-21": [0, 0], "22+": [0, 0]}

        for loc_id, smonth, q, yes_won in rows:
            if loc_id not in cfg.allowed_cities:
                continue
            delta = (cutoff_month.year - smonth.year) * 12 + (cutoff_month.month - smonth.month)
            if delta < 0 or delta >= months_back:
                continue
            outcome = session.query(PrecipOutcome).filter_by(location_id=loc_id, settlement_month=smonth).first()
            if not outcome or outcome.actual_precip is None or not bool(outcome.verified):
                continue
            low, high = parse_precip_range(q)
            if low is None and high is None:
                continue
            actual = float(outcome.actual_precip)
            y_true = (low is None or actual >= low) and (high is None or actual <= high)
            if y_true != yes_won:
                continue

            snaps = (
                session.query(PrecipForecast)
                .filter_by(location_id=loc_id, settlement_month=smonth)
                .order_by(PrecipForecast.timestamp.asc())
                .all()
            )
            if not snaps:
                continue
            for snap in snaps:
                p_yes = bucket_probability_yes(low, high, float(snap.p05), float(snap.p95), int(snap.days_remaining or 0))
                p_no = 1.0 - p_yes
                fair_no = p_no
                max_entry_no = fair_no / (1.0 + cfg.target_ev)
                if p_no < cfg.confidence_threshold:
                    continue
                # Historical as-of market microprice is not available from Gamma for prior
                # snapshots; use strategy cap as a conservative executable proxy.
                exec_no = min(max_entry_no, cfg.hard_price_cap)
                trades += 1
                city_counts[loc_id] = city_counts.get(loc_id, 0) + 1
                win = not yes_won
                if win:
                    wins += 1
                for thr in THRESHOLDS:
                    if p_no >= thr:
                        by_threshold[thr]["total"] += 1
                        by_threshold[thr]["wins"] += 1 if win else 0
                for cap in PRICE_CAPS:
                    if exec_no <= cap:
                        by_price_cap[cap]["total"] += 1
                        by_price_cap[cap]["wins"] += 1 if win else 0
                dr = int(snap.days_remaining or 0)
                key = "22+" if dr >= 22 else "15-21" if dr >= 15 else "8-14" if dr >= 8 else "0-7"
                by_days_bucket[key][1] += 1
                by_days_bucket[key][0] += 1 if win else 0

                signal = PrecipStrategySignal(
                    mode="backtest",
                    location_id=loc_id,
                    settlement_month=smonth,
                    forecast_id=snap.id,
                    question_text=q,
                    bucket_low=low,
                    bucket_high=high,
                    market_no=round(exec_no, 5),
                    p_no=round(p_no, 5),
                    fair_no=round(fair_no, 5),
                    edge_no=round(fair_no - exec_no, 5),
                    max_entry_no=round(max_entry_no, 5),
                    days_remaining=dr,
                    data_source=snap.source,
                    blocked_reason=None,
                )
                session.add(signal)
                session.flush()
                stake = cfg.trade_size_usd
                pnl = (stake / exec_no) * (1.0 - exec_no) if win and exec_no > 0 else (-stake if not win else 0.0)
                session.add(
                    PrecipStrategyTrade(
                        mode="backtest",
                        location_id=loc_id,
                        settlement_month=smonth,
                        signal_id=signal.id,
                        question_text=q,
                        side="NO",
                        size_usd=stake,
                        limit_price=round(max_entry_no, 5),
                        fill_price=round(exec_no, 5),
                        p_no=round(p_no, 5),
                        fair_no=round(fair_no, 5),
                        edge_no=round(fair_no - exec_no, 5),
                        days_remaining=dr,
                        data_source=snap.source,
                        realized_pnl=round(pnl, 2),
                        resolved_win=win,
                    )
                )
        session.commit()
        wr = (wins / trades * 100.0) if trades else 0.0
        print(f"✅ backtest complete months_back={months_back} trades={trades} win_rate={wr:.2f}% cities={sorted(city_counts)}")
        print("\nthreshold win-rate")
        for thr in THRESHOLDS:
            t = by_threshold[thr]["total"]
            w = by_threshold[thr]["wins"]
            print(f"{int(thr*100)}%: trades={t} win_rate={(w/t*100 if t else 0):.2f}%")
        print("\nprice-cap win-rate")
        for cap in PRICE_CAPS:
            t = by_price_cap[cap]["total"]
            w = by_price_cap[cap]["wins"]
            print(f"{cap:.2f}: trades={t} win_rate={(w/t*100 if t else 0):.2f}%")
        print("\ntrades by days_remaining bucket")
        for k, (w, t) in by_days_bucket.items():
            print(f"{k}: trades={t} win_rate={(w/t*100 if t else 0):.2f}%")
        print(f"\nℹ️ http requests={rl.request_count} rate_limit_hits={rl.rate_limit_hits}")
    finally:
        rl.close()
        session.close()


def run_calibrate(cfg: StrategyConfig) -> None:
    session = init_database()
    try:
        rows = (
            session.query(PrecipStrategyTrade)
            .filter(PrecipStrategyTrade.mode == "backtest")
            .all()
        )
        if not rows:
            print("❌ no backtest trades found. run backtest first.")
            return
        print("threshold,price_cap,trades,win_rate,pnl")
        for thr in THRESHOLDS:
            for cap in PRICE_CAPS:
                subset = [
                    r for r in rows
                    if float(r.p_no or 0) >= thr
                    and r.fill_price is not None
                    and float(r.fill_price) <= cap
                ]
                t = len(subset)
                w = sum(1 for r in subset if r.resolved_win)
                pnl = sum(float(r.realized_pnl or 0.0) for r in subset)
                wr = (w / t * 100.0) if t else 0.0
                print(f"{thr:.2f},{cap:.2f},{t},{wr:.2f},{pnl:.2f}")
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Precipitation strategy v1")
    parser.add_argument("--mode", choices=["scan", "backtest", "calibrate"], default="scan")
    parser.add_argument("--months-back", type=int, default=18)
    args = parser.parse_args()
    cfg = StrategyConfig()
    if args.mode == "scan":
        run_scan(cfg)
    elif args.mode == "backtest":
        run_backtest(cfg, months_back=args.months_back)
    else:
        run_calibrate(cfg)


if __name__ == "__main__":
    main()
