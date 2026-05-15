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

from database_schema import init_database, WeatherBotTsPosition, WeatherBotTsTradeHistory


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
        # 1. Sync open positions
        positions = sim_data.get("positions", {})
        for token_id, pos in positions.items():
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

        # 2. Parse closed trades from trades array
        # (if TS bot tracks closed trades in simulation.json)
        trades = sim_data.get("trades", [])
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
        print(f"✅ Synced TS bot trades: {updated_positions} positions, {new_trades} closed trades")
        return updated_positions, new_trades

    except Exception as e:
        session.rollback()
        print(f"❌ Sync failed: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    sync_ts_bot_trades()
