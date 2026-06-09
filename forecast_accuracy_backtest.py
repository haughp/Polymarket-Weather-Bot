#!/usr/bin/env python3
"""
Forecast source accuracy gate for the ECMWF dry-run pipeline.

For each (location_id, mode, source) combination, replays the bot's actual
top-2 bucket-selection logic (classify_markets) against all resolved Polymarket
markets over the last 30 trading days where both a forecast and a verified
outcome exist in the DB.

"Hit" = at least one of the two selected buckets resolved YES (matching the
live bot's dual-leg strategy).  Eligible threshold = hits >= 15 out of 30 days.

Results are persisted to public.ecmwf_forecast_accuracy (created if absent).
Re-runnable: each run appends one row per (location_id, mode, source) with the
current window_end date; existing rows for earlier window_ends are kept.

Usage:
    python forecast_accuracy_backtest.py              # run & persist
    python forecast_accuracy_backtest.py --report     # print latest results only
    python forecast_accuracy_backtest.py --days 14    # shorter window (debug)
    python forecast_accuracy_backtest.py --location paris --mode max  # single city
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from collections import defaultdict

import httpx
from sqlalchemy import text

from database_schema import init_database
from polymarket_dry_run import MONTHS, LOCATION_SLUGS, parse_temp_range, classify_markets

GAMMA_API = "https://gamma-api.polymarket.com"
WINDOW_DAYS = 30
ELIGIBLE_HITS = 15


# ── DB helpers ─────────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.ecmwf_forecast_accuracy (
    id            SERIAL PRIMARY KEY,
    location_id   VARCHAR(32) NOT NULL,
    mode          VARCHAR(16) NOT NULL,
    source        VARCHAR(32) NOT NULL,
    window_end    DATE        NOT NULL,
    days_tested   INTEGER     NOT NULL,
    hits          INTEGER     NOT NULL,
    hit_rate      NUMERIC(5,3) NOT NULL,
    eligible      BOOLEAN     NOT NULL,
    replay_mode   VARCHAR(16) NOT NULL DEFAULT 'bucket_replay',
    created_at    TIMESTAMP   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_efa_location_mode_source
    ON public.ecmwf_forecast_accuracy (location_id, mode, source);
CREATE INDEX IF NOT EXISTS ix_efa_window_end
    ON public.ecmwf_forecast_accuracy (window_end);
"""


def ensure_table(session) -> None:
    with session.bind.connect() as conn:
        conn.execute(text(CREATE_TABLE_SQL))
        conn.commit()


def load_forecast_history(session, days: int, location_id: str | None, mode: str | None) -> list[dict]:
    """
    Pull the last `days` days of (location_id, mode, source, forecast_temp,
    target_date) from the forecasts table, joined with verified outcomes.

    Uses the same latest_per_date CTE shape as _load_live_bias() —
    one forecast row per (location_id, mode, source, DATE(peak_time)).
    """
    where_clauses = ["o.verified = TRUE", "f.source IS NOT NULL"]
    if location_id:
        where_clauses.append(f"f.location_id = '{location_id}'")
    if mode:
        where_clauses.append(f"f.mode = '{mode}'")
    where_sql = " AND ".join(where_clauses)

    sql = f"""
    WITH latest_per_date AS (
        SELECT DISTINCT ON (location_id, mode, source, DATE(peak_time))
               location_id,
               mode,
               source,
               DATE(peak_time)  AS target_date,
               forecast_temp
          FROM public.forecasts
         WHERE peak_time IS NOT NULL
           AND source IS NOT NULL
           AND DATE(peak_time) >= (CURRENT_DATE - INTERVAL '{days} days')
           AND DATE(peak_time) <  CURRENT_DATE
      ORDER BY location_id, mode, source, DATE(peak_time), created_at DESC
    )
    SELECT
        f.location_id,
        f.mode,
        f.source,
        f.target_date,
        f.forecast_temp,
        o.actual_max_temp,
        o.actual_min_temp
    FROM latest_per_date f
    JOIN public.outcomes o
      ON o.location_id = f.location_id
     AND o.date        = f.target_date
     AND {where_sql}
    ORDER BY f.location_id, f.mode, f.source, f.target_date
    """
    with session.bind.connect() as conn:
        rows = conn.execute(text(sql)).fetchall()
    return [dict(r._mapping) for r in rows]


