#!/usr/bin/env python3
"""
Polymarket Dry Run Execution Logger
Discovers active weather markets via Gamma API for each ECMWF forecast location,
records order book state, and simulates NO bets on both confidence-band tails.
"""

import os
import re
import sys
import json
import datetime
import httpx
from zoneinfo import ZoneInfo
from database_schema import init_database, Forecast, MarketState, MarketBucket, TradeSimulation

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

_PROVIDER_MATRIX: dict | None = None  # lazy-loaded once


def load_provider_matrix() -> dict:
    """Load provider_matrix.json from repo root. Returns {} if absent."""
    global _PROVIDER_MATRIX
    if _PROVIDER_MATRIX is not None:
        return _PROVIDER_MATRIX
    path = os.path.join(os.path.dirname(__file__), "provider_matrix.json")
    if os.path.exists(path):
        with open(path) as f:
            _PROVIDER_MATRIX = json.load(f).get("cities", {})
    else:
        _PROVIDER_MATRIX = {}
    return _PROVIDER_MATRIX

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

# Per-location, per-mode ECMWF forecast bias correction.
# Convention: bias = (expected_actual − ecmwf_forecast).
#   Positive → ECMWF runs cold (actual warmer) → shift bucket selection up.
#   Negative → ECMWF runs warm (actual cooler) → shift bucket selection down.
#
# STALE — May 2026 ECMWF-IFS-vs-ERA5 backtest, do not extend. Cold-start fallback only.
# Live overrides via _load_live_bias() are authoritative for any cell with
# ≥5 verified samples in the last 45 days. As of the 2026-06 IEM migration those
# live values are the median forecast-vs-airport-METAR error (the source Polymarket
# settles on), not ERA5 — so this static dict is now only used before a city has 5
# resolved samples.
FORECAST_BIAS: dict[str, dict[str, float]] = {
    #                max    min
    'new_york': {'max': +0.8, 'min': -0.1},
    'dallas':   {'max': +0.8, 'min':  0.0},
    'atlanta':  {'max': +1.1, 'min':  0.0},
}

# Frozen snapshot of static cells so per-trade telemetry can distinguish
# `source=static` from `source=live` after the live loader merges into FORECAST_BIAS.
_STATIC_BIAS_KEYS: set[tuple[str, str]] = {
    (loc, mode) for loc, modes in FORECAST_BIAS.items() for mode in modes
}

# Populated per main() call by _load_live_bias(). FORECAST_BIAS stays an immutable
# cold-start constant; live values live here so the static fallback is never clobbered.
LIVE_BIAS: dict[str, dict[str, float]] = {}    # (loc, mode) → live median bias
LIVE_BIAS_N: dict[str, dict[str, int]] = {}    # (loc, mode) → resolved-sample count


