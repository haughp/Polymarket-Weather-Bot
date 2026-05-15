#!/usr/bin/env python3
"""
Polymarket Monthly Precipitation Dry-Run Scanner
Matches ECMWF/Open-Meteo monthly precipitation forecasts against
Polymarket bucket markets.  Sells NO on buckets outside the
[P05, P95] confidence band.

Slug format:  precipitation-in-{city_slug}-in-{month_name}
Cities:       hong-kong, seattle, nyc, seoul, london
"""

import re
import json
import datetime
import calendar
import httpx
from database_schema import (
    init_database,
    PrecipForecast, PrecipMarketState, PrecipMarketBucket, PrecipTradeSimulation,
)
from precip_forecast_pipeline import PRECIP_LOCATIONS

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BASE    = "https://clob.polymarket.com"

MONTHS = [
    'january','february','march','april','may','june',
    'july','august','september','october','november','december',
]

TRADE_SIZE_USD = 5.0


# ── CLOB helpers (identical to temperature dry-run) ──────────────────────────

def clob_no_token(market: dict | None) -> str | None:
    if not market:
        return None
    raw = market.get('clobTokenIds', '[]') or '[]'
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return str(ids[1]) if len(ids) > 1 else None
    except Exception:
        return None


def clob_yes_token(market: dict | None) -> str | None:
    if not market:
        return None
    raw = market.get('clobTokenIds', '[]') or '[]'
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return str(ids[0]) if ids else None
    except Exception:
        return None


def get_no_market_data(clob_token_id: str, market: dict | None = None) -> dict:
    """
    Fetch live CLOB data for a NO token.
    Correct endpoints: /book?token_id=X for bid/ask depth.
    Volume comes from Gamma market dict (volume24hr field).
    """
    result: dict = {}
    try:
        with httpx.Client(timeout=10) as client:
            try:
                r = client.get(f"{CLOB_BASE}/book", params={'token_id': clob_token_id})
                d = r.json()
                bids = sorted(d.get('bids', []), key=lambda x: float(x['price']), reverse=True)
                asks = sorted(d.get('asks', []), key=lambda x: float(x['price']))
                result['best_bid'] = float(bids[0]['price']) if bids else None
                result['best_ask'] = float(asks[0]['price']) if asks else None
                result['order_book'] = d
            except Exception:
                pass
    except Exception as e:
        result['error'] = str(e)

    # Volume from Gamma market dict (already fetched — no extra API call needed)
    if market:
        try:
            v = market.get('volume24hr') or market.get('volume_24hr')
            result['volume_24h'] = float(v) if v else None
        except Exception:
            pass
    return result


def extract_prices(market: dict | None) -> tuple[float | None, float | None]:
    if not market:
        return None, None
    raw = market.get('outcomePrices', '[0.5,0.5]') or '[0.5,0.5]'
    try:
        p = json.loads(raw) if isinstance(raw, str) else raw
        return float(p[0]) if p else None, float(p[1]) if len(p) > 1 else None
    except Exception:
        return None, None


# ── Market discovery ─────────────────────────────────────────────────────────

def fetch_precip_event_markets(city_slug: str, month_str: str) -> list[dict]:
    slugs_to_try = [f"precipitation-in-{city_slug}-in-{month_str}"]
    if city_slug == "nyc":
        slugs_to_try.extend([f"precipitation-in-new-york-in-{month_str}", f"precipitation-in-new-york-city-in-{month_str}"])
    elif city_slug == "seattle":
        slugs_to_try.append(f"precipitation-in-seattle-wa-in-{month_str}")

    for slug in slugs_to_try:
        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(GAMMA_EVENTS, params={'slug': slug})
                data = r.json()
                if data and data[0].get('markets'):
                    return data[0]['markets']
        except Exception:
            continue
    
    print(f"   ⚠️  Gamma API error: Could not find markets for {city_slug}")
    return []


# ── Range parsing ─────────────────────────────────────────────────────────────

def parse_precip_range(question: str) -> tuple[float | None, float | None]:
    """
    Returns (low, high) thresholds in the market's native units.
    (None, X)  → 'less than X'
    (X, Y)     → 'between X and Y' / 'between X-Y'
    (X, None)  → 'X or more' / 'more than X'
    """
    q = question.lower()

    # between X and Y  (inches)
    m = re.search(r'between\s+([\d.]+)\s+and\s+([\d.]+)', q)
    if m:
        return float(m.group(1)), float(m.group(2))

    # between X-Y  (mm)
    m = re.search(r'between\s+([\d.]+)[-–]([\d.]+)', q)
    if m:
        return float(m.group(1)), float(m.group(2))

    # less than / below
    m = re.search(r'(?:less than|below|under)\s+([\d.]+)', q)
    if m:
        return None, float(m.group(1))

    # X or more / more than X / X mm or more / greater than X
    m = re.search(r'([\d.]+)\s+(?:mm|inches?)?\s*or\s+more', q)
    if m:
        return float(m.group(1)), None
    m = re.search(r'(?:more than|greater than|above|over)\s+([\d.]+)', q)
    if m:
        return float(m.group(1)), None

    return None, None


