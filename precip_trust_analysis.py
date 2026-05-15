#!/usr/bin/env python3
"""Top-1 trust analysis for monthly precipitation markets."""

from __future__ import annotations

import argparse
import datetime
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import httpx

from database_schema import PrecipForecast, init_database
from polymarket_precip_dry_run import parse_precip_range
from precip_forecast_pipeline import PRECIP_LOCATIONS

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
ALLOWED_CITIES = ("seattle", "nyc", "london")
DAY_BUCKETS = ("d00-05", "d06-10", "d11-15", "d16-20", "d21-25", "d26+")


@dataclass
class MarketRow:
    question: str
    yes_won: bool
    low: float | None
    high: float | None


class RLClient:
    def __init__(self, timeout: int = 20, min_delay: float = 0.15):
        self.c = httpx.Client(timeout=timeout)
        self.min_delay = min_delay
        self.last_t = 0.0
        self.requests = 0
        self.rate_limit_hits = 0

    def close(self) -> None:
        self.c.close()

    def _pace(self) -> None:
        now = time.time()
        dt = now - self.last_t
        if dt < self.min_delay:
            time.sleep(self.min_delay - dt)

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        for attempt in range(6):
            self._pace()
            self.requests += 1
            try:
                r = self.c.get(url, params=params)
                self.last_t = time.time()
                if r.status_code == 429:
                    self.rate_limit_hits += 1
                    ra = r.headers.get("Retry-After")
                    wait = float(ra) if ra else min(60.0, 2 ** attempt)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception:
                if attempt == 5:
                    return []
                time.sleep(min(10.0, 2 ** attempt))
        return []


def parse_yes_won(market: dict) -> bool | None:
    raw = market.get("outcomePrices", [])
    try:
        p = json.loads(raw) if isinstance(raw, str) else raw
        if len(p) != 2:
            return None
        return float(p[0]) > 0.5
    except Exception:
        return None


def p_yes_for_bucket(low: float | None, high: float | None, p05: float, p95: float, days_remaining: int) -> float:
    if p95 <= p05:
        x = p05
        return 1.0 if (low is None or x >= low) and (high is None or x <= high) else 0.0
    mu = (p05 + p95) / 2.0
    sigma = max(0.01, (p95 - p05) / (2.0 * 1.645))
    sigma *= (1.0 + max(0, days_remaining) / 10.0)

    def cdf(x: float) -> float:
        z = (x - mu) / (sigma * math.sqrt(2.0))
        return 0.5 * (1.0 + math.erf(z))

    lo = float("-inf") if low is None else low
    hi = float("inf") if high is None else high
    return max(0.0, min(1.0, cdf(hi) - cdf(lo)))


def day_bucket(days_elapsed: int) -> str:
    if days_elapsed <= 5:
        return "d00-05"
    if days_elapsed <= 10:
        return "d06-10"
    if days_elapsed <= 15:
        return "d11-15"
    if days_elapsed <= 20:
        return "d16-20"
    if days_elapsed <= 25:
        return "d21-25"
    return "d26+"


def month_range(months_back: int) -> list[datetime.date]:
    today = datetime.date.today()
    out = []
    y = today.year
    m = today.month
    for k in range(1, months_back + 1):
        mm = m - k
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append(datetime.date(yy, mm, 1))
    return sorted(out)


def fetch_resolved_event_markets(rl: RLClient, city_slug: str, month_name: str, year: int) -> list[MarketRow]:
    slug = f"precipitation-in-{city_slug}-in-{month_name}"
    rows = rl.get_json(GAMMA_EVENTS, {"slug": slug, "closed": "true"})
    if not isinstance(rows, list) or not rows:
        return []
    target = None
    for ev in rows:
        end_str = ev.get("endDate") or ev.get("end_date") or ""
        if str(year) in end_str:
            target = ev
            break
    if target is None:
        target = rows[0]
    out: list[MarketRow] = []
    for m in target.get("markets", []):
        yes_won = parse_yes_won(m)
        if yes_won is None:
            continue
        q = m.get("question") or ""
        low, high = parse_precip_range(q)
        if low is None and high is None:
            continue
        out.append(MarketRow(question=q, yes_won=yes_won, low=low, high=high))
    return out