def persist_results(session, rows: list[dict]) -> None:
    with session.bind.connect() as conn:
        for r in rows:
            conn.execute(text("""
                INSERT INTO public.ecmwf_forecast_accuracy
                    (location_id, mode, source, window_end, days_tested,
                     hits, hit_rate, eligible, replay_mode)
                VALUES
                    (:location_id, :mode, :source, :window_end, :days_tested,
                     :hits, :hit_rate, :eligible, 'bucket_replay')
            """), r)
        conn.commit()


# ── Gamma API helpers (pattern from temp_forecast_backtest.py) ─────────────────

def _fetch_gamma_slug(client: httpx.Client, slug: str) -> list[dict]:
    for attempt in range(3):
        try:
            r = client.get(f"{GAMMA_API}/events", params={"slug": slug})
            if r.status_code == 429:
                time.sleep(min(30.0, 4 ** attempt))
                continue
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            if attempt == 2:
                return []
            time.sleep(min(5.0, 2 ** attempt))
    return []


def fetch_markets_for_date(client: httpx.Client, location_id: str, target_date: datetime.date, mode: str) -> list[dict]:
    """Return resolved bucket markets for (location_id, mode, target_date) via Gamma."""
    slug_city = LOCATION_SLUGS.get(location_id, location_id.replace("_", "-"))
    month_name = MONTHS[target_date.month - 1]
    prefix = "highest" if mode == "max" else "lowest"
    slug = f"{prefix}-temperature-in-{slug_city}-on-{month_name}-{target_date.day}-{target_date.year}"
    events = _fetch_gamma_slug(client, slug)
    if not events:
        return []
    return events[0].get("markets", [])


# ── Core replay logic ──────────────────────────────────────────────────────────

def compute_hit(markets: list[dict], forecast_temp: float, actual_temp: float) -> bool | None:
    """
    Replay classify_markets(top2) and test whether actual_temp lands in
    at least one of the two selected buckets.

    Returns True (hit), False (miss), or None if top2 couldn't be determined
    (no parseable markets, or <2 markets available).
    """
    if not markets:
        return None

    result = classify_markets(markets, forecast_temp)
    top2 = result["top2"]
    if not top2:
        return None

    for cand in top2:
        lo, hi = cand["range"]
        lo_bound = lo if lo is not None else float("-inf")
        hi_bound = hi if hi is not None else float("inf")
        if lo_bound <= actual_temp <= hi_bound:
            return True
    return False


def run_backtest(
    session,
    days: int,
    location_id: str | None,
    mode: str | None,
) -> list[dict]:
    """
    Full replay for all (location_id, mode, source) groups in the DB window.
    Returns a list of result dicts ready for printing / persisting.
    """
    print(f"Loading forecast+outcome history ({days}-day window)...")
    history = load_forecast_history(session, days, location_id, mode)
    if not history:
        print("No joined forecast+outcome rows found — check DB data.")
        return []

    # Group by (location_id, mode, source)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in history:
        key = (row["location_id"], row["mode"], row["source"])
        groups[key].append(row)

    total_keys = len(groups)
    print(f"Replaying {total_keys} (city, mode, source) combinations across {len(history)} day-rows...")

    window_end = datetime.date.today() - datetime.timedelta(days=1)
    results: list[dict] = []

    with httpx.Client(timeout=15) as client:
        for idx, ((loc_id, m, src), day_rows) in enumerate(sorted(groups.items()), 1):
            hits = 0
            tested = 0
            skipped = 0

            for row in day_rows:
                actual_temp = float(row["actual_max_temp"] if m == "max" else row["actual_min_temp"])
                forecast_temp = float(row["forecast_temp"])
                target_date = row["target_date"]
                if isinstance(target_date, str):
                    target_date = datetime.date.fromisoformat(target_date)

                markets = fetch_markets_for_date(client, loc_id, target_date, m)
                hit = compute_hit(markets, forecast_temp, actual_temp)

                if hit is None:
                    skipped += 1
                else:
                    tested += 1
                    if hit:
                        hits += 1

                time.sleep(0.08)  # rate-limit Gamma

            if tested == 0:
                print(f"  [{idx}/{total_keys}] {loc_id}/{m}/{src}: skipped all {len(day_rows)} days (no Gamma data)")
                continue

            hit_rate = round(hits / tested, 3)
            eligible = hits >= ELIGIBLE_HITS
            status = "ELIGIBLE" if eligible else "BELOW GATE"

            print(
                f"  [{idx}/{total_keys}] {loc_id:<12} {m:<4} {src:<12} "
                f"{hits:>2}/{tested:<2} days ({100*hit_rate:.1f}%)  {status}"
                + (f"  [{skipped} days skipped — no Gamma markets]" if skipped else "")
            )

            results.append({
                "location_id": loc_id,
                "mode":        m,
                "source":      src,
                "window_end":  window_end,
                "days_tested": tested,
                "hits":        hits,
                "hit_rate":    hit_rate,
                "eligible":    eligible,
            })

    return results