def _load_live_bias(session, min_samples: int = 5, days_back: int = 45) -> dict:
    """Return median(actual − forecast) per (location_id, mode) from resolved outcomes.

    Forecast bias is a slowly-varying *level*, not a fast signal, so a robust median
    over the full window beats a recency-weighted mean: the median ignores the
    occasional bad-reading outlier instead of letting it swing the correction by a
    whole bucket. Only cells with ≥ min_samples resolved days are included; others
    keep the static value (or 0 if not in FORECAST_BIAS).

    NOTE: ground truth comes from `outcomes`, which (post-2026-06 migration) is the
    airport-METAR reading Polymarket settles on — see outcome_backfiller.IEM_STATIONS.
    For IEM_EXCLUDED cities (hong_kong, shenzhen) `outcomes` is still ERA5, so their
    bias remains untrustworthy until a proper source is wired in.

    Returns: {location_id: {mode: (bias_degrees, n_samples)}}
    """
    from sqlalchemy import text
    sql = text(f"""
        WITH latest_per_date AS (
            SELECT DISTINCT ON (location_id, mode, DATE(peak_time))
                location_id, mode, forecast_temp, DATE(peak_time) AS fdate
            FROM forecasts
            WHERE peak_time > NOW() - INTERVAL '{days_back} days'
            ORDER BY location_id, mode, DATE(peak_time), ecmwf_run DESC
        ),
        errors AS (
            SELECT f.location_id, f.mode,
                   (CASE WHEN f.mode = 'max' THEN o.actual_max_temp
                         ELSE o.actual_min_temp END - f.forecast_temp) AS err
            FROM latest_per_date f
            JOIN outcomes o ON f.location_id = o.location_id AND f.fdate = DATE(o.date)
            WHERE o.verified = TRUE
        )
        SELECT location_id, mode,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY err) AS median_bias,
               COUNT(*) AS n
        FROM errors
        GROUP BY location_id, mode
        HAVING COUNT(*) >= :min_samples
    """)
    rows = session.execute(sql, {'min_samples': min_samples}).fetchall()
    live: dict = {}
    for loc, mode, bias, n in rows:
        live.setdefault(loc, {})[mode] = (round(float(bias), 1), int(n))
    return live


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
) -> dict:
    """
    Rank markets by midpoint distance to forecast; return all candidates plus
    the top-2 closest. Open-ended markets (above/below X) use the bound as the
    midpoint. Markets that don't parse to a temperature are skipped.
    """
    candidates = []

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

        if low is not None and high is not None:
            midpoint = (low + high) / 2.0
        elif low is not None:
            midpoint = low
        else:
            midpoint = high

        candidates.append({
            'market':   mkt,
            'question': question,
            'range':    (low, high),
            'midpoint': midpoint,
            'distance': abs(midpoint - forecast_temp),
            'yes_price': yes_p,
        })

    candidates.sort(key=lambda x: x['distance'])
    return {'candidates': candidates, 'top2': candidates[:2]}


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

ENTRY_WINDOW_OPEN_H  = 42     # Earliest hours-before-peak the bot will trade
ENTRY_WINDOW_CLOSE_H = 18     # Latest hours-before-peak the bot will trade


