#!/usr/bin/env python3
"""Backtest monthly precipitation NO-tail strategy against resolved Polymarket markets."""

from __future__ import annotations

import datetime
import json
import math
import time
from dataclasses import dataclass

import httpx

from database_schema import init_database, PrecipForecast, PrecipOutcome
from precip_forecast_pipeline import PRECIP_LOCATIONS
from polymarket_precip_dry_run import parse_precip_range

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
THRESHOLDS = [0.85, 0.90, 0.93, 0.95, 0.97]


@dataclass
class ResolvedMarket:
    location_id: str
    settlement_month: datetime.date
    question: str
    yes_won: bool


def _parse_yes_won(market: dict) -> bool | None:
    raw = market.get("outcomePrices", [])
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if len(prices) != 2:
            return None
        return float(prices[0]) > 0.5
    except Exception:
        return None


def _month_from_slug(slug: str) -> int | None:
    for month_name, idx in MONTHS.items():
        if f"-in-{month_name}" in slug:
            return idx
    return None


def _settlement_month_from_event(event: dict, month_idx: int) -> datetime.date | None:
    for key in ("endDate", "end_date", "startDate", "createdAt"):
        val = event.get(key)
        if not val:
            continue
        try:
            year = datetime.datetime.fromisoformat(val.replace("Z", "+00:00")).year
            return datetime.date(year, month_idx, 1)
        except Exception:
            continue
    return None


def _fetch_events_page(client: httpx.Client, params: dict) -> list[dict]:
    for attempt in range(5):
        try:
            r = client.get(GAMMA_EVENTS, params=params)
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                wait_s = float(retry_after) if retry_after else min(60.0, 2 ** attempt)
                time.sleep(wait_s)
                continue
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            if attempt == 4:
                return []
            time.sleep(min(10.0, 2 ** attempt))
    return []


def _fetch_events_paginated(
    client: httpx.Client,
    base_params: dict,
    max_pages: int = 12,
    page_size: int = 200,
) -> list[dict]:
    out: list[dict] = []
    seen_ids: set[str] = set()
    for page in range(max_pages):
        params = dict(base_params)
        params["limit"] = page_size
        params["offset"] = page * page_size
        rows = _fetch_events_page(client, params)
        if not rows:
            break
        added = 0
        for ev in rows:
            key = str(ev.get("id") or ev.get("slug") or f"row-{len(out)}")
            if key in seen_ids:
                continue
            seen_ids.add(key)
            out.append(ev)
            added += 1
        if len(rows) < page_size or added == 0:
            break
    return out


def fetch_resolved_precip_markets(limit: int = 1000) -> list[ResolvedMarket]:
    city_by_slug = {v["city_slug"]: k for k, v in PRECIP_LOCATIONS.items()}
    month_names = list(MONTHS.keys())
    with httpx.Client(timeout=30) as client:
        events: list[dict] = []
        seen_events: set[str] = set()
        # Deterministic slug matrix (city x month) is far more reliable than q-search.
        for city_slug in city_by_slug:
            for month_name in month_names:
                rows = _fetch_events_page(
                    client,
                    {"slug": f"precipitation-in-{city_slug}-in-{month_name}", "closed": "true"},
                )
                for ev in rows:
                    key = str(ev.get("id") or ev.get("slug") or f"ev-{len(events)}")
                    if key in seen_events:
                        continue
                    seen_events.add(key)
                    events.append(ev)

    out: list[ResolvedMarket] = []
    seen_market_rows: set[tuple[str, str, str]] = set()
    for ev in events:
        ev_slug = (ev.get("slug") or "").lower()
        ev_title = (ev.get("title") or "").lower()
        month_idx = _month_from_slug(ev_slug)

        for mkt in ev.get("markets", []):
            q = (mkt.get("question") or "").lower()
            yes_won = _parse_yes_won(mkt)
            if yes_won is None:
                continue

            location_id = None
            for city_slug, loc_id in city_by_slug.items():
                spaced = city_slug.replace("-", " ")
                underscored = city_slug.replace("-", "_")
                if (
                    city_slug in ev_slug
                    or city_slug in q
                    or city_slug in ev_title
                    or spaced in q
                    or spaced in ev_title
                    or underscored in q
                    or underscored in ev_title
                ):
                    location_id = loc_id
                    break
            if location_id is None:
                continue

            m_idx = month_idx
            if m_idx is None:
                for name, idx in MONTHS.items():
                    if name in ev_slug or name in q:
                        m_idx = idx
                        break
            if m_idx is None:
                continue

            settlement_month = _settlement_month_from_event(ev, m_idx)
            if settlement_month is None:
                continue

            q_raw = mkt.get("question") or ""
            dedupe_key = (location_id, settlement_month.isoformat(), q_raw.strip().lower())
            if dedupe_key in seen_market_rows:
                continue
            seen_market_rows.add(dedupe_key)

            out.append(
                ResolvedMarket(
                    location_id=location_id,
                    settlement_month=settlement_month,
                    question=q_raw,
                    yes_won=yes_won,
                )
            )
            if len(out) >= limit:
                return out
    return out[:limit]


def _bucket_yes(actual_precip: float, low: float | None, high: float | None) -> bool:
    bucket_low = low if low is not None else float("-inf")
    bucket_high = high if high is not None else float("inf")
    return bucket_low <= actual_precip <= bucket_high


