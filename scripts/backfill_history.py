#!/usr/bin/env python3
"""
Polymarket Historical Data Backfill Script
Uses pmxt library to backfill historical OHLCV data into local database
"""
import sys
import os
import time
import datetime
import logging
from typing import List, Dict, Any
from dataclasses import dataclass

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json, subprocess, time, os
from pathlib import Path
import pmxt
import sqlite3

LAUNCHER = '/opt/homebrew/lib/node_modules/pmxtjs/node_modules/pmxt-core/bin/pmxt-ensure-server'
LOCK_PATH = Path.home() / '.pmxt' / 'server.lock'

def _ensure_server():
    """Start pmxt server if not running at v2.23.0."""
    if LOCK_PATH.exists():
        lock = json.loads(LOCK_PATH.read_text())
        if lock.get('version') == '2.23.0':
            try:
                os.kill(lock['pid'], 0)
                return
            except OSError:
                pass
        try:
            os.kill(lock['pid'], 15)
            time.sleep(0.5)
        except OSError:
            pass
        LOCK_PATH.unlink(missing_ok=True)

    subprocess.Popen(
        [LAUNCHER],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    for _ in range(30):
        time.sleep(0.2)
        if LOCK_PATH.exists():
            lock = json.loads(LOCK_PATH.read_text())
            if lock.get('version') == '2.23.0':
                return
    raise RuntimeError("pmxt server failed to start")

def get_pm():
    """Return Polymarket client with auto-injected auth token."""
    _ensure_server()
    pm = pmxt.Polymarket()
    lock = json.loads(LOCK_PATH.read_text())
    pm._api_client.default_headers['x-pmxt-access-token'] = lock['accessToken']
    return pm

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class BackfillConfig:
    start_time: datetime.datetime
    end_time: datetime.datetime
    resolution: str = "1h"
    batch_size: int = 50
    sleep_between_requests: float = 0.5
    market_filter: str = "weather"


class HistoricalBackfiller:
    def __init__(self, db_connection_string: str = None):
        """Initialize backfiller with pmxt API and database connection"""
        self.api = get_pm()
        
        # Use SQLite file database for zero setup
        self.db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "polymarket.db")
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        
        # Create table if not exists
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS market_prices (
                market_id TEXT,
                timestamp TIMESTAMP,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                resolution TEXT,
                PRIMARY KEY (market_id, timestamp, resolution)
            )
        """)
        self.conn.commit()
        logger.info("Initialized Historical Backfiller")

    def get_target_markets(self, query_filter: str = "weather") -> List[Any]:
        """Get list of markets to backfill - ✅ CORRECT FILTERED MARKETS"""
        
        # ✅ Proper search terms that find REAL WEATHER MARKETS NOT SPACE WEATHER
        search_terms = [
            'temperature',
            'highest temperature',
            'lowest temperature',
            'celsius',
            'fahrenheit',
            'rainfall',
            'snowfall',
            'tornado',
            'hurricane'
        ]
        
        exclude_terms = ['space weather', 'solar', 'geomagnetic']
        
        all_markets = []
        
        for term in search_terms:
            logger.info(f"Searching for markets matching: {term}")
            events = self.api.fetch_events(query=term)
            
            for event in events:
                for market in event.markets:
                    question = market.question.lower()
                    
                    # Exclude space weather markets entirely
                    if any(exclude in question for exclude in exclude_terms):
                        continue
                    
                    all_markets.append(market)
        
        # Remove duplicates
        unique_markets = {m.market_id: m for m in all_markets}.values()
        
        logger.info(f"Total unique valid weather markets found: {len(unique_markets)}")
        return list(unique_markets)

    def backfill_market(self, market, resolution: str = "1h") -> int:
        """Backfill single market historical data"""
        market_id = market.market_id
        question = market.question[:60]
        
        logger.info(f"Backfilling market: {question} ({market_id})")
        
        try:
            # Fetch historical OHLCV data with 7 day max window
            history = self.api.fetch_ohlcv(
                market.yes.outcome_id,
                resolution=resolution,
                limit=168  # 7 days × 24h = 168 hours maximum allowed
            )
            
            if not history:
                logger.warning(f"No historical data for {market_id}")
                return 0

            # Prepare rows for batch insert
            rows = []
            for candle in history:
                rows.append((
                    market_id,
                    datetime.datetime.fromtimestamp(candle.timestamp / 1000),
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    resolution
                ))

            # Bulk insert (SQLite syntax)
            insert_sql = """
                INSERT OR IGNORE INTO market_prices 
                (market_id, timestamp, open, high, low, close, volume, resolution)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """
            
            self.cursor.executemany(insert_sql, rows)
            self.conn.commit()
            
            logger.info(f"Inserted {len(rows)} candles for {question}")
            return len(rows)
            
        except Exception as e:
            logger.error(f"Failed to backfill {market_id}: {str(e)}")
            self.conn.rollback()
            return 0

    def run_backfill(self, config: BackfillConfig):
        """Run full backfill process"""
        logger.info(f"Starting historical backfill for {config.market_filter} markets")
        logger.info(f"Time range: {config.start_time} -> {config.end_time}")
        logger.info(f"Resolution: {config.resolution}")
        
        markets = self.get_target_markets(config.market_filter)
        
        total_candles = 0
        total_markets = len(markets)
        
        for idx, market in enumerate(markets, 1):
            logger.info(f"Processing market {idx}/{total_markets}")
            
            count = self.backfill_market(market, config.resolution)
            total_candles += count
            
            time.sleep(config.sleep_between_requests)
        
        logger.info("=" * 60)
        logger.info(f"BACKFILL COMPLETE")
        logger.info(f"Total markets processed: {total_markets}")
        logger.info(f"Total candles inserted: {total_candles}")
        logger.info("=" * 60)

    def close(self):
        """Cleanup connections"""
        self.cursor.close()
        self.conn.close()


if __name__ == "__main__":
    config = BackfillConfig(
        start_time=datetime.datetime.now() - datetime.timedelta(days=90),
        end_time=datetime.datetime.now(),
        resolution="1h",
        market_filter="weather",
        sleep_between_requests=0.3
    )
    
    try:
        backfiller = HistoricalBackfiller()
        backfiller.run_backfill(config)
    except KeyboardInterrupt:
        logger.info("Backfill interrupted by user")
        sys.exit(0)
    finally:
        if 'backfiller' in locals():
            backfiller.close()