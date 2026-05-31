#!/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -u
"""
ECMWF Weather Pipeline Loop Runner
Runs ecmwf_forecast_pipeline + polymarket_dry_run every 6 hours aligned to
ECMWF analysis times (00/06/12/18z). Data is typically available ~4.5h after
each analysis, so we target the next 6h boundary + 4h30m offset.

Must be invoked with the framework Python which has ecmwf/xarray/cfgrib:
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 run_ecmwf_loop.py
"""

import os
import sys
import time
import datetime
import logging

# Must run from the Weather Bot directory so relative imports resolve correctly.
WEATHER_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(WEATHER_BOT_DIR)
if WEATHER_BOT_DIR not in sys.path:
    sys.path.insert(0, WEATHER_BOT_DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def _next_ecmwf_utc() -> datetime.datetime:
    """UTC datetime of the next ECMWF data availability window (04:30, 10:30, 16:30, 22:30)."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    for target_hour in [4, 10, 16, 22]:
        target = now.replace(hour=target_hour, minute=30, second=0, microsecond=0)
        if target > now:
            return target

    return (now + datetime.timedelta(days=1)).replace(
        hour=4, minute=30, second=0, microsecond=0
    )


def _run_pipeline(full_refresh: bool = True) -> bool:
    import importlib
    log.info("=" * 60)
    log.info("Pipeline run starting  [%s]", "FULL" if full_refresh else "RETRY")
    log.info("=" * 60)

    if full_refresh:
        try:
            import ecmwf_forecast_pipeline
            importlib.reload(ecmwf_forecast_pipeline)
            ecmwf_forecast_pipeline.main()
            log.info("Stage 1 ✅  ECMWF forecast pipeline complete")
        except Exception as exc:
            log.error("Stage 1 ❌  ECMWF forecast pipeline failed: %s", exc)
            return False

    try:
        import polymarket_dry_run
        importlib.reload(polymarket_dry_run)
        polymarket_dry_run.main(pending_only=not full_refresh)
        log.info("Stage 2 ✅  Temperature YES scan complete")
    except Exception as exc:
        log.error("Stage 2 ❌  Temperature YES scan failed: %s", exc)

    if full_refresh:
        try:
            import precip_forecast_pipeline
            importlib.reload(precip_forecast_pipeline)
            precip_forecast_pipeline.main()
            log.info("Stage 3a ✅  Precipitation forecast pipeline complete")
        except Exception as exc:
            log.error("Stage 3a ❌  Precipitation forecast pipeline failed: %s", exc)

        try:
            import polymarket_precip_dry_run
            importlib.reload(polymarket_precip_dry_run)
            polymarket_precip_dry_run.main()
            log.info("Stage 3b ✅  Precipitation Polymarket dry run complete")
        except Exception as exc:
            log.error("Stage 3b ❌  Precipitation dry run failed: %s", exc)

        try:
            import outcome_backfiller
            importlib.reload(outcome_backfiller)
            outcome_backfiller.backfill_outcomes(days_back=7)
            outcome_backfiller.backfill_precip_outcomes(months_back=3)
            log.info("Stage 4 ✅  Outcome backfiller complete")
        except Exception as exc:
            log.error("Stage 4 ❌  Outcome backfiller failed: %s", exc)

        try:
            import outcome_backfiller
            outcome_backfiller.settle_temperature_pnl()
            log.info("Stage 5 ✅  Temperature PnL settlement complete")
        except Exception as exc:
            log.error("Stage 5 ❌  PnL settlement failed: %s", exc)

    return True


def show_recent() -> None:
    from database_schema import init_database, TradeSimulation, PrecipTradeSimulation, PrecipStrategyTrade
    session = init_database()
    
    # 1. Temperature Trades
    temp_trades = (
        session.query(TradeSimulation)
        .order_by(TradeSimulation.timestamp.desc())
        .limit(100)
        .all()
    )
    if not temp_trades:
        print("No recent temperature trade simulations found in database.")
    else:
        print("\n" + "="*120)
        print("  TEMPERATURE EXECUTOR HISTORY (Recent 100)")
        print("="*120)
        print(f"  {'ID':>5} | {'Date':>19} | {'City':>12} | {'Mode':>4} | {'Mkt Date':>10} | {'Type':>4} | {'Price':>6} | {'Size':>6} | {'PnL':>8} | {'Question'}")
        print("-" * 120)
        for t in temp_trades:
            date_str = t.timestamp.strftime('%Y-%m-%d %H:%M:%S') if t.timestamp else "unknown"
            mkt_date = str(t.market_date) if t.market_date else "unknown"
            price = float(t.price) if t.price is not None else 0.0
            pnl_str = f"${float(t.simulated_pnl):+.2f}" if t.simulated_pnl is not None else "pending"
            q_trunc = t.question_text[:35] + "..." if t.question_text and len(t.question_text) > 35 else (t.question_text or "")
            print(f"  {t.id:>5} | {date_str} | {t.location_id:>12} | {getattr(t, 'mode', 'max').upper():>4} | {mkt_date:>10} | {t.order_type:>4} | {price:>6.3f} | ${float(t.size):>5.2f} | {pnl_str:>8} | {q_trunc}")
        print("=" * 120 + "\n")

    # 2. Precip Dry Run Trades
    precip_trades = (
        session.query(PrecipTradeSimulation)
        .order_by(PrecipTradeSimulation.timestamp.desc())
        .limit(100)
        .all()
    )
    if not precip_trades:
        print("No recent precipitation trade simulations found in database.")
    else:
        print("\n" + "="*120)
        print("  PRECIPITATION EXECUTOR HISTORY (Recent 100)")
        print("="*120)
        print(f"  {'ID':>5} | {'Date':>19} | {'City':>12} | {'Mkt Date':>10} | {'Type':>4} | {'Price':>6} | {'Size':>6} | {'PnL':>8} | {'Question'}")
        print("-" * 120)
        for t in precip_trades:
            date_str = t.timestamp.strftime('%Y-%m-%d %H:%M:%S') if t.timestamp else "unknown"
            mkt_date = str(t.settlement_month) if t.settlement_month else "unknown"
            price = float(t.price) if t.price is not None else 0.0
            pnl_str = f"${float(t.simulated_pnl):+.2f}" if t.simulated_pnl is not None else "pending"
            q_trunc = t.question_text[:35] + "..." if t.question_text and len(t.question_text) > 35 else (t.question_text or "")
            print(f"  {t.id:>5} | {date_str} | {t.location_id:>12} | {mkt_date:>10} | {t.order_type:>4} | {price:>6.3f} | ${float(t.size):>5.2f} | {pnl_str:>8} | {q_trunc}")
        print("=" * 120 + "\n")
        
    # 3. Precip Live Trades
    live_precip = (
        session.query(PrecipStrategyTrade)
        .filter(PrecipStrategyTrade.mode == "forward_yes_v1")
        .order_by(PrecipStrategyTrade.timestamp.desc())
        .limit(100)
        .all()
    )
    if live_precip:
        print("\n" + "="*120)
        print("  PRECIPITATION V1 LIVE/FWD TRADES (Recent 100)")
        print("="*120)
        print(f"  {'ID':>5} | {'Date':>19} | {'City':>12} | {'Mkt Date':>10} | {'Type':>4} | {'Price':>6} | {'Size':>6} | {'PnL':>8} | {'Question'}")
        print("-" * 120)
        for t in live_precip:
            date_str = t.timestamp.strftime('%Y-%m-%d %H:%M:%S') if t.timestamp else "unknown"
            mkt_date = str(t.settlement_month) if t.settlement_month else "unknown"
            price = float(t.fill_price) if t.fill_price is not None else float(t.limit_price or 0.0)
            pnl_str = f"${float(t.realized_pnl):+.2f}" if t.realized_pnl is not None else "pending"
            q_trunc = t.question_text[:35] + "..." if t.question_text and len(t.question_text) > 35 else (t.question_text or "")
            print(f"  {t.id:>5} | {date_str} | {t.location_id:>12} | {mkt_date:>10} | {t.side:>4} | {price:>6.3f} | ${float(t.size_usd):>5.2f} | {pnl_str:>8} | {q_trunc}")
        print("=" * 120 + "\n")

    session.close()


def main() -> None:
    log.info("ECMWF Weather Pipeline Loop Runner started")
    log.info("Working directory: %s", WEATHER_BOT_DIR)

    _run_pipeline(full_refresh=True)
    next_ecmwf = _next_ecmwf_utc()

    while True:
        try:
            log.info("─── Loop tick — checking wake schedule ───")
            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            secs_to_ecmwf = (next_ecmwf - now).total_seconds()
            sleep_secs = min(1800, max(60, secs_to_ecmwf))

            next_wake = now + datetime.timedelta(seconds=sleep_secs)
            log.info(
                "Next wake in %.1fh at %s UTC  (next ECMWF refresh at %s UTC)",
                sleep_secs / 3600,
                next_wake.strftime("%Y-%m-%d %H:%M"),
                next_ecmwf.strftime("%Y-%m-%d %H:%M"),
            )
            time.sleep(sleep_secs)

            now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            do_full = now >= next_ecmwf
            _run_pipeline(full_refresh=do_full)
            if do_full:
                next_ecmwf = _next_ecmwf_utc()

        except Exception:
            log.exception("Loop body crashed — sleeping 60s before retry")
            time.sleep(60)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="Show recent execution history and exit")
    args, _ = ap.parse_known_args()
    
    if args.status:
        show_recent()
    else:
        main()