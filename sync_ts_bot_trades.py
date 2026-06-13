#!/usr/bin/env python3
"""
Sync TS Weather Bot trades from simulation.json to database.
Run this periodically (e.g., every 5 minutes) to keep trade history updated.
"""

import json
import sys
from pathlib import Path
from datetime import datetime
from decimal import Decimal

# Add sniff_test_polymarket to path for .env
sys.path.insert(0, str(Path.home() / "sniff_test_polymarket"))

from dotenv import load_dotenv
load_dotenv(Path.home() / "sniff_test_polymarket" / ".env")

from database_schema import init_database, WeatherBotTsPosition, WeatherBotTsTradeHistory, WeatherBotSignalSnapshot


SNAPSHOTS_JSONL  = Path(__file__).parent / "snapshots.jsonl"
SNAPSHOTS_CURSOR = Path(__file__).parent / "snapshots_cursor.txt"


def sync_snapshots(session) -> int:
    """Cursor-based sync of snapshots.jsonl → weatherbot_signal_snapshots.
    Only processes lines beyond the last known byte offset, so re-runs are O(new lines)."""
    if not SNAPSHOTS_JSONL.exists():
        return 0
    cursor = int(SNAPSHOTS_CURSOR.read_text().strip()) if SNAPSHOTS_CURSOR.exists() else 0
    new_count = 0
    with open(SNAPSHOTS_JSONL, "r") as f:
        f.seek(cursor)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snap = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = snap.get("snapshot_key")
            if not key:
                continue
            if session.query(WeatherBotSignalSnapshot).filter_by(snapshot_key=key).first():
                continue
            session.add(WeatherBotSignalSnapshot(
                snapshot_key  = key,
                snapped_at    = snap.get("snapped_at"),
                city          = snap.get("city"),
                mode          = snap.get("mode"),
                market_date   = snap.get("market_date"),
                hours_to_peak = Decimal(str(snap["hours_to_peak"])) if snap.get("hours_to_peak") is not None else None,
                nws_forecast  = Decimal(str(snap["nws_forecast"])) if snap.get("nws_forecast") is not None else None,
                adj_forecast  = Decimal(str(snap["adj_forecast"]))  if snap.get("adj_forecast")  is not None else None,
                bucket1_range = snap.get("bucket1_range"),
                bucket1_price = Decimal(str(snap["bucket1_price"])) if snap.get("bucket1_price") is not None else None,
                bucket2_range = snap.get("bucket2_range"),
                bucket2_price = Decimal(str(snap["bucket2_price"])) if snap.get("bucket2_price") is not None else None,
                entered       = snap.get("entered", False),
                status        = snap.get("status"),
                would_enter   = snap.get("would_enter"),
            ))
            new_count += 1
        new_cursor = f.tell()
    session.commit()
    SNAPSHOTS_CURSOR.write_text(str(new_cursor))
    return new_count


