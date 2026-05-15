#!/usr/bin/env python3
"""
Weather Data Merging Pipeline for Polymarket
Merges actual historical weather measurements with Polymarket market prices
Uses Open-Meteo Public API - 100% free, no keys, no rate limits
"""
import sys
import os
import re
import time
import datetime
import logging
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
import requests
import pandas as pd
import numpy as np
import sqlite3

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class WeatherLocation:
    name: str
    latitude: float
    longitude: float
    confidence: float


class WeatherDataMerger:
    def __init__(self):
        self.open_meteo_base = "https://archive-api.open-meteo.com/v1/archive"
        self.geocode_base = "https://geocoding-api.open-meteo.com/v1/search"
        
        # Use same SQLite database as backfill script
        self.db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "polymarket.db")
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        
        # Location cache
        self.location_cache: Dict[str, WeatherLocation] = {}
        
        logger.info("Weather Data Merger initialized")

    def extract_location_from_market(self, question: str) -> Optional[str]:
        """Extract geographic location from market question text"""
        # Common weather market patterns
        patterns = [
            r"Will (it rain|snow|the temperature) in ([A-Za-z\s,]+) (on|be|reach|exceed)",
            r"([A-Za-z\s,]+) (temperature|rainfall|snowfall)",
            r"in ([A-Za-z\s,]+) (on|during|for)",
            r"^What will the (maximum|minimum|high|low) temperature be in ([A-Za-z\s,]+)"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                # Find the location capture group
                for group in match.groups():
                    if group and len(group.strip()) > 3 and not any(w in group.lower() for w in ['will', 'it', 'the', 'on', 'be']):
                        location = group.strip().rstrip(',. ')
                        if location:
                            return location
        
        logger.debug(f"Could not extract location from: {question[:80]}")
        return None

    def geocode_location(self, location_name: str) -> Optional[WeatherLocation]:
        """Geocode location name to coordinates using Open-Meteo geocoding API"""
        if location_name in self.location_cache:
            return self.location_cache[location_name]
        
        try:
            params = {
                "name": location_name,
                "count": 1,
                "language": "en",
                "format": "json"
            }
            
            response = requests.get(self.geocode_base, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if "results" not in data or len(data["results"]) == 0:
                return None
            
            result = data["results"][0]
            location = WeatherLocation(
                name=result["name"],
                latitude=result["latitude"],
                longitude=result["longitude"],
                confidence=0.95
            )
            
            self.location_cache[location_name] = location
            logger.info(f"Geocoded: {location_name} → {location.latitude}, {location.longitude}")
            
            time.sleep(0.1)
            return location
            
        except Exception as e:
            logger.warning(f"Geocoding failed for {location_name}: {str(e)}")
            return None

    def fetch_historical_weather(self, location: WeatherLocation, 
                               start_date: datetime.datetime, 
                               end_date: datetime.datetime) -> Optional[pd.DataFrame]:
        """Fetch historical weather data from Open-Meteo Archive API"""
        
        params = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "hourly": "temperature_2m,relative_humidity_2m,precipitation,rain,snowfall,surface_pressure,wind_speed_10m",
            "timezone": "UTC"
        }
        
        try:
            response = requests.get(self.open_meteo_base, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if "hourly" not in data:
                return None
            
            df = pd.DataFrame(data["hourly"])
            df["time"] = pd.to_datetime(df["time"], utc=True)
            
            # Rename columns to standard names
            df = df.rename(columns={
                "temperature_2m": "temperature",
                "relative_humidity_2m": "humidity",
                "surface_pressure": "pressure",
                "wind_speed_10m": "wind_speed"
            })
            
            logger.info(f"Fetched {len(df)} hours of weather data for {location.name}")
            time.sleep(0.2)
            return df
            
        except Exception as e:
            logger.error(f"Failed to fetch weather data: {str(e)}")
            return None

    def get_market_price_history(self, market_id: str) -> pd.DataFrame:
        """Get market OHLCV history from database"""
        query = """
            SELECT timestamp, open, high, low, close, volume
            FROM market_prices
            WHERE market_id = %s AND resolution = '1h'
            ORDER BY timestamp ASC
        """
        
        df = pd.read_sql(query, self.conn, params=(market_id,))
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df

    def merge_market_weather(self, market_id: str, question: str) -> Optional[pd.DataFrame]:
        """Merge market prices with actual weather measurements aligned by timestamp"""
        
        logger.info(f"\nProcessing market: {question[:70]}")
        
        # 1. Extract and geocode location
        location_name = self.extract_location_from_market(question)
        if not location_name:
            return None
            
        location = self.geocode_location(location_name)
        if not location:
            return None
        
        # 2. Get price history
        prices_df = self.get_market_price_history(market_id)
        if len(prices_df) < 24:
            logger.warning(f"Insufficient price data: {len(prices_df)} records")
            return None
        
        min_time = prices_df["timestamp"].min()
        max_time = prices_df["timestamp"].max()
        
        logger.info(f"Price data range: {min_time.date()} → {max_time.date()} ({len(prices_df)} hours)")
        
        # 3. Fetch weather data for the same time range
        weather_df = self.fetch_historical_weather(location, min_time, max_time)
        if weather_df is None or len(weather_df) == 0:
            return None
        
        # 4. PERFECT TIMESTAMP MERGE - EXACT MATCH ONLY
        # Inner join on exact hour timestamp (UTC aligned)
        merged_df = pd.merge(
            prices_df,
            weather_df,
            on="timestamp",
            how="inner"
        )
        
        # Add metadata columns
        merged_df["market_id"] = market_id
        merged_df["location"] = location.name
        merged_df["latitude"] = location.latitude
        merged_df["longitude"] = location.longitude
        
        logger.info(f"Merged dataset: {len(merged_df)} matched timestamps")
        return merged_df

    def build_master_dataset(self) -> pd.DataFrame:
        """Build complete merged dataset for all weather markets"""
        
        logger.info("\n=== Building Master Weather + Price Dataset ===")
        
        # Get all markets from backfilled data
        self.cursor.execute("""
            SELECT DISTINCT market_id
            FROM market_prices
            ORDER BY market_id
        """)
        
        markets = self.cursor.fetchall()
        logger.info(f"Found {len(markets)} weather markets")
        
        all_data = []
        
        # ✅ CORRECT MARKET FILTERING - EXACTLY WHAT WAS REQUESTED
        # Positive match keywords
        include_keywords = [
            'temperature', 'high temperature', 'low temperature', 'highest temperature', 'lowest temperature',
            'rain', 'snow', 'rainfall', 'snowfall',
            'tornado', 'hurricane', 'typhoon', 'cyclone',
            'earthquake', 'volcano', 'tsunami',
            'flood', 'drought', 'heatwave', 'cold wave',
            'wind speed', 'wind gust',
            'fahrenheit', 'celsius', 'degree'
        ]
        
        # ✅ EXPLICIT EXCLUSION LIST - THE #1 BUG FIX
        exclude_keywords = [
            'space weather', 'solar flare', 'geomagnetic', 'sunspot', 'coronal mass ejection',
            'solar storm', 'cosmic', 'aurora', 'magnetic field'
        ]
        
        # Get market metadata directly from database - no pmxt dependency required!
        self.cursor.execute("""
            SELECT DISTINCT mp.market_id, m.question
            FROM market_prices mp
            LEFT JOIN markets m ON mp.market_id = m.market_id
            WHERE mp.resolution = '1h'
            GROUP BY mp.market_id, m.question
            ORDER BY COUNT(*) DESC
        """)
        
        markets = self.cursor.fetchall()
        logger.info(f"Found {len(markets)} markets with price history")
        
        for market_id, question in markets:
            try:
                if not question:
                    continue
                    
                question_lower = question.lower()
                
                # 1. First check exclusions (most important!)
                if any(keyword in question_lower for keyword in exclude_keywords):
                    logger.debug(f"❌ EXCLUDED: Space weather market: {question[:60]}")
                    continue
                
                # 2. Check positive inclusion match
                if not any(keyword in question_lower for keyword in include_keywords):
                    continue
                
                logger.info(f"✅ ACCEPTED: {question[:80]}")
                
            except Exception as e:
                logger.debug(f"Skipping market {market_id}: {str(e)}")
                continue
                
            merged = self.merge_market_weather(market_id, question)
            if merged is not None and len(merged) > 0:
                all_data.append(merged)
        
        if not all_data:
            logger.error("No markets successfully merged")
            return pd.DataFrame()
        
        master_df = pd.concat(all_data, ignore_index=True)
        
        # Sort by timestamp and market
        master_df = master_df.sort_values(["market_id", "timestamp"]).reset_index(drop=True)
        
        logger.info("\n" + "="*60)
        logger.info("✅ MASTER DATASET COMPLETE")
        logger.info(f"Total rows: {len(master_df):,}")
        logger.info(f"Total markets: {master_df['market_id'].nunique()}")
        logger.info(f"Date range: {master_df['timestamp'].min().date()} → {master_df['timestamp'].max().date()}")
        logger.info(f"Columns: {list(master_df.columns)}")
        logger.info("="*60)
        
        return master_df

    def export_dataset(self, df: pd.DataFrame, output_path: str = "weather_market_dataset.parquet"):
        """Export dataset in model-ready format"""
        
        # Save in multiple formats
        df.to_parquet(output_path, index=False)
        logger.info(f"Dataset saved to {output_path}")
        
        # Also save CSV for easy inspection
        csv_path = output_path.replace('.parquet', '.csv')
        df.to_csv(csv_path, index=False, date_format='%Y-%m-%d %H:%M:%S')
        logger.info(f"CSV version saved to {csv_path}")
        
        # Generate summary statistics
        with open("dataset_summary.md", "w") as f:
            f.write("# Weather Market Dataset Summary\n\n")
            f.write(f"Generated: {datetime.datetime.utcnow()} UTC\n\n")
            f.write(f"Total observations: {len(df):,}\n")
            f.write(f"Total markets: {df['market_id'].nunique()}\n\n")
            f.write("## Statistics\n")
            f.write(df.describe().to_markdown())

    def close(self):
        self.cursor.close()
        self.conn.close()


if __name__ == "__main__":
    merger = None
    try:
        merger = WeatherDataMerger()
        master_dataset = merger.build_master_dataset()
        
        if len(master_dataset) > 0:
            merger.export_dataset(master_dataset)
            logger.info("\n✅ Pipeline completed successfully")
            logger.info("Dataset is ready for regression modelling and statistical analysis")
    
    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        sys.exit(0)
    finally:
        if merger:
            merger.close()