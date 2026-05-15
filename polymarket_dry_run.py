#!/usr/bin/env python3
"""
Polymarket Dry Run Execution Logger
Discovers active weather markets via Gamma API for each ECMWF forecast location,
records order book state, and simulates NO bets on both confidence-band tails.
"""

import re
import sys
import json
import datetime
import httpx
from zoneinfo import ZoneInfo
from database_schema import init_database, Forecast, MarketState, MarketBucket, TradeSimulation

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

MONTHS = [
    'january', 'february', 'march', 'april', 'may', 'june',
    'july', 'august', 'september', 'october', 'november', 'december',
]

LOCATION_SLUGS = {
    'shanghai':     'shanghai',
    'hong_kong':    'hong-kong',
    'qingdao':      'qingdao',
    'beijing':      'beijing',
    'london':       'london',
    'dallas':       'dallas',
    'paris':        'paris',
    'taipei':       'taipei',
    'new_york':     'nyc',          # Polymarket uses "nyc" not "new-york"
    'lucknow':      'lucknow',
    'toronto':      'toronto',
    'atlanta':      'atlanta',
    'buenos_aires': 'buenos-aires',
    'singapore':    'singapore',
    'moscow':       'moscow',
    'tokyo':        'tokyo',
    'madrid':       'madrid',
    'shenzhen':     'shenzhen',
    'chongqing':    'chongqing',
    'austin':       'austin',
}

# IANA timezone for each observatory — used to convert forecast valid time
# (UTC) to the local calendar date that Polymarket markets are named after.
LOCATION_TIMEZONES = {
    'shanghai':     'Asia/Shanghai',
    'hong_kong':    'Asia/Hong_Kong',
    'qingdao':      'Asia/Shanghai',
    'beijing':      'Asia/Shanghai',
    'london':       'Europe/London',
    'dallas':       'America/Chicago',
    'paris':        'Europe/Paris',
    'taipei':       'Asia/Taipei',
    'new_york':     'America/New_York',
    'lucknow':      'Asia/Kolkata',
    'toronto':      'America/Toronto',
    'atlanta':      'America/New_York',
    'buenos_aires': 'America/Argentina/Buenos_Aires',
    'singapore':    'Asia/Singapore',
    'moscow':       'Europe/Moscow',
    'tokyo':        'Asia/Tokyo',
    'madrid':       'Europe/Madrid',
    'shenzhen':     'Asia/Shanghai',
    'chongqing':    'Asia/Shanghai',
    'austin':       'America/Chicago',
}

SIM_SIZE_USD = 5.0


# ── Gamma API helpers ──────────────────────────────────────────────────────────

def fetch_event_markets(location_slug: str, date: datetime.date, mode: str = 'max') -> list[dict]:
    """Return markets for a highest/lowest-temperature event on a given date."""
    month = MONTHS[date.month - 1]
    prefix = "highest" if mode == 'max' else "lowest"
    slug = f"{prefix}-temperature-in-{location_slug}-on-{month}-{date.day}-{date.year}"
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{GAMMA_API}/events", params={'slug': slug})
            r.raise_for_status()
            data = r.json()
        if isinstance(data, list) and data:
            return data[0].get('markets', [])
    except Exception as e:
        print(f"   ⚠️  Gamma API error ({slug}): {e}")
    return []


def get_no_market_data(clob_token_id: str, market: dict | None = None) -> dict:
    """
    Fetch NO-side price and 24h volume for a CLOB token.
    Returns: {best_ask, best_bid, volume_24h, order_book}
    Correct endpoints: /book?token_id=X for depth; volume from Gamma market dict.
    """
    result: dict = {}
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{CLOB_API}/book", params={'token_id': clob_token_id})
            d = r.json()
            bids = sorted(d.get('bids', []), key=lambda x: float(x['price']), reverse=True)
            asks = sorted(d.get('asks', []), key=lambda x: float(x['price']))
            result['best_bid'] = float(bids[0]['price']) if bids else None
            result['best_ask'] = float(asks[0]['price']) if asks else None
            result['order_book'] = d
    except Exception as exc:
        result['error'] = str(exc)

    if market:
        try:
            v = market.get('volume24hr') or market.get('volume_24hr')
            result['volume_24h'] = float(v) if v else None
        except Exception:
            pass
    return result