def record_dry_run(forecast: dict, session) -> None:
    """
    Spec: forecast → top-2 closest market buckets by midpoint distance →
    place YES on BOTH legs when current time is within [peak - 36h, peak - 30h].
    """
    location_id = forecast['location_id']
    mode = forecast.get('mode', 'max')
    slug = LOCATION_SLUGS.get(location_id, location_id.replace('_', '-'))
    units_label = '°F' if forecast['units'] == 'fahrenheit' else '°C'

    tz_name = LOCATION_TIMEZONES.get(location_id, 'UTC')
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(tz_name))
    target_date = (now_local + datetime.timedelta(days=1)).date()
    hours_to_peak: float | None = None

    # ── Entry-window gate ─────────────────────────────────────────────────────
    peak_time = forecast.get('peak_time')
    if peak_time is None:
        print(f"\n📊 {forecast['name']} ({mode.upper()}): peak_time missing — skip")
        return
    try:
        if peak_time.tzinfo is None:
            peak_utc = peak_time.replace(tzinfo=datetime.timezone.utc)
        else:
            peak_utc = peak_time.astimezone(datetime.timezone.utc)
        entry_open  = peak_utc - datetime.timedelta(hours=ENTRY_WINDOW_OPEN_H)
        entry_close = peak_utc - datetime.timedelta(hours=ENTRY_WINDOW_CLOSE_H)
        peak_local = peak_utc.astimezone(ZoneInfo(tz_name))

        print(f"\n📊 {forecast['name']} ({mode.upper()}): forecast={forecast['forecast_temp']}{units_label}  "
              f"peak={peak_local.strftime('%Y-%m-%d %H:%M %Z')}")
        print(f"   Entry window: {entry_open.astimezone(ZoneInfo(tz_name)).strftime('%Y-%m-%d %H:%M %Z')} "
              f"→ {entry_close.astimezone(ZoneInfo(tz_name)).strftime('%Y-%m-%d %H:%M %Z')}")

        hours_to_peak = (peak_utc - now_utc).total_seconds() / 3600.0
        if now_utc < entry_open or now_utc >= entry_close:
            print(f"   ⏳ Outside entry window (currently {hours_to_peak:+.1f}h to peak) — skip")
            return
    except Exception as exc:
        print(f"   ❌ {location_id}/{mode}: entry-gate computation failed: {exc!r}")
        return

    markets = fetch_event_markets(slug, target_date, mode)
    if not markets:
        print(f"   ⚠️  No Polymarket markets found for {location_id} on {target_date}")
        return

    raw_temp = float(forecast['forecast_temp'])
    matrix_cell = load_provider_matrix().get(location_id, {}).get(mode)
    if mode in LIVE_BIAS_N.get(location_id, {}):
        # Live outcome-based bias always wins (highest priority)
        bias = LIVE_BIAS.get(location_id, {}).get(mode, 0.0)
        source = f"live(n={LIVE_BIAS_N[location_id][mode]})"
    elif matrix_cell is not None:
        bias = matrix_cell.get("bias", 0.0)
        source = f"matrix(provider={matrix_cell.get('provider','?')})"
    elif (location_id, mode) in _STATIC_BIAS_KEYS:
        bias = FORECAST_BIAS.get(location_id, {}).get(mode, 0.0)
        source = "static"
    else:
        bias = 0.0
        source = "none"
    corrected_temp = raw_temp + bias
    units_label_inner = '°F' if forecast['units'] == 'fahrenheit' else '°C'
    print(f"   📐 Bias: raw={raw_temp:.1f}{units_label_inner} "
          f"applied={bias:+.1f}{units_label_inner} "
          f"corrected={corrected_temp:.1f}{units_label_inner} "
          f"({location_id}/{mode}, source={source})")
    classified = classify_markets(markets, corrected_temp)
    candidates = classified['candidates']
    top2 = classified['top2']

    if not candidates:
        print(f"   ⚠️  No markets parsed to a temperature bucket")
        return

    if len(top2) < 2:
        print(f"   ⚠️  Only {len(top2)} candidate(s) parsed — need 2 for dual-bucket — skip")
        return

    print(f"   ℹ️  Found {len(candidates)} markets; top 2 closest to forecast:")
    for c in candidates:
        marker = '➡️' if c in top2 else '  '
        rng = c['range']
        rng_label = f"[{rng[0]}, {rng[1]}]" if rng[0] is not None and rng[1] is not None else (
            f"≤{rng[1]}" if rng[0] is None else f"≥{rng[0]}"
        )
        print(f"   {marker} d={c['distance']:.2f}° mid={c['midpoint']} {rng_label}  "
              f"q={c['question'][:50]}")

    # ── Pre-fetch tokens + asks for both legs, then gate as a pair ────────────
    legs = []
    for idx, cand in enumerate(top2):
        mkt = cand['market']
        tok_yes = clob_yes_token(mkt)
        if not tok_yes:
            print(f"   ❌ Leg {idx+1}: no YES token — abort pair  q={mkt.get('question','')[:50]}")
            return

        existing = session.query(TradeSimulation).filter_by(
            location_id=location_id,
            market_date=target_date,
            clob_token_id=tok_yes,
            order_type='YES',
        ).first()

        yes_ask = get_yes_ask(tok_yes)
        if yes_ask is None:
            print(f"   ❌ Leg {idx+1}: no YES ask — abort pair  q={mkt.get('question','')[:50]}")
            return

        legs.append({
            'idx':      idx + 1,
            'cand':     cand,
            'mkt':      mkt,
            'tok_yes':  tok_yes,
            'yes_ask':  yes_ask,
            'existing': existing,
        })

    # Both legs pass — record them (skipping any that are already on the book)
    for leg in legs:
        if leg['existing']:
            print(f"   ⏭️  Leg {leg['idx']}: already placed YES  q={leg['mkt'].get('question','')[:50]}")
            continue

        payout = round(SIM_SIZE_USD / leg['yes_ask'], 2)
        sim = TradeSimulation(
            forecast_id=forecast.get('db_id'),
            location_id=location_id,
            mode=mode,
            market_date=target_date,
            clob_token_id=leg['tok_yes'],
            question_text=leg['mkt'].get('question', '')[:512],
            market_side='top2_closest',
            order_type='YES',
            price=leg['yes_ask'],
            size=SIM_SIZE_USD,
            hours_to_peak=round(hours_to_peak, 2),
            simulated_pnl=None,
        )
        session.add(sim)
        print(f"   📝 Leg {leg['idx']}: distance={leg['cand']['distance']:.2f}° ask=${leg['yes_ask']:.3f} "
              f"cost=${SIM_SIZE_USD:.2f} payout=${payout:.2f}  q={leg['mkt'].get('question','')[:50]}")


