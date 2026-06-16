#!/usr/bin/env python3
"""
analyze_time_regime.py — Time-to-Peak Price Evolution Analysis

This script tests the hypothesis behind the 18h-20h "time regime" by 
analyzing how the YES price of the *eventual winning bucket* evolves 
over the 48 hours leading up to the daily peak temperature.

It joins:
  1. sniff.weather_signals (30-min price ticks)
  2. sniff.forecasts (to get the exact peak_time)
  3. sniff.outcomes (to filter only for the bucket that actually won)
"""

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import numpy as np
from sqlalchemy import text
import sys
import os

# Import your existing DB connection
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from database_schema import init_database

def fetch_price_evolution_data(session):
    """
    Extracts price ticks for winning buckets, calculating hours_to_peak.
    Adjust the table/column names if your schema differs slightly.
    """
    query = text("""
        WITH WinningBuckets AS (
            -- Find the bucket that actually won for each city/date
            SELECT location_id, DATE(date) AS market_date, actual_max_temp AS actual_value
            FROM outcomes
            WHERE actual_max_temp IS NOT NULL
        ),
        UniqueForecasts AS (
            SELECT location_id, DATE(peak_time) AS market_date, MAX(peak_time) AS peak_time
            FROM forecasts
            WHERE mode = 'max'
            GROUP BY location_id, DATE(peak_time)
        )
        SELECT 
            s.city AS location_id,
            s.market_date,
            s.scanned_at,
            s.yes_price,
            f.peak_time,
            -- Calculate hours remaining from scan time until the peak temperature
            EXTRACT(EPOCH FROM (f.peak_time - s.scanned_at)) / 3600.0 AS hours_to_peak
        FROM sniff.weather_signals s
        JOIN UniqueForecasts f 
          ON s.city = f.location_id 
         AND s.market_date = f.market_date
        JOIN WinningBuckets w 
          ON s.city = w.location_id 
         AND s.market_date = w.market_date
        WHERE 
            -- Only look at the bucket that ended up winning
            s.bucket_lo <= w.actual_value AND w.actual_value <= s.bucket_hi
            -- Focus on the 48 hours prior to peak
            AND EXTRACT(EPOCH FROM (f.peak_time - s.scanned_at)) / 3600.0 BETWEEN -6 AND 48
    """)
    
    df = pd.read_sql(query, session.connection())
    return df

def plot_time_regime(df):
    """Groups data by hour and plots the average price curve."""
    if df.empty:
        print("❌ No data found. Ensure weather_signals, forecasts, and outcomes are populated.")
        return

    # Round hours_to_peak to the nearest hour for grouping
    df['hours_to_peak_rounded'] = df['hours_to_peak'].round(0)
    
    # Aggregate average price and standard deviation per hour
    agg_df = df.groupby('hours_to_peak_rounded')['yes_price'].agg(['mean', 'std', 'count']).reset_index()
    
    # Filter out buckets with too few samples to avoid noise
    agg_df = agg_df[agg_df['count'] >= 5]

    sns.set_theme(style="darkgrid")
    plt.figure(figsize=(12, 6))
    
    # Plot the evolution curve
    # Note: X-axis is inverted so time flows from left (48h) to right (0h)
    plt.plot(agg_df['hours_to_peak_rounded'], agg_df['mean'], marker='o', color='b', label='Average YES Price')
    plt.fill_between(agg_df['hours_to_peak_rounded'], 
                     agg_df['mean'] - agg_df['std'], 
                     agg_df['mean'] + agg_df['std'], 
                     color='b', alpha=0.2, label='±1 Std Dev')

    # Highlight our hypothesized 18-20h regime
    plt.axvspan(20, 18, color='green', alpha=0.3, label='Current Entry Window (18-20h)')
    
    plt.gca().invert_xaxis()
    plt.title('Winning Bucket YES Price vs. Hours to Peak Temperature', fontsize=14)
    plt.xlabel('Hours Until Forecasted Peak Temperature', fontsize=12)
    plt.ylabel('Polymarket YES Price ($)', fontsize=12)
    plt.axvline(x=0, color='red', linestyle='--', label='Peak Temperature Time')
    plt.legend()
    plt.tight_layout()
    
    output_path = os.path.join(os.path.dirname(__file__), 'time_regime_analysis.png')
    plt.savefig(output_path)
    print(f"✅ Analysis complete. Chart saved to: {output_path}")

if __name__ == "__main__":
    session = init_database()
    print("Fetching price evolution data...")
    df = fetch_price_evolution_data(session)
    print(f"Found {len(df)} historical price ticks for winning buckets.")
    plot_time_regime(df)
    session.close()