def get_yes_ask(clob_token_id_yes: str) -> float | None:
    """Return the best YES ask price for a token, or None on failure."""
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{CLOB_API}/book", params={'token_id': clob_token_id_yes})
            r.raise_for_status()
            data = r.json()
        asks = sorted([float(a['price']) for a in data.get('asks', []) if float(a['price']) < 0.97])
        return asks[0] if asks else None
    except Exception as e:
        print(f"   ⚠️  CLOB YES ask fetch failed ({clob_token_id_yes[:12]}…): {e}")
        return None


# ── Market classification ──────────────────────────────────────────────────────

def parse_temp_range(question: str) -> tuple[float | None, float | None]:
    """
    Extract (low, high) temperature bounds from a market question.
    Returns (None, X) for below-X markets, (X, None) for above-X markets,
    (lo, hi) for range buckets, and (X, X) for single-degree HK-style markets.
    """
    q = question.lower()

    # "between X and Y" or "X-Y°"
    m = re.search(r'between\s+(\d+\.?\d*)\s*(?:-|and)\s*(\d+\.?\d*)', q)
    if m:
        return float(m.group(1)), float(m.group(2))
    # bare "54-55°f" / "19-20°c" style range
    m = re.search(r'(\d+)\s*-\s*(\d+)°', question)
    if m:
        return float(m.group(1)), float(m.group(2))

    # "below X" / "under X" / "X°F or below" / "X or lower"
    # The \s*°?[a-z]* allows for unit suffixes like °F / °C between the number and "or"
    m = re.search(r'(?:below|under|less than)\s+(\d+\.?\d*)', q)
    if m:
        return None, float(m.group(1))
    m = re.search(r'(\d+\.?\d*)\s*°?[a-z]*\s+or\s+(?:below|under|lower|less)', q)
    if m:
        return None, float(m.group(1))

    # "above X" / "over X" / "X°F or higher" / "X or above"
    m = re.search(r'(?:above|over|more than|higher than|at least)\s+(\d+\.?\d*)', q)
    if m:
        return float(m.group(1)), None
    m = re.search(r'(\d+\.?\d*)\s*°?[a-z]*\s+or\s+(?:above|over|higher|more)', q)
    if m:
        return float(m.group(1)), None

    # Single-degree format: "be 26°C" / "be 26°" (Hong Kong / Shanghai style)
    m = re.search(r'\bbe\s+(\d+\.?\d*)°', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return v, v   # exact bucket — treat as (X, X) for band comparison

    return None, None


def classify_markets(
    markets: list[dict],
    forecast_temp: float,
    lower_band: float,
    upper_band: float,
) -> dict:
    """
    Classify markets into lower-tail, upper-tail, and predicted buckets.

    lower tail  → high < lower_band  (i.e. resolves YES only if temp is below our floor)
    upper tail  → low  > upper_band  (i.e. resolves YES only if temp is above our ceiling)
    predicted   → range straddles forecast_temp

    Returns lists for tail markets so we capture every bucket outside the band —
    useful when markets use single-degree buckets (Hong Kong style).
    The 'best_lower' / 'best_upper' entries are the single highest-YES market on
    each side (most price-informative for CLOB recording).
    """
    result: dict = {
        'lower': None,          # best single lower-tail market
        'upper': None,          # best single upper-tail market
        'predicted': None,
        'lower_all': [],        # all lower-tail markets
        'upper_all': [],        # all upper-tail markets
        'in_band': [],          # buckets whose range overlaps [lower_band, upper_band]
    }

    for mkt in markets:
        question = mkt.get('question', '')
        low, high = parse_temp_range(question)
        if low is None and high is None:
            continue

        try:
            prices = json.loads(mkt.get('outcomePrices', '[0.5,0.5]') or '[0.5,0.5]')
            yes_p = float(prices[0])
        except Exception:
            yes_p = 0.0

        # Single-degree exact bucket (low == high)
        if low is not None and high is not None and low == high:
            v = low
            if v < lower_band:
                result['lower_all'].append(mkt)
            elif v > upper_band:
                result['upper_all'].append(mkt)
            elif low <= forecast_temp <= high or abs(v - forecast_temp) < 0.6:
                result['predicted'] = mkt
            if lower_band <= v <= upper_band:
                result['in_band'].append(mkt)
            continue

        # Range buckets and tail markers
        if high is not None and (low is None or low < 0) and high <= lower_band:
            result['lower_all'].append(mkt)
        elif low is not None and (high is None or high > 999) and low >= upper_band:
            result['upper_all'].append(mkt)
            
        eff_low = low if low is not None else float('-inf')
        eff_high = high if high is not None else float('inf')
        
        if eff_low <= forecast_temp <= eff_high:
            result['predicted'] = mkt
            
        if eff_low <= upper_band and eff_high >= lower_band:
            result['in_band'].append(mkt)

    # Pick the most price-informative (highest YES price) market from each tail list
    if result['lower_all']:
        result['lower'] = max(result['lower_all'],
                              key=lambda m: float(json.loads(m.get('outcomePrices', '[0]') or '[0]')[0]))
    if result['upper_all']:
        result['upper'] = max(result['upper_all'],
                              key=lambda m: float(json.loads(m.get('outcomePrices', '[0]') or '[0]')[0]))

    return result


# ── Price / book helpers ───────────────────────────────────────────────────────

def extract_prices(market: dict | None) -> tuple[float | None, float | None]:
    """Return (yes_price, no_price) from outcomePrices, or (None, None)."""
    if not market:
        return None, None
    raw = market.get('outcomePrices', '[0.5,0.5]')
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        yes_p = float(prices[0]) if prices else 0.5
        no_p = float(prices[1]) if len(prices) > 1 else round(1 - yes_p, 4)
        return yes_p, no_p
    except Exception:
        return 0.5, 0.5


def clob_yes_token(market: dict | None) -> str | None:
    """Return the CLOB YES token ID (index 0) for a market."""
    if not market:
        return None
    raw = market.get('clobTokenIds', '[]') or '[]'
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return str(ids[0]) if ids else None
    except Exception:
        return None


def clob_no_token(market: dict | None) -> str | None:
    """Return the CLOB NO token ID (index 1) for a market."""
    if not market:
        return None
    raw = market.get('clobTokenIds', '[]') or '[]'
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        return str(ids[1]) if len(ids) > 1 else (str(ids[0]) if ids else None)
    except Exception:
        return None


# ── Main recording logic ───────────────────────────────────────────────────────

def record_dry_run(forecast: dict, session) -> None:
    """
    Discover the Polymarket weather market for the date corresponding to the
    ECMWF 36h forecast valid time, record NO-side CLOB price + volume for both
    tail markets, and persist simulated NO bets.

    Strategy: sell NO on both ±1°C tails. If the forecast is correct, both
    markets resolve NO (we win both). If forecast is wrong by >1°C in one
    direction, that tail's NO loses but the other wins — partial hedge.
    """
    location_id = forecast['location_id']
    mode = forecast.get('mode', 'max')
    slug = LOCATION_SLUGS.get(location_id, location_id.replace('_', '-'))
    units_label = '°F' if forecast['units'] == 'fahrenheit' else '°C'

    # ── Derive the target market date ─────────────────────────────────────────
    tz_name = LOCATION_TIMEZONES.get(location_id, 'UTC')
    now_local = datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo(tz_name))
    target_date = (now_local + datetime.timedelta(days=1)).date()

    print(f"\n📊 {forecast['name']} ({mode.upper()}): forecast={forecast['forecast_temp']}{units_label}  "
          f"band=[{forecast['lower_band']}, {forecast['upper_band']}]")
    print(f"   Target market: {target_date} ({tz_name})")

    markets = fetch_event_markets(slug, target_date, mode)
    if not markets:
        print(f"   ⚠️  No Polymarket markets found for {location_id} on {target_date}")
        return

    tails = classify_markets(
        markets,
        float(forecast['forecast_temp']),
        float(forecast['lower_band']),
        float(forecast['upper_band']),
    )

    if not tails['lower'] and not tails['upper'] and not tails['predicted']:
        print(f"   ⚠️  Markets exist but none classify into lower/predicted/upper")
        print(f"        Sample questions: {[m.get('question','')[:60] for m in markets[:4]]}")
        return

    lower_yes, lower_no = extract_prices(tails['lower'])
    pred_yes,  pred_no  = extract_prices(tails['predicted'])
    upper_yes, upper_no = extract_prices(tails['upper'])

    # ── Fetch CLOB NO data once per best tail market ───────────────────────────
    lower_token_no = clob_no_token(tails['lower'])
    upper_token_no = clob_no_token(tails['upper'])
    lower_clob: dict = {}
    upper_clob: dict = {}

    if lower_token_no:
        lower_clob = get_no_market_data(lower_token_no, tails['lower'])
        print(f"   📈 LOWER tail NO  ask={lower_clob.get('best_ask')}  "
              f"bid={lower_clob.get('best_bid')}  vol_24h={lower_clob.get('volume_24h')}  "
              f"q={tails['lower'].get('question','')[:60]}")

    if upper_token_no:
        upper_clob = get_no_market_data(upper_token_no, tails['upper'])
        print(f"   📈 UPPER tail NO  ask={upper_clob.get('best_ask')}  "
              f"bid={upper_clob.get('best_bid')}  vol_24h={upper_clob.get('volume_24h')}  "
              f"q={tails['upper'].get('question','')[:60]}")

    for side in ('lower', 'upper'):
        for mkt in tails.get(f'{side}_all', []):
            yes_p, no_p = extract_prices(mkt)
            print(f"      {side} bucket: YES={yes_p:.4f}  NO={no_p:.4f}  "
                  f"{mkt.get('question','')[:60]}")

    # ── Persist MarketState summary row ───────────────────────────────────────
    # flush() after add so state.id is available for MarketBucket FK
    raw_book = {
        'lower_no_best': {**lower_clob, 'question': tails['lower'].get('question', '') if tails['lower'] else ''},
        'upper_no_best': {**upper_clob, 'question': tails['upper'].get('question', '') if tails['upper'] else ''},
    }
    state = MarketState(
        forecast_id=forecast.get('db_id'),
        location_id=location_id,
        mode=mode,
        market_date=target_date,
        lower_yes_price=lower_yes,
        lower_no_price=lower_no,
        lower_clob_token_no=lower_token_no,
        lower_no_best_ask=lower_clob.get('best_ask'),
        lower_no_best_bid=lower_clob.get('best_bid'),
        lower_no_volume_24h=lower_clob.get('volume_24h'),
        predicted_yes_price=pred_yes,
        predicted_no_price=pred_no,
        upper_yes_price=upper_yes,
        upper_no_price=upper_no,
        upper_clob_token_no=upper_token_no,
        upper_no_best_ask=upper_clob.get('best_ask'),
        upper_no_best_bid=upper_clob.get('best_bid'),
        upper_no_volume_24h=upper_clob.get('volume_24h'),
        order_book_depth=raw_book,
    )
    session.add(state)
    session.flush()  # populate state.id before creating child bucket rows

    # ── Persist MarketBucket rows — one per temperature bucket per scan ────────
    # lower/upper: all tail markets from classify_markets; predicted: single best.
    for bucket_type in ('lower', 'upper', 'predicted'):
        if bucket_type == 'predicted':
            all_mkts = [tails['predicted']] if tails['predicted'] else []
        else:
            all_mkts = tails.get(f'{bucket_type}_all', [])
            if not all_mkts and tails[bucket_type]:
                all_mkts = [tails[bucket_type]]

        best_mkt  = tails[bucket_type]
        clob_data = lower_clob if bucket_type == 'lower' else (
                    upper_clob if bucket_type == 'upper' else {})

        for mkt in all_mkts:
            yes_p, no_p = extract_prices(mkt)
            is_best     = (mkt is best_mkt)
            bucket = MarketBucket(
                market_state_id=state.id,
                location_id=location_id,
                market_date=target_date,
                bucket_type=bucket_type,
                is_best=is_best,
                question_text=mkt.get('question', '')[:512],
                clob_token_yes=clob_yes_token(mkt),
                clob_token_no=clob_no_token(mkt),
                yes_price=yes_p,
                no_price=no_p,
            )
            if is_best and clob_data:
                bucket.no_best_ask   = clob_data.get('best_ask')
                bucket.no_best_bid   = clob_data.get('best_bid')
                bucket.no_volume_24h = clob_data.get('volume_24h')
            session.add(bucket)

    # ── Simulate YES bets on in-band buckets ──────────────────────────────────────
    YES_PRICE_CAP = 0.30

    for mkt in tails['in_band']:
        tok_yes = clob_yes_token(mkt)
        if not tok_yes:
            continue

        # De-duplicate: skip if a YES trade for this token+date already recorded
        existing = session.query(TradeSimulation).filter_by(
            location_id=location_id,
            market_date=target_date,
            clob_token_id=tok_yes,
            order_type='YES',
        ).first()
        if existing:
            print(f"   ⏭️  Already placed YES for {mkt.get('question','')[:50]}")
            continue

        yes_ask = get_yes_ask(tok_yes)
        if yes_ask is None:
            print(f"   ⚠️  Could not fetch YES ask for {mkt.get('question','')[:50]}")
            continue

        if yes_ask >= YES_PRICE_CAP:
            print(f"   ⏸️  YES ask={yes_ask:.3f} ≥ {YES_PRICE_CAP} — retry next hour  "
                  f"q={mkt.get('question','')[:50]}")
            continue

        payout = round(SIM_SIZE_USD / yes_ask, 2)
        sim = TradeSimulation(
            forecast_id=forecast.get('db_id'),
            location_id=location_id,
            mode=mode,
            market_date=target_date,
            clob_token_id=tok_yes,
            question_text=mkt.get('question', '')[:512],
            market_side='in_band',
            order_type='YES',
            price=yes_ask,
            size=SIM_SIZE_USD,
            simulated_pnl=None,
        )
        session.add(sim)
        print(f"   📝 Sim YES in-band: ask={yes_ask:.3f}  cost=${SIM_SIZE_USD:.2f}  "
              f"payout=${payout:.2f} if wins  q={mkt.get('question','')[:50]}")