def main(pending_only: bool = False) -> None:
    print("=" * 70)
    print("Polymarket Dry Run — Market State Recorder")
    print("=" * 70)

    session = init_database(auto_migrate=not pending_only)

    # Refresh bias from resolved outcomes — live values override static fallback.
    # Stored in LIVE_BIAS / LIVE_BIAS_N (NOT written back into FORECAST_BIAS, which
    # stays an immutable cold-start constant for the `source=static` path).
    for loc, modes in _load_live_bias(session).items():
        for mode, (bias, n) in modes.items():
            LIVE_BIAS.setdefault(loc, {})[mode] = bias
            LIVE_BIAS_N.setdefault(loc, {})[mode] = n

    # Load the most recent forecast for each location (fallback path)
    from sqlalchemy import func, text as sa_text
    def _get_latest_from_forecasts(loc_id, mode_):
        subq = (
            session.query(
                Forecast.location_id,
                Forecast.mode,
                func.max(Forecast.created_at).label('latest'),
            )
            .filter(Forecast.location_id == loc_id, Forecast.mode == mode_)
            .group_by(Forecast.location_id, Forecast.mode)
            .subquery()
        )
        return (
            session.query(Forecast)
            .join(subq, (Forecast.location_id == subq.c.location_id) &
                        (Forecast.mode == subq.c.mode) &
                        (Forecast.created_at == subq.c.latest))
            .first()
        )

    # Collect all (location_id, mode) pairs from the forecasts table
    from sqlalchemy import distinct
    loc_modes = session.query(
        distinct(Forecast.location_id), Forecast.mode
    ).all()

    if not loc_modes:
        print("⚠️  No forecasts in DB. Run ecmwf_forecast_pipeline.py first.")
        session.close()
        return

    matrix = load_provider_matrix()
    print(f"\n🔍 Processing {len(loc_modes)} forecast locations…")

    for (loc_id, mode_) in loc_modes:
        # Matrix-aware forecast fetch: prefer provider_forecasts table when matrix
        # assigns a non-NWS provider; fall back to forecasts table otherwise.
        fc = None
        forecast_source_label = "forecasts table"
        cell = matrix.get(loc_id, {}).get(mode_)
        if cell and cell.get("provider") not in (None, "nws"):
            provider = cell["provider"]
            tz_name = LOCATION_TIMEZONES.get(loc_id, 'UTC')
            target_date = (
                datetime.datetime.now(datetime.timezone.utc)
                .astimezone(ZoneInfo(tz_name)) + datetime.timedelta(days=1)
            ).date()
            row = session.execute(sa_text("""
                SELECT forecast_temp FROM provider_forecasts
                WHERE city = :city AND mode = :mode AND provider = :prov
                  AND target_date = :tdate
                ORDER BY captured_at DESC LIMIT 1
            """), {"city": loc_id, "mode": mode_, "prov": provider,
                   "tdate": target_date}).fetchone()
            if row:
                # Build a synthetic forecast dict compatible with record_dry_run
                fc_base = _get_latest_from_forecasts(loc_id, mode_)
                if fc_base:
                    fc = type("FC", (), {
                        "id": fc_base.id,
                        "location_id": loc_id,
                        "mode": mode_,
                        "location_name": fc_base.location_name,
                        "forecast_temp": float(row[0]),
                        "units": fc_base.units,
                        "ecmwf_run": fc_base.ecmwf_run,
                        "peak_time": fc_base.peak_time,
                    })()
                    forecast_source_label = f"provider_forecasts({provider})"

        if fc is None:
            fc = _get_latest_from_forecasts(loc_id, mode_)
        if fc is None:
            continue

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
                continue  # already filled — skip on retry pass

        forecast_dict = {
            'db_id': fc.id,
            'location_id': fc.location_id,
            'mode': fc.mode,
            'name': fc.location_name,
            'forecast_temp': float(fc.forecast_temp),
            'units': fc.units,
            'ecmwf_run': fc.ecmwf_run,
            'peak_time': fc.peak_time,
            '_forecast_source': forecast_source_label,
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
