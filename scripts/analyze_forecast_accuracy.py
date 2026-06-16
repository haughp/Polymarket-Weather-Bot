#!/usr/bin/env python3
"""
analyze_forecast_accuracy.py — Forecast Accuracy vs. Lead Time

This script analyzes historical ECMWF forecasts stored in the database,
compares them against the actual verified outcomes, and plots the 
Mean Absolute Error (MAE) based on how many hours out the forecast was made.
"""

import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from sqlalchemy import text
import sys
import os

# Import your existing DB connection
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from database_schema import init_database

def fetch_accuracy_data(session):
    """
    Extracts historical forecasts and joins them with verified outcomes.
    Calculates lead time (hours from ECMWF run to peak time).
    """
    query = text("""
        SELECT 
            f.location_id,
            f.mode,
            f.forecast_temp,
            f.peak_time,
            f.ecmwf_run,
            o.actual_max_temp,
            o.actual_min_temp,
            -- Calculate lead time in hours
            EXTRACT(EPOCH FROM (f.peak_time - f.ecmwf_run)) / 3600.0 AS lead_time_hours
        FROM forecasts f
        JOIN outcomes o 
          ON f.location_id = o.location_id 
         AND DATE(f.peak_time) = DATE(o.date)
        WHERE o.verified = TRUE
          AND ((f.mode = 'max' AND o.actual_max_temp IS NOT NULL) OR 
               (f.mode = 'min' AND o.actual_min_temp IS NOT NULL))
    """)
    
    df = pd.read_sql(query, session.connection())
    return df

def plot_accuracy_decay(df):
    if df.empty:
        print("❌ No overlapping forecast/outcome data found. Has the bot saved forecasts recently?")
        return

    # Determine the actual target temperature based on the mode
    df['actual_temp'] = df.apply(
        lambda row: row['actual_max_temp'] if row['mode'] == 'max' else row['actual_min_temp'], 
        axis=1
    )
    
    # Calculate Absolute Error (in degrees)
    df['abs_error'] = (df['forecast_temp'] - df['actual_temp']).abs()

    # Bin the lead times into logical forecast windows
    bins = [0, 12, 24, 36, 48, 72]
    labels = ['0-12h', '12-24h (Current)', '24-36h (Proposed)', '36-48h', '48-72h']
    df['lead_time_bin'] = pd.cut(df['lead_time_hours'], bins=bins, labels=labels)

    # Aggregate MAE per bin
    agg_df = df.groupby('lead_time_bin', observed=False)['abs_error'].agg(['mean', 'count']).reset_index()
    agg_df = agg_df[agg_df['count'] > 0] # Filter empty bins

    sns.set_theme(style="whitegrid")
    plt.figure(figsize=(10, 6))
    
    # Bar chart for error magnitude
    ax = sns.barplot(x='lead_time_bin', y='mean', data=agg_df, color='coral')
    
    plt.title('ECMWF Forecast Mean Absolute Error (MAE) by Lead Time', fontsize=14)
    plt.xlabel('Hours Before Peak Temperature (Forecast Horizon)', fontsize=12)
    plt.ylabel('Mean Absolute Error (Degrees)', fontsize=12)
    
    # Add data labels on top of bars
    for i, row in agg_df.iterrows():
        ax.text(i, row['mean'] + 0.05, f"{row['mean']:.2f}°\n(n={row['count']})", 
                color='black', ha="center", fontsize=10)

    output_path = os.path.join(os.path.dirname(__file__), 'forecast_accuracy_analysis.png')
    plt.savefig(output_path)
    print(f"✅ Accuracy analysis complete. Chart saved to: {output_path}")

if __name__ == "__main__":
    session = init_database()
    print("Fetching forecast accuracy data...")
    df = fetch_accuracy_data(session)
    print(f"Found {len(df)} forecast records matched with actual outcomes.")
    plot_accuracy_decay(df)
    session.close()