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

# Dual-bucket entry price gates (mirror weatherbot-ts src/strategy.ts).
# Floor applies to the neighbour only — the forecast-centre bucket F is always
# kept regardless of price. Ceiling applies to both legs (a forecast-centre
# priced above it makes the dual entry −EV → abort).
BUCKET_MIN_PRICE = 0.12
BUCKET_MAX_PRICE = 0.35

# Provider-accuracy gate (mirror weatherbot-ts src/strategy.ts, added 2026-06-16).
# Refuse to trade a city/mode whose matrix provider carries a debiased MAE wider than
# one bucket. Unit-specific: US markets quote 2°F buckets, non-US markets quote 1°C
# buckets, so the °C ceiling is tighter. A city with NO usable matrix MAE is treated as
# unproven and SKIPPED (we do not trade a provider whose error is unmeasured).
MAX_PROVIDER_MAE_F = 1.5
MAX_PROVIDER_MAE_C = 1.0

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

def _bucket_width(question: str) -> float:
    """
    Polymarket temperature buckets are a fixed width keyed on the unit:
      °F (US) markets are 2° wide; °C (non-US) markets are 1° wide.
    Detect from the question text; default to 2 (°F) since the only Celsius
    shape in this market set is the single-degree "be X°C" form.
    """
    q = question.lower()
    if '°c' in q or 'celsius' in q:
        return 1.0
    return 2.0


def parse_temp_range(
    question: str,
) -> tuple[float | None, float | None, float | None]:
    """
    Extract a temperature bucket from a market question as a half-open interval.

    Returns (lo, hi, width) where, for a finite bucket, the interval is the
    HALF-OPEN range [lo, hi) and ``hi == lo + width``:
      - range bucket  "between 62-63°F" -> (62.0, 64.0, 2.0)  [62,64)
      - single-degree "be 13°C"         -> (13.0, 14.0, 1.0)  [13,14)
    Tail markets keep their single bound and carry width=None:
      - "X°F or below" -> (None, X, None)   (inclusive of X)
      - "X°F or higher"-> (X, None, None)   (inclusive of X)
    Unparseable -> (None, None, None).

    A label names the LOWER bound; the bucket spans one full width above it, so
    "62-63" really resolves on a 62 or 63 reading => true interval [62, 64).
    """
    q = question.lower()
    width = _bucket_width(question)

    # "between X and Y" or "X-Y°" — a width-wide bucket whose label lower bound is X.
    m = re.search(r'between\s+(\d+\.?\d*)\s*(?:-|and)\s*(\d+\.?\d*)', q)
    if m:
        lo = float(m.group(1))
        return lo, lo + width, width
    # bare "54-55°f" / "19-20°c" style range
    m = re.search(r'(\d+)\s*-\s*(\d+)°', question)
    if m:
        lo = float(m.group(1))
        return lo, lo + width, width

    # "below X" / "under X" / "X°F or below" / "X or lower" — tail, inclusive of X.
    # The \s*°?[a-z]* allows for unit suffixes like °F / °C between the number and "or"
    m = re.search(r'(?:below|under|less than)\s+(\d+\.?\d*)', q)
    if m:
        return None, float(m.group(1)), None
    m = re.search(r'(\d+\.?\d*)\s*°?[a-z]*\s+or\s+(?:below|under|lower|less)', q)
    if m:
        return None, float(m.group(1)), None

    # "above X" / "over X" / "X°F or higher" / "X or above" — tail, inclusive of X.
    m = re.search(r'(?:above|over|more than|higher than|at least)\s+(\d+\.?\d*)', q)
    if m:
        return float(m.group(1)), None, None
    m = re.search(r'(\d+\.?\d*)\s*°?[a-z]*\s+or\s+(?:above|over|higher|more)', q)
    if m:
        return float(m.group(1)), None, None

    # Single-degree format: "be 26°C" / "be 26°" (Hong Kong / Shanghai style).
    # The label names the lower bound of a width-wide (1°C) bucket: "be 13°C"
    # resolves on a reading in [13, 14).
    m = re.search(r'\bbe\s+(\d+\.?\d*)°', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return v, v + width, width

    return None, None, None


def bucket_contains(
    lo: float | None,
    hi: float | None,
    width: float | None,
    actual: float,
) -> bool:
    """
    The single definition of whether an actual reading resolves a bucket YES.

    Finite buckets are HALF-OPEN [lo, hi): an "62-63°F" bucket ([62,64)) wins on
    a 62 or 63 reading but NOT 64 (which is the next bucket's). Tail markets keep
    inclusive bounds: "or higher" wins on actual >= lo, "or below" on actual <= hi.
    """
    if lo is not None and hi is not None:
        return lo <= actual < hi
    if lo is not None:
        return actual >= lo
    if hi is not None:
        return actual <= hi
    return False


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
        low, high, width = parse_temp_range(question)
        if low is None and high is None:
            continue

        try:
            prices = json.loads(mkt.get('outcomePrices', '[0.5,0.5]') or '[0.5,0.5]')
            yes_p = float(prices[0])
        except Exception:
            yes_p = 0.0

        # high is the EXCLUSIVE upper bound, so (low+high)/2 is the true bucket
        # centre: "62-63°F" -> [62,64) -> 63; "be 13°C" -> [13,14) -> 13.5.
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
            'width':    width,
            'midpoint': midpoint,
            'distance': abs(midpoint - forecast_temp),
            'yes_price': yes_p,
        })

    candidates.sort(key=lambda x: x['distance'])
    return {'candidates': candidates, 'top2': candidates[:2]}