def sync_ts_bot_trades():
    """Load simulation.json and sync open positions + closed trades to database."""
    sim_path = Path(__file__).parent / "simulation.json"

    if not sim_path.exists():
        print(f"⚠️  simulation.json not found at {sim_path}")
        return 0, 0

    try:
        sim_data = json.loads(sim_path.read_text())
    except Exception as e:
        print(f"❌ Failed to parse simulation.json: {e}")
        return 0, 0

    session = init_database()
    updated_positions = 0
    new_trades = 0

    try:
        # 1. Sync open positions.  simulation.json["positions"] is keyed by market_id
        # (e.g. "2247217"); the real on-chain ERC-1155 token_id lives at pos["token_id"].
        # Historically this loop used the dict key (the market_id) as clob_token_id which
        # broke every on-chain cross-check downstream.
        positions = sim_data.get("positions", {})
        current_token_ids: set[str] = set()
        for market_id, pos in positions.items():
            token_id = str(pos.get("token_id") or "").strip()
            if not token_id:
                # Position lacks a real CTF token_id (older entries before the TS bot started
                # storing it).  Skip — there's nothing useful to write.
                continue
            current_token_ids.add(token_id)

            existing = session.query(WeatherBotTsPosition).filter_by(
                clob_token_id=token_id
            ).first()

            if existing:
                # Update existing position
                existing.current_price = pos.get("current_price")
                existing.current_pnl = pos.get("pnl")
                existing.updated_at = datetime.utcnow()
                updated_positions += 1
            else:
                # Create new position
                new_pos = WeatherBotTsPosition(
                    clob_token_id=token_id,
                    question=pos.get("question", ""),
                    location=pos.get("location", ""),
                    market_date=pos.get("date"),
                    entry_price=Decimal(str(pos.get("entry_price", 0))),
                    entry_cost=Decimal(str(pos.get("cost", 0))),
                    shares=Decimal(str(pos.get("shares", 0))),
                    current_price=Decimal(str(pos.get("current_price", pos.get("entry_price", 0)))),
                    current_pnl=Decimal(str(pos.get("pnl", 0))),
                    opened_at=pos.get("opened_at"),
                    forecast_temp=Decimal(str(pos.get("forecast_temp", 0))),
                    kelly_pct=Decimal(str(pos.get("kelly_pct", 0))) if pos.get("kelly_pct") else None,
                    ev=Decimal(str(pos.get("ev", 0))) if pos.get("ev") else None,
                    our_prob=Decimal(str(pos.get("our_prob", 0))) if pos.get("our_prob") else None,
                )
                session.add(new_pos)
                updated_positions += 1

        # Remove rows whose position is no longer in simulation.json — these were resolved
        # by the TS bot (strategy.ts delete positions[mid]) and the mirror needs to follow.
        deleted_positions = 0
        if current_token_ids:
            deleted_positions = session.query(WeatherBotTsPosition).filter(
                ~WeatherBotTsPosition.clob_token_id.in_(current_token_ids)
            ).delete(synchronize_session=False)
        else:
            # No current positions at all → simulation.json is empty.  Clear the mirror.
            deleted_positions = session.query(WeatherBotTsPosition).delete(synchronize_session=False)

        # 2. Parse closed trades — merge live trades + full archive so historical
        # exits (stored in trades_archive post-reset) are also backfilled.
        # The filter_by(closed_at, question) guard below prevents double-inserts.
        trades = sim_data.get("trades", []) + sim_data.get("trades_archive", [])
        for trade in trades:
            if trade.get("type") == "exit" and trade.get("closed_at"):
                # Check if this closed trade is already recorded
                existing = session.query(WeatherBotTsTradeHistory).filter_by(
                    closed_at=trade.get("closed_at"),
                    question=trade.get("question")
                ).first()

                if not existing:
                    new_trade = WeatherBotTsTradeHistory(
                        clob_token_id=trade.get("token_id", ""),
                        question=trade.get("question", ""),
                        location=trade.get("location", ""),
                        market_date=trade.get("date"),
                        entry_price=Decimal(str(trade.get("entry_price", 0))),
                        entry_cost=Decimal(str(trade.get("cost", 0))),
                        shares=Decimal(str(trade.get("shares", 0))),
                        exit_price=Decimal(str(trade.get("exit_price", 0))),
                        realized_pnl=Decimal(str(trade.get("pnl", 0))),
                        opened_at=trade.get("opened_at"),
                        closed_at=trade.get("closed_at"),
                        resolved_outcome=trade.get("resolved_outcome"),
                        kelly_pct=Decimal(str(trade.get("kelly_pct", 0))) if trade.get("kelly_pct") else None,
                        ev=Decimal(str(trade.get("ev", 0))) if trade.get("ev") else None,
                        our_prob=Decimal(str(trade.get("our_prob", 0))) if trade.get("our_prob") else None,
                        forecast_temp=Decimal(str(trade.get("forecast_temp", 0))),
                    )
                    session.add(new_trade)
                    new_trades += 1

        session.commit()
        snap_count = sync_snapshots(session)
        print(f"✅ Synced TS bot trades: {updated_positions} positions, "
              f"{deleted_positions} stale removed, {new_trades} closed trades, {snap_count} snapshots")
        return updated_positions, new_trades

    except Exception as e:
        session.rollback()
        print(f"❌ Sync failed: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    sync_ts_bot_trades()
