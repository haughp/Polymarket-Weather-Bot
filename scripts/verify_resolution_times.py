#!/usr/bin/env python3
"""
Verify Historical Resolution Times
Queries the Gamma API for the last 14 days of temperature markets
to determine exactly when Polymarket sets the 'endDate' (trading cutoff).
"""

import httpx
from datetime import datetime, timedelta, timezone

def verify_historical_resolutions(days_back=14):
    cities = ["nyc", "new-york", "miami", "chicago"]
    print("=" * 70)
    print(f" Historical Polymarket Trading Cutoffs (Last {days_back} days)")
    print("=" * 70)
    print(f"{'City':<12} | {'Target Date':<12} | {'API endDate (UTC)':<22} | {'Status':<8}")
    print("-" * 70)
    
    for i in range(1, days_back + 1):
        target_date = datetime.now(timezone.utc) - timedelta(days=i)
        month_name = target_date.strftime("%B").lower()
        day = target_date.day
        year = target_date.year
        
        for city in cities:
            slug = f"highest-temperature-in-{city}-on-{month_name}-{day}-{year}"
            url = f"https://gamma-api.polymarket.com/events?slug={slug}"
            
            try:
                r = httpx.get(url, timeout=10)
                data = r.json()
                if data and isinstance(data, list):
                    event = data[0]
                    end_date = event.get("endDate", "Unknown")
                    closed = event.get("closed", False)
                    status = "Closed" if closed else "Active"
                    date_str = f"{month_name[:3].capitalize()} {day:02d}, {year}"
                    print(f"{city:<12} | {date_str:<12} | {end_date:<22} | {status:<8}")
                    break  # Found a valid city slug for this date, move to next date
            except Exception as e:
                pass
        else:
            date_str = f"{month_name[:3].capitalize()} {day:02d}, {year}"
            print(f"{'Unknown':<12} | {date_str:<12} | {'Not Found':<22} | {'-':<8}")

if __name__ == "__main__":
    verify_historical_resolutions()