def select_entry_pair(
    candidates: list[dict],
    corrected_temp: float,
    min_price: float,
    max_price: float,
) -> dict:
    """F + higher-priced-neighbour entry selection (ported from weatherbot-ts).

    Buy the forecast-centre bucket F (the bucket whose half-open interval contains
    `corrected_temp`) plus the single adjacent neighbour (F±width) the MARKET prices
    higher. `candidates` are classify_markets() dicts (carry range/width/yes_price).

    Price gates (mirror weatherbot-ts):
      - floor (min_price) applies to the NEIGHBOUR only — F is always kept, even
        when priced below the floor (dropping the cheap-but-correct forecast bucket
        was the 2026-06-14 Dallas loss).
      - ceiling (max_price) applies to BOTH legs — a forecast-centre priced above
        the ceiling makes the dual entry −EV, so abort the city (no cheaper-neighbour
        fallback, per spec).

    Returns {"ok": True, "pair": [F, neighbour]} (F first) or
            {"ok": False, "reason": str}.
    """
    F = next(
        (c for c in candidates if bucket_contains(c['range'][0], c['range'][1], c['width'], corrected_temp)),
        None,
    )
    if F is None:
        return {"ok": False, "reason": f"forecast {corrected_temp:.1f}° outside all listed buckets"}

    f_lo, f_hi = F['range']
    width = F['width']
    # Neighbour lower bounds, by arithmetic on the half-open grid. A finite F has
    # lo and hi=lo+width; its neighbours start at f_lo-width (below) and f_hi (above).
    # A tail F has only one open side: "or higher" (lo set, hi None) → lower neighbour
    # ends at f_lo; "or below" (lo None, hi set) → upper neighbour starts at f_hi.
    def _is_neighbour(c: dict) -> bool:
        c_lo, c_hi = c['range']
        if c is F:
            return False
        if f_lo is not None and f_hi is not None:       # finite F
            return c_lo == f_lo - (width or 0) or c_lo == f_hi
        if f_lo is not None:                            # "or higher" tail → neighbour just below
            return c_hi == f_lo
        return c_lo == f_hi                             # "or below" tail → neighbour just above

    neighbours = [c for c in candidates if _is_neighbour(c)]
    if not neighbours:
        return {"ok": False, "reason": f"no neighbour bucket adjacent to F {F['range']}"}

    # Higher YES price wins — "let the market decide".
    neighbour = max(neighbours, key=lambda c: c['yes_price'] if c['yes_price'] is not None else 0.0)

    # Ceiling applies to both legs; floor to the neighbour only.
    if F['yes_price'] is not None and F['yes_price'] > max_price:
        return {"ok": False, "reason": f"forecast bucket price ${F['yes_price']:.3f} > ceiling ${max_price}"}
    if neighbour['yes_price'] > max_price:
        return {"ok": False, "reason": f"neighbour price ${neighbour['yes_price']:.3f} > ceiling ${max_price}"}
    if neighbour['yes_price'] < min_price:
        return {"ok": False, "reason": f"neighbour price ${neighbour['yes_price']:.3f} < floor ${min_price}"}

    return {"ok": True, "pair": [F, neighbour]}


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
    # Guard: a matrix cell is only usable if its unit matches the forecast's
    # unit. The cell stores "F"/"C"; the forecast stores "fahrenheit"/"celsius".
    # A mis-keyed cell (e.g. a °C bias landing on a °F forecast) would silently
    # corrupt bucket selection — so on mismatch we ignore the cell and fall
    # through to static/0. Cells without a unit (legacy) are treated as matching.
    fc_unit = "C" if forecast['units'] == 'celsius' else "F"
    if matrix_cell is not None:
        cell_unit = matrix_cell.get("unit")
        if cell_unit is not None and cell_unit != fc_unit:
            print(f"   ⚠️  matrix cell unit {cell_unit} != forecast unit {fc_unit} "
                  f"for {location_id}/{mode} — ignoring matrix bias")
            matrix_cell = None

    # ── Provider-accuracy gate ────────────────────────────────────────────────
    # Skip the city/mode unless its matrix provider has a measured debiased MAE
    # within one bucket (unit-specific). No usable cell/MAE → unproven → skip.
    mae = None
    if matrix_cell is not None:
        mae = matrix_cell.get("mae_debiased")
        if not isinstance(mae, (int, float)):
            mae = matrix_cell.get("mae")
    if not isinstance(mae, (int, float)):
        print(f"   ⏭️  No proven provider MAE for {location_id}/{mode} — unproven, skipping")
        return
    mae_gate = MAX_PROVIDER_MAE_C if fc_unit == "C" else MAX_PROVIDER_MAE_F
    if mae > mae_gate:
        print(f"   ⏭️  Provider MAE too high for {location_id}/{mode} — "
              f"{mae:.2f}°{fc_unit} > {mae_gate}°{fc_unit} gate — skipping")
        return

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

    if not candidates:
        print(f"   ⚠️  No markets parsed to a temperature bucket")
        return

    # ── F + higher-priced-neighbour selection (ported from weatherbot-ts) ─────
    # Buy the forecast-centre bucket F + the adjacent neighbour the market prices
    # higher. F is exempt from the price floor; ceiling applies to both legs.
    selection = select_entry_pair(
        candidates, corrected_temp,
        min_price=BUCKET_MIN_PRICE, max_price=BUCKET_MAX_PRICE,
    )
    if not selection['ok']:
        print(f"   ⏭️  Selection: {selection['reason']}")
        return
    pair = selection['pair']

    def _rng_label(c):
        rng = c['range']
        return (f"[{rng[0]}, {rng[1]}]" if rng[0] is not None and rng[1] is not None
                else (f"≤{rng[1]}" if rng[0] is None else f"≥{rng[0]}"))

    print(f"   ℹ️  Found {len(candidates)} markets; selected F + higher-priced neighbour:")
    print(f"      F        {_rng_label(pair[0])} yes=${pair[0]['yes_price']:.3f}  q={pair[0]['question'][:50]}")
    print(f"      neighbour {_rng_label(pair[1])} yes=${pair[1]['yes_price']:.3f}  q={pair[1]['question'][:50]}")

    # ── Pre-fetch tokens + asks for both legs, then gate as a pair ────────────
    legs = []
    for idx, cand in enumerate(pair):
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
            market_side=('F' if leg['idx'] == 1 else 'neighbour'),
            order_type='YES',
            price=leg['yes_ask'],
            size=SIM_SIZE_USD,
            hours_to_peak=round(hours_to_peak, 2),
            simulated_pnl=None,
        )
        session.add(sim)
        tag = 'F' if leg['idx'] == 1 else 'neighbour'
        print(f"   📝 Leg {leg['idx']} ({tag}): distance={leg['cand']['distance']:.2f}° ask=${leg['yes_ask']:.3f} "
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
