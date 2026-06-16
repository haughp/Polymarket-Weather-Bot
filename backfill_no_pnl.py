#!/usr/bin/env python3
"""
One-off backfill: settle the 903 orphaned ECMWF dry-run NO trades from May 3-9.

WHY THIS EXISTS
---------------
The simulator generated order_type='NO' trades only during 2026-05-03..05-09,
then switched to YES-only. The live settler `outcome_backfiller.settle_temperature_pnl()`
filters `order_type == 'YES'`, so it has no code path for those NO trades — they
have sat with simulated_pnl IS NULL ever since and show as PENDING on the dashboard
despite their market_date being long past and verified actuals existing.

NO is the exact complement of YES. We reuse the SAME helpers the live settler uses
(parse_temp_range, bucket_contains) so the scoring is identical — only the win
condition is negated. This script does NOT modify the daemon or the settler; it is
a standalone, idempotent, read-then-write tool.

USAGE
-----
    python3 backfill_no_pnl.py            # DRY-RUN: print what would settle, write nothing
    python3 backfill_no_pnl.py --commit   # actually write simulated_pnl

Idempotent: only touches rows where order_type='NO' AND simulated_pnl IS NULL.
"""

import sys
import datetime as _dt
from sqlalchemy import func

from polymarket_dry_run import parse_temp_range, bucket_contains
from database_schema import TradeSimulation, Outcome, init_database


def main(commit: bool) -> None:
    session = init_database()
    today = _dt.date.today()

    pending = (
        session.query(TradeSimulation)
        .filter(
            TradeSimulation.order_type == 'NO',
            TradeSimulation.simulated_pnl.is_(None),
            TradeSimulation.market_date < today,
        )
        .all()
    )

    print(f"{'COMMIT' if commit else 'DRY-RUN'}: {len(pending)} unsettled NO trade(s) found\n")
    if not pending:
        session.close()
        return

    settled = wins = losses = skipped = 0
    pnl_sum = 0.0

    for trade in pending:
        outcome = (
            session.query(Outcome)
            .filter(
                Outcome.location_id == trade.location_id,
                func.date(Outcome.date) == trade.market_date,
                Outcome.verified == True,  # noqa: E712
            )
            .first()
        )
        if not outcome or outcome.actual_max_temp is None:
            skipped += 1
            continue

        lo, hi, width = parse_temp_range(trade.question_text or '')
        if lo is None and hi is None:
            skipped += 1
            continue

        if getattr(trade, 'mode', 'max') == 'min':
            if outcome.actual_min_temp is None:
                skipped += 1
                continue
            actual = float(outcome.actual_min_temp)
        else:
            actual = float(outcome.actual_max_temp)

        # NO wins exactly when the YES side would have LOST the bucket.
        yes_won = bucket_contains(lo, hi, width, actual)
        no_won = not yes_won

        size = float(trade.size)
        price = float(trade.price)
        pnl = round(size / price - size, 2) if no_won else round(-size, 2)

        if commit:
            trade.simulated_pnl = pnl

        settled += 1
        pnl_sum += pnl
        if no_won:
            wins += 1
        else:
            losses += 1

        print(
            f"  {'WRITE' if commit else 'WOULD'} NO: {trade.location_id:12s} "
            f"{trade.market_date} | actual={actual:.1f}° | "
            f"bucket=[{lo},{hi}) w={width} | "
            f"{'WIN' if no_won else 'LOSS'} | pnl=${pnl}"
        )

    if commit:
        session.commit()

    print(
        f"\n{'COMMITTED' if commit else 'DRY-RUN (no writes)'}: "
        f"settled={settled}  wins={wins}  losses={losses}  "
        f"skipped(no outcome)={skipped}  net_pnl=${round(pnl_sum, 2)}"
    )
    session.close()


if __name__ == "__main__":
    main(commit="--commit" in sys.argv[1:])