# ── Classification ─────────────────────────────────────────────────────────────

def classify_precip_markets(
    markets: list[dict],
    total_forecast: float,
    p05: float,
    p95: float,
) -> dict:
    """
    Bins markets into lower / predicted / upper buckets.
    lower  = bucket entirely below p05  (safe to sell NO)
    upper  = bucket entirely above p95  (safe to sell NO)
    predicted = bucket that straddles or contains total_forecast
    """
    lower_list: list[dict]     = []
    upper_list: list[dict]     = []
    predicted_list: list[dict] = []

    for mkt in markets:
        q = mkt.get('question', '')
        low, high = parse_precip_range(q)

        if low is None and high is None:
            continue

        bucket_high = high if high is not None else float('inf')
        bucket_low  = low  if low  is not None else 0.0

        if bucket_high <= p05:
            lower_list.append(mkt)
        elif bucket_low >= p95:
            upper_list.append(mkt)
        elif bucket_low <= total_forecast <= bucket_high:
            predicted_list.append(mkt)

    def best_by_yes(lst):
        if not lst:
            return None
        return max(lst, key=lambda m: (extract_prices(m)[0] or 0))

    return {
        'lower':         best_by_yes(lower_list),
        'upper':         best_by_yes(upper_list),
        'predicted':     best_by_yes(predicted_list),
        'lower_all':     lower_list,
        'upper_all':     upper_list,
        'predicted_all': predicted_list,
    }


# ── Main recording function ───────────────────────────────────────────────────

def record_precip_dry_run(forecast_row: dict, session) -> None:
    loc_id    = forecast_row['location_id']
    obs       = PRECIP_LOCATIONS[loc_id]
    city_slug = obs['city_slug']
    units     = forecast_row['units']

    # Settlement month (first day of month)
    settlement_month = forecast_row['settlement_month']
    if isinstance(settlement_month, str):
        settlement_month = datetime.date.fromisoformat(settlement_month)

    month_str = MONTHS[settlement_month.month - 1]

    print(f"\n{'─'*60}")
    print(f"📍 {obs['name']}  [{month_str.capitalize()} {settlement_month.year}]")
    print(f"   Forecast: {forecast_row['total_forecast']:.1f} {units}  "
          f"(P05={forecast_row['p05']:.1f}, P95={forecast_row['p95']:.1f})")
    print(f"   Accumulated: {forecast_row['accumulated']:.1f}  "
          f"Remaining: {forecast_row['forecast_remaining']:.1f}  "
          f"Days left: {forecast_row['days_remaining']}")

    # Also scan the next month if it has markets
    months_to_scan = [month_str]
    if forecast_row['days_remaining'] <= 7:
        # Near end of month — also scan next month's markets
        next_month_idx = settlement_month.month % 12  # 0-indexed next month
        months_to_scan.append(MONTHS[next_month_idx])

    for i, scan_month_str in enumerate(months_to_scan):
        _scan_month(loc_id, obs, city_slug, units, forecast_row,
                    scan_month_str, session, is_next_month=(i > 0))