def main(pending_only: bool = False) -> None:
    print("=" * 70)
    print("Polymarket Dry Run — Market State Recorder")
    print("=" * 70)

    session = init_database(auto_migrate=True)

    # Load the most recent forecast for each location
    from sqlalchemy import func
    subq = (
        session.query(
            Forecast.location_id,
            Forecast.mode,
            func.max(Forecast.created_at).label('latest'),
        )
        .group_by(Forecast.location_id, Forecast.mode)
        .subquery()
    )
    forecasts = (
        session.query(Forecast)
        .join(subq, (Forecast.location_id == subq.c.location_id) &
                    (Forecast.mode == subq.c.mode) &
                    (Forecast.created_at == subq.c.latest))
        .all()
    )

    if not forecasts:
        print("⚠️  No forecasts in DB. Run ecmwf_forecast_pipeline.py first.")
        session.close()
        return

    print(f"\n🔍 Processing {len(forecasts)} forecast locations…")

    for fc in forecasts:
        if pending_only:
            tz_name = LOCATION_TIMEZONES.get(fc.location_id, 'UTC')
            now_local = datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo(tz_name))
            target_date = (now_local + datetime.timedelta(days=1)).date()
            has_trade = session.query(TradeSimulation).filter_by(
                location_id=fc.location_id,
                market_date=target_date,
                mode=fc.mode,
                order_type='YES',
            ).first()
            if has_trade:
                continue  # city already filled — skip on retry pass
                
        forecast_dict = {
            'db_id': fc.id,
            'location_id': fc.location_id,
            'mode': fc.mode,
            'name': fc.location_name,
            'forecast_temp': float(fc.forecast_temp),
            'lower_band': float(fc.lower_band),
            'upper_band': float(fc.upper_band),
            'units': fc.units,
            'ecmwf_run': fc.ecmwf_run,
            'horizon_hours': fc.horizon_hours,
        }
        record_dry_run(forecast_dict, session)

    session.commit()
    session.close()
    print("\n✅ Dry run recording complete")