def print_report(session) -> None:
    """Print the latest accuracy window from the DB."""
    sql = """
    SELECT
        location_id, mode, source,
        window_end, days_tested, hits, hit_rate, eligible, created_at
    FROM public.ecmwf_forecast_accuracy
    WHERE window_end = (SELECT max(window_end) FROM public.ecmwf_forecast_accuracy)
    ORDER BY eligible, hit_rate, location_id, mode
    """
    with session.bind.connect() as conn:
        rows = conn.execute(text(sql)).fetchall()

    if not rows:
        print("No rows in ecmwf_forecast_accuracy — run without --report first.")
        return

    window_end = rows[0][3]
    print(f"\n{'='*80}")
    print(f"  ECMWF FORECAST SOURCE ACCURACY GATE — window_end {window_end}")
    print(f"  Eligible threshold: >= {ELIGIBLE_HITS} hits / {WINDOW_DAYS} days")
    print(f"{'='*80}")
    print(f"  {'City':<14} {'Mode':<5} {'Source':<12} {'Hits':>6} {'Days':>5}  {'WR%':>6}  Status")
    print(f"  {'-'*14} {'-'*5} {'-'*12} {'-'*6} {'-'*5}  {'-'*6}  {'-'*12}")

    ineligible = []
    for r in rows:
        loc, m, src, wend, days, hits, hr, elig, _ = r
        status = "ELIGIBLE" if elig else "BELOW GATE ⚠"
        print(f"  {loc:<14} {m:<5} {src:<12} {hits:>6} {days:>5}  {100*float(hr):>5.1f}%  {status}")
        if not elig:
            ineligible.append((loc, m, src, hits, days))

    if ineligible:
        print(f"\n  RECOMMENDATION — consider replacing forecast source for:")
        for loc, m, src, hits, days in ineligible:
            print(f"    {loc} ({m}, {src}): only {hits}/{days} days hit — research alternative API")
    else:
        print(f"\n  All cities/sources eligible — no action needed.")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="ECMWF forecast source accuracy gate")
    parser.add_argument("--report",   action="store_true", help="Print latest DB results only (no replay)")
    parser.add_argument("--days",     type=int, default=WINDOW_DAYS, metavar="N", help=f"Lookback window (default {WINDOW_DAYS})")
    parser.add_argument("--location", type=str, default=None, metavar="CITY",    help="Limit to one city (e.g. paris)")
    parser.add_argument("--mode",     type=str, default=None, choices=["max","min"], help="Limit to max or min")
    parser.add_argument("--no-persist", action="store_true", help="Run replay but don't write to DB")
    args = parser.parse_args()

    session = init_database(auto_migrate=True)
    ensure_table(session)

    if args.report:
        print_report(session)
        session.close()
        return

    results = run_backtest(session, args.days, args.location, args.mode)

    if results:
        if args.no_persist:
            print(f"\n--no-persist: skipping DB write ({len(results)} rows)")
        else:
            persist_results(session, results)
            print(f"\nPersisted {len(results)} rows to ecmwf_forecast_accuracy.")
        print_report(session)

    session.close()


if __name__ == "__main__":
    main()