def _scan_month(loc_id, obs, city_slug, units, forecast_row,
                month_str, session, is_next_month: bool = False) -> None:
    markets = fetch_precip_event_markets(city_slug, month_str)
    if not markets:
        print(f"   ℹ️  No markets found for {city_slug}/{month_str}")
        return

    # For next month, use the forecast as-is (best available estimate)
    total   = float(forecast_row['total_forecast'])
    p05     = float(forecast_row['p05'])
    p95     = float(forecast_row['p95'])

    buckets = classify_precip_markets(markets, total, p05, p95)

    lower_mkt = buckets['lower']
    upper_mkt = buckets['upper']
    pred_mkt  = buckets['predicted']

    print(f"\n   Markets found: {len(markets)} | "
          f"lower_tail={len(buckets['lower_all'])} | "
          f"upper_tail={len(buckets['upper_all'])} | "
          f"predicted={len(buckets['predicted_all'])}")

    if not any([lower_mkt, upper_mkt, pred_mkt]):
        qs = [m.get('question','')[:60] for m in markets[:3]]
        print(f"   ⚠️  No markets classified. Samples: {qs}")
        return

    lower_yes, lower_no = extract_prices(lower_mkt)
    upper_yes, upper_no = extract_prices(upper_mkt)
    pred_yes,  pred_no  = extract_prices(pred_mkt)

    # CLOB data for best tail markets
    lower_token_no = clob_no_token(lower_mkt)
    upper_token_no = clob_no_token(upper_mkt)
    lower_clob: dict = get_no_market_data(lower_token_no, lower_mkt) if lower_token_no else {}
    upper_clob: dict = get_no_market_data(upper_token_no, upper_mkt) if upper_token_no else {}

    if lower_mkt:
        print(f"   📉 Lower tail: {lower_mkt.get('question','')[:65]}")
        print(f"      YES={lower_yes}  NO={lower_no}  ask={lower_clob.get('best_ask')}  vol={lower_clob.get('volume_24h')}")
    if upper_mkt:
        print(f"   📈 Upper tail: {upper_mkt.get('question','')[:65]}")
        print(f"      YES={upper_yes}  NO={upper_no}  ask={upper_clob.get('best_ask')}  vol={upper_clob.get('volume_24h')}")
    if pred_mkt:
        print(f"   🎯 Predicted:  {pred_mkt.get('question','')[:65]}")
        print(f"      YES={pred_yes}  NO={pred_no}")

    # Derive settlement_month from month_str and forecast year
    forecast_year = forecast_row['settlement_month']
    if isinstance(forecast_year, str):
        forecast_year = datetime.date.fromisoformat(forecast_year)
    month_idx = MONTHS.index(month_str) + 1
    year = forecast_year.year if month_idx >= forecast_year.month else forecast_year.year + 1
    settlement_month = datetime.date(year, month_idx, 1)

    raw_book = {
        'lower_no': lower_clob.get('order_book'),
        'upper_no': upper_clob.get('order_book'),
    }

    # PrecipMarketState
    state = PrecipMarketState(
        forecast_id         = forecast_row.get('db_id'),
        location_id         = loc_id,
        settlement_month    = settlement_month,
        lower_yes_price     = lower_yes,
        lower_no_price      = lower_no,
        lower_clob_token_no = lower_token_no,
        lower_no_best_ask   = lower_clob.get('best_ask'),
        lower_no_best_bid   = lower_clob.get('best_bid'),
        lower_no_volume_24h = lower_clob.get('volume_24h'),
        predicted_yes_price = pred_yes,
        predicted_no_price  = pred_no,
        upper_yes_price     = upper_yes,
        upper_no_price      = upper_no,
        upper_clob_token_no = upper_token_no,
        upper_no_best_ask   = upper_clob.get('best_ask'),
        upper_no_best_bid   = upper_clob.get('best_bid'),
        upper_no_volume_24h = upper_clob.get('volume_24h'),
        order_book_depth    = raw_book,
    )
    session.add(state)
    session.flush()

    # PrecipMarketBucket — all markets
    for bucket_type, all_mkts, clob_data in [
        ('lower',     buckets['lower_all'],     lower_clob),
        ('upper',     buckets['upper_all'],     upper_clob),
        ('predicted', buckets['predicted_all'], {}),
    ]:
        best_mkt = buckets[bucket_type]
        for mkt in all_mkts:
            low_th, high_th = parse_precip_range(mkt.get('question', ''))
            yes_p, no_p = extract_prices(mkt)
            is_best = (mkt is best_mkt)
            bucket = PrecipMarketBucket(
                market_state_id  = state.id,
                location_id      = loc_id,
                settlement_month = settlement_month,
                bucket_type      = bucket_type,
                is_best          = is_best,
                question_text    = mkt.get('question', '')[:512],
                low_threshold    = low_th,
                high_threshold   = high_th,
                clob_token_yes   = clob_yes_token(mkt),
                clob_token_no    = clob_no_token(mkt),
                yes_price        = yes_p,
                no_price         = no_p,
            )
            if is_best and clob_data:
                bucket.no_best_ask  = clob_data.get('best_ask')
                bucket.no_best_bid  = clob_data.get('best_bid')
                bucket.no_volume_24h = clob_data.get('volume_24h')
            session.add(bucket)

    # PrecipTradeSimulation — NO bets on tail buckets
    # Skip sim trades for next month until ≥7 days have elapsed (no data yet)
    days_elapsed = forecast_row.get('days_elapsed', 0)
    if is_next_month and days_elapsed < 7:
        print(f"   ⏭️  Skipping next-month sim trades — only {days_elapsed} days elapsed")
        return

    # Block sim trades for cities where ERA5 is the only data source.
    # ERA5 overstates HK/Seoul by ~27-32% vs actual resolution source (HKO/KMA),
    # which can move the accumulated figure across threshold boundaries.
    # Example: Seoul ERA5=53mm but market prices <40mm at YES=98.9% — ERA5 is wrong.
    data_confidence = forecast_row.get('data_confidence', 'low')
    if data_confidence != 'high':
        resolution_src = forecast_row.get('resolution_source', 'unknown')
        print(f"   ⚠️  SIM TRADES BLOCKED — data source is ERA5, not {resolution_src} station data.")
        print(f"      ERA5 overstates vs {resolution_src} by ~25-32%. Signals unreliable for threshold comparison.")
        print(f"      To unlock: obtain {resolution_src} API access or add manual verification step.")
        return

    clob_by_side = {'lower': lower_clob, 'upper': upper_clob}

    for side, mkt, token_no in [('lower', lower_mkt, lower_token_no),
                                  ('upper', upper_mkt, upper_token_no)]:
        if mkt is None:
            continue
        _, gamma_no_price = extract_prices(mkt)
        if gamma_no_price is None:
            continue

        # Use real CLOB ask if available — more accurate than Gamma mid
        clob = clob_by_side[side]
        fill_price = clob.get('best_ask') or gamma_no_price

        # Skip near-certain bets where fill price leaves < $0.50 edge per $5
        if fill_price >= 0.90:
            continue

        payout = round(TRADE_SIZE_USD / fill_price, 2)
        pnl    = round(payout - TRADE_SIZE_USD, 2)
        sim = PrecipTradeSimulation(
            forecast_id      = forecast_row.get('db_id'),
            location_id      = loc_id,
            settlement_month = settlement_month,
            clob_token_id    = token_no,
            question_text    = mkt.get('question', '')[:512],
            market_side      = side,
            order_type       = 'NO',
            price            = fill_price,
            size             = TRADE_SIZE_USD,
            simulated_pnl    = pnl,
            confidence_pct   = forecast_row.get('max_prob'),
        )
        session.add(sim)
        src = 'clob' if clob.get('best_ask') else 'gamma'
        print(f"   💰 SIM NO {side:5s}  price={fill_price:.3f} [{src}]  "
              f"payout=${payout:.2f}  PnL=${pnl:.2f}  "
              f"[{mkt.get('question','')[:50]}]")