def run_analysis(months_back: int) -> None:
    session = init_database()
    rl = RLClient()
    try:
        months = month_range(months_back)
        event_cache: dict[tuple[str, datetime.date], list[MarketRow]] = {}
        gap_counts = defaultdict(int)
        metrics_rows = []          # top-1 rows
        metrics_rows_all = []      # v2 all-bucket rows

        forecasts = (
            session.query(PrecipForecast)
            .filter(PrecipForecast.location_id.in_(ALLOWED_CITIES))
            .filter(PrecipForecast.settlement_month.in_(months))
            .order_by(PrecipForecast.location_id.asc(), PrecipForecast.settlement_month.asc(), PrecipForecast.timestamp.asc())
            .all()
        )
        for fc in forecasts:
            city_slug = PRECIP_LOCATIONS[fc.location_id]["city_slug"]
            sm = fc.settlement_month
            cache_key = (fc.location_id, sm)
            if cache_key not in event_cache:
                event_cache[cache_key] = fetch_resolved_event_markets(rl, city_slug, MONTHS[sm.month - 1], sm.year)
            mkts = event_cache[cache_key]
            b = day_bucket(int(fc.days_elapsed or 0))

            if not mkts:
                gap_counts[(fc.location_id, b, "no_market_event")] += 1
                continue

            scored = []
            for m in mkts:
                p_yes = p_yes_for_bucket(m.low, m.high, float(fc.p05), float(fc.p95), int(fc.days_remaining or 0))
                scored.append((p_yes, m))
                y_all = 1.0 if m.yes_won else 0.0
                p_all = max(1e-6, min(1 - 1e-6, float(p_yes)))
                brier_all = (p_all - y_all) ** 2
                logloss_all = -(y_all * math.log(p_all) + (1 - y_all) * math.log(1 - p_all))
                metrics_rows_all.append((fc.location_id, b, p_all, y_all, brier_all, logloss_all))
            if not scored:
                gap_counts[(fc.location_id, b, "no_parsable_market")] += 1
                continue
            top_p, top_m = max(scored, key=lambda x: x[0])
            y = 1.0 if top_m.yes_won else 0.0
            p = max(1e-6, min(1 - 1e-6, float(top_p)))
            brier = (p - y) ** 2
            logloss = -(y * math.log(p) + (1 - y) * math.log(1 - p))
            metrics_rows.append((fc.location_id, b, p, y, brier, logloss))

        agg = defaultdict(lambda: {"n": 0, "acc_hits": 0, "brier": 0.0, "logloss": 0.0, "p": [], "y": []})
        for city, b, p, y, br, ll in metrics_rows:
            k = (city, b)
            agg[k]["n"] += 1
            agg[k]["acc_hits"] += int((p >= 0.5 and y == 1.0) or (p < 0.5 and y == 0.0))
            agg[k]["brier"] += br
            agg[k]["logloss"] += ll
            agg[k]["p"].append(p)
            agg[k]["y"].append(y)

        agg_all = defaultdict(lambda: {"n": 0, "brier": 0.0, "logloss": 0.0, "p": [], "y": []})
        for city, b, p, y, br, ll in metrics_rows_all:
            k = (city, b)
            agg_all[k]["n"] += 1
            agg_all[k]["brier"] += br
            agg_all[k]["logloss"] += ll
            agg_all[k]["p"].append(p)
            agg_all[k]["y"].append(y)

        print(f"Top-1 trust analysis over {months_back} months")
        print(f"HTTP requests={rl.requests} rate_limit_hits={rl.rate_limit_hits}")
        print("\ncity,bucket,n,accuracy,brier,logloss,auc")
        for city in ALLOWED_CITIES:
            for b in DAY_BUCKETS:
                k = (city, b)
                d = agg.get(k)
                if not d or d["n"] == 0:
                    print(f"{city},{b},0,NULL,NULL,NULL,NULL")
                    continue
                n = d["n"]
                acc = d["acc_hits"] / n
                brier = d["brier"] / n
                logloss = d["logloss"] / n
                auc = auc_roc(d["y"], d["p"])
                auc_str = f"{auc:.4f}" if auc is not None else "NULL"
                print(f"{city},{b},{n},{acc:.4f},{brier:.5f},{logloss:.5f},{auc_str}")

        print("\nALL-BUCKET v2 analysis")
        print("city,bucket,n,brier,logloss,auc")
        for city in ALLOWED_CITIES:
            for b in DAY_BUCKETS:
                k = (city, b)
                d = agg_all.get(k)
                if not d or d["n"] == 0:
                    print(f"{city},{b},0,NULL,NULL,NULL")
                    continue
                n = d["n"]
                brier = d["brier"] / n
                logloss = d["logloss"] / n
                auc = auc_roc(d["y"], d["p"])
                auc_str = f"{auc:.4f}" if auc is not None else "NULL"
                print(f"{city},{b},{n},{brier:.5f},{logloss:.5f},{auc_str}")

        print("\nEarliest tradable bucket candidates (ranked)")
        print("city,rank,bucket,n,auc,brier,logloss,confidence_band")
        for city in ALLOWED_CITIES:
            candidates = []
            for b in DAY_BUCKETS:
                d = agg_all.get((city, b))
                if not d or d["n"] == 0:
                    continue
                n = d["n"]
                brier = d["brier"] / n
                logloss = d["logloss"] / n
                auc = auc_roc(d["y"], d["p"])
                if auc is None:
                    continue
                # Composite discovery score: prioritize discrimination + calibration.
                # No fixed threshold; this only ranks.
                score = (0.65 * auc) + (0.20 * (1.0 - min(1.0, brier))) + (0.15 * (1.0 - min(1.0, logloss)))
                # Confidence band around AUC from bootstrap.
                lo, hi = auc_ci_bootstrap(d["y"], d["p"], n_boot=250)
                # Earlier buckets get slight tie-break preference to favor timing.
                bucket_idx = DAY_BUCKETS.index(b)
                candidates.append({
                    "bucket": b,
                    "n": n,
                    "auc": auc,
                    "brier": brier,
                    "logloss": logloss,
                    "score": score - bucket_idx * 1e-4,
                    "ci_lo": lo,
                    "ci_hi": hi,
                })
            candidates.sort(key=lambda x: x["score"], reverse=True)
            if not candidates:
                print(f"{city},NULL,NULL,0,NULL,NULL,NULL,NULL")
                continue
            for i, c in enumerate(candidates[:3], start=1):
                print(
                    f"{city},{i},{c['bucket']},{c['n']},"
                    f"{c['auc']:.4f},{c['brier']:.5f},{c['logloss']:.5f},"
                    f"[{c['ci_lo']:.4f},{c['ci_hi']:.4f}]"
                )

        print("\nGaps (city,bucket,reason,count)")
        if not gap_counts:
            print("none")
        else:
            for (city, b, reason), c in sorted(gap_counts.items()):
                print(f"{city},{b},{reason},{c}")
    finally:
        rl.close()
        session.close()


