#!/usr/bin/env python3
"""
Phase 4 — before/after win-rate validation (READ-ONLY, writes nothing).

Re-grades every settled YES trade_simulation against two ground truths:
  OLD = ERA5 outcomes (from outcomes_pre_iem_backup.csv, the pre-migration snapshot)
  NEW = IEM airport-METAR outcomes (current DB)

Uses the exact win logic from outcome_backfiller.settle_temperature_pnl via the
shared polymarket_dry_run.bucket_contains():
  finite bucket: half-open [lo, hi)  (hi = lo + width; "62-63°F" -> [62,64))
  tail markets:  inclusive bound     ("X or higher" -> actual >= lo)

Caveat: these trades were SELECTED under the old ERA5-biased corrector, so this
isolates the ground-truth-source fix (did we mis-grade wins/losses against the
wrong truth?). The trade-SELECTION improvement from the corrected median corrector
only shows in trades placed from now on.

Usage: python3 validate_winrate_beforeafter.py
"""

import csv
import datetime
from pathlib import Path

from sqlalchemy import text
from database_schema import init_database
from polymarket_dry_run import parse_temp_range, bucket_contains

US = {'new_york', 'atlanta', 'dallas', 'chicago', 'miami', 'seattle', 'austin'}
EXCLUDED = {'hong_kong', 'shenzhen'}   # outcomes still ERA5 even in "new" set
BACKUP = Path(__file__).resolve().parent / 'outcomes_pre_iem_backup.csv'


def load_csv_outcomes(path):
    """{(location_id, date): (max, min)} from the pre-migration CSV snapshot."""
    out = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            d = row['date'][:10]  # 'YYYY-MM-DD ...' -> date
            try:
                mx = float(row['actual_max_temp']) if row['actual_max_temp'] else None
                mn = float(row['actual_min_temp']) if row['actual_min_temp'] else None
            except ValueError:
                mx = mn = None
            out[(row['location_id'], d)] = (mx, mn)
    return out


def won(question, actual, mode):
    lo, hi, width = parse_temp_range(question or '')
    if lo is None and hi is None:
        return None
    # Half-open [lo, lo+width) for finite buckets; inclusive bound for tails.
    return bucket_contains(lo, hi, width, actual)


def main():
    session = init_database()
    old = load_csv_outcomes(BACKUP)

    # New outcomes from DB.
    new = {}
    for loc, dt, mx, mn in session.execute(text(
        "SELECT location_id, date::date, actual_max_temp, actual_min_temp "
        "FROM outcomes WHERE verified = TRUE"
    )):
        new[(loc, str(dt))] = (float(mx) if mx is not None else None,
                               float(mn) if mn is not None else None)

    trades = session.execute(text(
        "SELECT location_id, market_date, question_text, COALESCE(mode,'max') AS mode "
        "FROM trade_simulations "
        "WHERE order_type='YES' AND market_date < CURRENT_DATE"
    )).fetchall()

    # group -> {'old':[wins], 'new':[wins], 'n':int}
    stats = {'US': {'old': 0, 'new': 0, 'n_old': 0, 'n_new': 0},
             'non-US': {'old': 0, 'new': 0, 'n_old': 0, 'n_new': 0},
             'non-US(ex-excl)': {'old': 0, 'new': 0, 'n_old': 0, 'n_new': 0}}

    for loc, mdate, q, mode in trades:
        key = (loc, str(mdate))
        groups = ['US'] if loc in US else ['non-US']
        if loc not in US and loc not in EXCLUDED:
            groups.append('non-US(ex-excl)')

        for src_name, store in (('old', old), ('new', new)):
            o = store.get(key)
            if not o:
                continue
            actual = o[1] if mode == 'min' else o[0]
            if actual is None:
                continue
            w = won(q, actual, mode)
            if w is None:
                continue
            for g in groups:
                stats[g][f'n_{src_name}'] += 1
                if w:
                    stats[g][src_name] += 1

    print("\nPhase 4 — win-rate re-grade: OLD (ERA5) vs NEW (IEM airport-METAR)")
    print("Same placed trades, graded against each ground truth.\n")
    print(f"{'group':<18} {'n_old':>6} {'wr_old':>8} {'n_new':>6} {'wr_new':>8} {'Δ':>7}")
    print("-" * 58)
    for g, s in stats.items():
        wr_old = 100 * s['old'] / s['n_old'] if s['n_old'] else float('nan')
        wr_new = 100 * s['new'] / s['n_new'] if s['n_new'] else float('nan')
        delta = wr_new - wr_old
        print(f"{g:<18} {s['n_old']:>6} {wr_old:>7.1f}% {s['n_new']:>6} {wr_new:>7.1f}% {delta:>+6.1f}")
    print("\nNote: 'non-US(ex-excl)' drops hong_kong/shenzhen (still ERA5 in both sets).")
    print("This isolates the ground-truth fix; trade-selection gains appear only in future trades.\n")


if __name__ == "__main__":
    main()