def _no_confidence_for_bucket(
    low: float | None,
    high: float | None,
    p05: float,
    p95: float,
    days_remaining: int,
) -> float:
    """
    Estimate P(NO wins) for a bucket under a Gaussian approximation where:
      p05 ~= mu - 1.645*sigma
      p95 ~= mu + 1.645*sigma
    This avoids confidence saturation and makes thresholds meaningful.
    """
    # Only score tail buckets (outside the confidence band).
    is_lower_tail = high is not None and high <= p05
    is_upper_tail = low is not None and low >= p95
    if not (is_lower_tail or is_upper_tail):
        return 0.0

    # Degenerate band: if uncertainty collapsed, treat as near-certain point mass.
    if p95 <= p05:
        mu = p05
        in_bucket = (low is None or mu >= low) and (high is None or mu <= high)
        return 0.001 if in_bucket else 0.999

    mu = (p05 + p95) / 2.0
    sigma = max(0.01, (p95 - p05) / (2.0 * 1.645))
    # Early in month, uncertainty should be wider; late month naturally tightens.
    sigma *= (1.0 + max(0, days_remaining) / 10.0)

    def cdf(x: float) -> float:
        z = (x - mu) / (sigma * math.sqrt(2.0))
        return 0.5 * (1.0 + math.erf(z))

    lo = float("-inf") if low is None else low
    hi = float("inf") if high is None else high
    if lo > hi:
        lo, hi = hi, lo

    p_yes = max(0.0, min(1.0, cdf(hi) - cdf(lo)))
    p_no = 1.0 - p_yes
    return max(0.001, min(0.999, p_no))


def run_backtest(months_back: int = 12) -> dict:
    session = init_database()
    markets = fetch_resolved_precip_markets(limit=1500)

    cutoff_month = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    results = {
        t: {"wins": 0, "total": 0, "cities": set()} for t in THRESHOLDS
    }
    debug = {
        "markets_fetched": len(markets),
        "skipped_future_month": 0,
        "skipped_outside_month_window": 0,
        "markets_city_month_matched": 0,
        "markets_without_outcome": 0,
        "markets_bucket_parse_failed": 0,
        "markets_outcome_parse_mismatch": 0,
        "markets_with_snapshots": 0,
        "markets_without_snapshots": 0,
        "snapshots_evaluated": 0,
        "snapshots_with_tail_signal": 0,
    }
    threshold_triggers = {t: 0 for t in THRESHOLDS}

    for rm in markets:
        if rm.settlement_month > cutoff_month:
            debug["skipped_future_month"] += 1
            continue
        months_delta = (cutoff_month.year - rm.settlement_month.year) * 12 + (cutoff_month.month - rm.settlement_month.month)
        if months_delta >= months_back:
            debug["skipped_outside_month_window"] += 1
            continue

        debug["markets_city_month_matched"] += 1
        outcome = (
            session.query(PrecipOutcome)
            .filter_by(location_id=rm.location_id, settlement_month=rm.settlement_month)
            .first()
        )
        if not outcome or outcome.actual_precip is None:
            debug["markets_without_outcome"] += 1
            continue

        low, high = parse_precip_range(rm.question)
        if low is None and high is None:
            debug["markets_bucket_parse_failed"] += 1
            continue

        actual_precip = float(outcome.actual_precip)
        actual_yes_from_outcome = _bucket_yes(actual_precip, low, high)
        if actual_yes_from_outcome != rm.yes_won:
            debug["markets_outcome_parse_mismatch"] += 1
            continue

        snapshots = (
            session.query(PrecipForecast)
            .filter_by(location_id=rm.location_id, settlement_month=rm.settlement_month)
            .order_by(PrecipForecast.timestamp.asc())
            .all()
        )
        if not snapshots:
            debug["markets_without_snapshots"] += 1
            continue
        debug["markets_with_snapshots"] += 1

        for snap in snapshots:
            debug["snapshots_evaluated"] += 1
            p05 = float(snap.p05)
            p95 = float(snap.p95)
            conf_no = _no_confidence_for_bucket(low, high, p05, p95, int(snap.days_remaining or 0))
            if conf_no <= 0:
                continue
            debug["snapshots_with_tail_signal"] += 1

            for thr in THRESHOLDS:
                if conf_no >= thr:
                    threshold_triggers[thr] += 1
                    results[thr]["total"] += 1
                    if not rm.yes_won:
                        results[thr]["wins"] += 1
                    results[thr]["cities"].add(rm.location_id)

    session.close()
    return {"results": results, "debug": debug, "threshold_triggers": threshold_triggers}


def _print_report(payload: dict) -> None:
    results = payload["results"]
    debug = payload["debug"]
    threshold_triggers = payload["threshold_triggers"]
    print("\nDebug Counters")
    print("-" * 45)
    for k, v in debug.items():
        print(f"{k:32s} {v}")
    print("\nThreshold Trigger Counts")
    print("-" * 45)
    for thr in THRESHOLDS:
        print(f"{int(thr * 100):>3}% -> {threshold_triggers[thr]}")

    print("\nThreshold | Trades | Win Rate | Cities")
    print("-" * 45)
    for thr in THRESHOLDS:
        total = results[thr]["total"]
        wins = results[thr]["wins"]
        wr = (wins / total * 100.0) if total else 0.0
        cities = ",".join(sorted(results[thr]["cities"])) if results[thr]["cities"] else "-"
        print(f"{int(thr*100):>3}%      | {total:>6} | {wr:>7.2f}% | {cities}")

    print("\nGate: win_rate >= 95%, trades >= 30, cities >= 3")
    passed = False
    for thr in THRESHOLDS:
        total = results[thr]["total"]
        wins = results[thr]["wins"]
        wr = (wins / total * 100.0) if total else 0.0
        city_count = len(results[thr]["cities"])
        if total >= 30 and city_count >= 3 and wr >= 95.0:
            print(f"PASS at {int(thr*100)}% threshold ({total} trades, {wr:.2f}% win rate, {city_count} cities)")
            passed = True
    if not passed:
        print("FAIL: no threshold met deployment gate")


if __name__ == "__main__":
    res = run_backtest(months_back=12)
    _print_report(res)