# ── Orchestration ─────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("Polymarket Monthly Precipitation Dry-Run Scanner")
    print("=" * 70)

    session = init_database()

    # Load latest PrecipForecast per location
    from sqlalchemy import func
    latest_subq = (
        session.query(
            PrecipForecast.location_id,
            func.max(PrecipForecast.created_at).label('max_ts'),
        ).group_by(PrecipForecast.location_id).subquery()
    )
    forecasts = (
        session.query(PrecipForecast)
        .join(latest_subq, (PrecipForecast.location_id == latest_subq.c.location_id) &
                           (PrecipForecast.created_at  == latest_subq.c.max_ts))
        .all()
    )

    if not forecasts:
        print("❌ No precipitation forecasts in DB — run precip_forecast_pipeline.py first")
        session.close()
        return

    for fc in forecasts:
        obs = PRECIP_LOCATIONS.get(fc.location_id, {})
        forecast_dict = {
            'db_id':              fc.id,
            'location_id':        fc.location_id,
            'location_name':      fc.location_name,
            'settlement_month':   fc.settlement_month,
            'accumulated':        float(fc.accumulated or 0),
            'forecast_remaining': float(fc.forecast_remaining or 0),
            'total_forecast':     float(fc.total_forecast or 0),
            'p05':                float(fc.p05 or 0),
            'p95':                float(fc.p95 or 0),
            'max_prob':           fc.max_prob,
            'days_remaining':     fc.days_remaining,
            'days_elapsed':       fc.days_elapsed or 0,
            'units':              fc.units,
            # Data source metadata — used to gate sim trades
            'data_confidence':    obs.get('data_confidence', 'low'),
            'resolution_source':  obs.get('resolution_source', 'unknown'),
            'source':             fc.source or obs.get('data_source', 'era5'),
        }
        record_precip_dry_run(forecast_dict, session)

    session.commit()
    session.close()
    print("\n✅ Precipitation dry-run complete")


if __name__ == "__main__":
    main()