def auc_roc(y_true: list[float], y_score: list[float]) -> float | None:
    pos = [(s, y) for s, y in zip(y_score, y_true) if y == 1.0]
    neg = [(s, y) for s, y in zip(y_score, y_true) if y == 0.0]
    if not pos or not neg:
        return None
    # Mann-Whitney U formulation
    better = 0.0
    ties = 0.0
    for sp, _ in pos:
        for sn, _ in neg:
            if sp > sn:
                better += 1.0
            elif sp == sn:
                ties += 1.0
    return (better + 0.5 * ties) / (len(pos) * len(neg))


def auc_ci_bootstrap(y_true: list[float], y_score: list[float], n_boot: int = 250) -> tuple[float, float]:
    auc0 = auc_roc(y_true, y_score)
    if auc0 is None or len(y_true) < 8:
        return (0.0, 0.0)
    import random

    vals = []
    idx = list(range(len(y_true)))
    for _ in range(n_boot):
        samp = [random.choice(idx) for _ in idx]
        yt = [y_true[i] for i in samp]
        ys = [y_score[i] for i in samp]
        a = auc_roc(yt, ys)
        if a is not None:
            vals.append(a)
    if not vals:
        return (auc0, auc0)
    vals.sort()
    lo = vals[max(0, int(0.05 * len(vals)) - 1)]
    hi = vals[min(len(vals) - 1, int(0.95 * len(vals)))]
    return (lo, hi)


def main() -> None:
    parser = argparse.ArgumentParser(description="Top-1 trust analysis for precipitation")
    parser.add_argument("--months-back", type=int, default=18)
    args = parser.parse_args()
    run_analysis(args.months_back)


if __name__ == "__main__":
    main()