def show_recent() -> None:
    session = init_database()
    trades = (
        session.query(TradeSimulation)
        .order_by(TradeSimulation.timestamp.desc())
        .limit(30)
        .all()
    )
    if not trades:
        print("No recent temperature trade simulations found in database.")
        return
        
    print("\n" + "="*120)
    print("  TEMPERATURE EXECUTOR HISTORY (Recent 30)")
    print("="*120)
    print(f"  {'ID':>5} | {'Date':>19} | {'City':>12} | {'Mode':>4} | {'Mkt Date':>10} | {'Type':>4} | {'Price':>6} | {'Size':>6} | {'PnL':>8} | {'Question'}")
    print("-" * 120)
    for t in trades:
        date_str = t.timestamp.strftime('%Y-%m-%d %H:%M:%S') if t.timestamp else "unknown"
        mkt_date = str(t.market_date) if t.market_date else "unknown"
        price = float(t.price) if t.price is not None else 0.0
        pnl_str = f"${float(t.simulated_pnl):+.2f}" if t.simulated_pnl is not None else "pending"
        q_trunc = t.question_text[:35] + "..." if t.question_text and len(t.question_text) > 35 else (t.question_text or "")
        print(f"  {t.id:>5} | {date_str} | {t.location_id:>12} | {getattr(t, 'mode', 'max').upper():>4} | {mkt_date:>10} | {t.order_type:>4} | {price:>6.3f} | ${float(t.size):>5.2f} | {pnl_str:>8} | {q_trunc}")
    print("=" * 120 + "\n")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pending-only", action="store_true")
    ap.add_argument("--status", action="store_true", help="Show recent execution history and exit")
    args = ap.parse_args()
    
    if args.status:
        show_recent()
    else:
        main(pending_only=args.pending_only)
