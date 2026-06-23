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
from execution.weather_executor import WeatherExecutor

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

# provider_forecasts.city uses 'nyc' for New York; the runtime keys everything
# else by location_id, which equals provider_forecasts.city. Only NYC diverges.
PROVIDER_FORECASTS_CITY: dict[str, str] = {"new_york": "nyc"}


def pf_city(location_id: str) -> str:
    """Map a runtime location_id to its provider_forecasts.city key."""
    return PROVIDER_FORECASTS_CITY.get(location_id, location_id)


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

# Per-(location, mode) live/shadow gate, mirroring weatherbot-ts's city_status.json
# (cityStatus.ts). Fail-safe: a missing file or missing key means shadow — an
# unlisted combo must never place a real order.
_LIVE_STATUS_PATH = os.path.join(os.path.dirname(__file__), "ecmwf_live_status.json")


def _load_live_status() -> dict:
    """Load ecmwf_live_status.json. Returns {} if absent or unreadable."""
    if os.path.exists(_LIVE_STATUS_PATH):
        with open(_LIVE_STATUS_PATH) as f:
            return json.load(f)
    return {}


def _is_live(location_id: str, mode: str, status: dict) -> bool:
    """Decide whether a (location_id, mode) combo may trade with real money."""
    entry = status.get(f"{location_id}:{mode}")
    if not entry:
        return False
    return entry.get("status") == "live"


# Mirrors weatherbot-ts strategy.ts's already-held guard (lines 606-617): don't add
# a 3rd leg for the same city/date if forecast drift already produced 2 legs.
MAX_LEGS_PER_CITY_DATE = 2

# Global cap across all live (location, mode) combos — mirrors config.max_open_positions
# in weatherbot-ts. Limits aggregate exposure regardless of how many combos are live.
MAX_OPEN_LIVE_POSITIONS = 8


def _count_open_legs(session, location_id: str, mode: str, market_date) -> int:
    """Count real (non-dry-run) held legs for this (location_id, mode, market_date).

    TradeSimulation rows are shared by every city/mode and by shadow combos too —
    must filter on mode and dry_run=False, or shadow rows (and other modes for the
    same city/date) saturate the cap and permanently block a live combo that has
    never actually held a position.
    """
    return (
        session.query(TradeSimulation)
        .filter_by(location_id=location_id, mode=mode, market_date=market_date, dry_run=False)
        .count()
    )


def _count_open_live_positions(session) -> int:
    """Count real (non-dry-run) held legs across every city/mode/date (global exposure cap)."""
    return session.query(TradeSimulation).filter_by(dry_run=False).count()


# Real-money safety: even with combos flagged "live" in ecmwf_live_status.json,
# WeatherExecutor stays in dry-run mode unless ECMWF_LIVE_TRADING=1 is explicitly
# set in the environment. This lets the live code path (gates, DB writes, logging)
# be exercised end-to-end with zero real-order risk before flipping the env var —
# the forced-dry-run verification step called for before any combo goes live.
_executor = WeatherExecutor(
    trade_size_usdc=SIM_SIZE_USD,
    dry_run=os.environ.get("ECMWF_LIVE_TRADING") != "1",
)


def _execute_leg(session, executor, location_id: str, mode: str, market_date,
                  token_id: str, price: float, size_usdc: float, status: dict):
    """
    Decide whether this leg should place a real order, and do so if eligible.

    Returns (is_live, order_id, success, error, fee_paid). For a shadow combo,
    is_live=False and the rest are None — the caller proceeds to record a plain
    simulated TradeSimulation row exactly as before. For a live combo, this
    enforces the already-held and global-exposure guards before calling
    executor.buy_yes_fok(), mirroring weatherbot-ts strategy.ts's willExecute
    block (lines 606-664, 681-732).
    """
    if not _is_live(location_id, mode, status):
        return False, None, None, None, None

    if _count_open_legs(session, location_id, mode, market_date) >= MAX_LEGS_PER_CITY_DATE:
        return True, None, False, "already_holding_max_legs", None

    if _count_open_live_positions(session) >= MAX_OPEN_LIVE_POSITIONS:
        return True, None, False, "max_open_live_positions_reached", None

    result = executor.buy_yes_fok(token_id, price, size_usdc=size_usdc)
    return True, result.order_id, result.success, result.error, result.fee_paid

# Dual-bucket entry price gates (mirror weatherbot-ts src/strategy.ts).
# Floor applies to the neighbour only — the forecast-centre bucket F is always
# kept regardless of price. Ceiling applies to both legs (a forecast-centre
# priced above it makes the dual entry −EV → abort).
BUCKET_MIN_PRICE = 0.12
BUCKET_MAX_PRICE = 0.35

# Provider-accuracy gate (mirror weatherbot-ts src/strategy.ts, added 2026-06-16).
# The real selection already happened in provider_backtest.py's build_matrix(): it only
# writes a (city, mode) cell when some provider's debiased MAE clears this same cap, so
# any cell read here is already proven. This check is therefore a defensive re-verify
# against a stale/hand-edited provider_matrix.json, not the primary gate — the primary
# gate is "does a cell exist at all" (see the unproven-skip just above this block).
# Unit-specific: US markets quote 2°F buckets, non-US markets quote 1°C buckets, so the
# °C ceiling is tighter.
MAX_PROVIDER_MAE_F = 1.5
MAX_PROVIDER_MAE_C = 1.0

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


def resolve_bias(location_id: str, mode: str, matrix_cell: dict | None):
    """Bias for the F decision = the matrix cell's bias ONLY (sign: corrected = raw + bias).
    Returns (bias_float, source_str) or None if there is no usable cell (caller skips)."""
    if matrix_cell is None:
        return None
    return float(matrix_cell.get("bias", 0.0)), f"matrix(provider={matrix_cell.get('provider','?')})"


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

    _bias = resolve_bias(location_id, mode, matrix_cell)
    if _bias is None:
        print(f"   ⏭️  No usable matrix cell for {location_id}/{mode} — skipping")
        return
    bias, source = _bias
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
    live_status = _load_live_status()
    for leg in legs:
        if leg['existing']:
            print(f"   ⏭️  Leg {leg['idx']}: already placed YES  q={leg['mkt'].get('question','')[:50]}")
            continue

        is_live, order_id, live_success, live_error, fee_paid = _execute_leg(
            session, _executor, location_id, mode, target_date,
            leg['tok_yes'], leg['yes_ask'], SIM_SIZE_USD, live_status,
        )
        if is_live and not live_success:
            print(f"   ❌ Leg {leg['idx']}: LIVE order blocked/failed ({live_error}) — not recorded  "
                  f"q={leg['mkt'].get('question','')[:50]}")
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
            order_id=order_id,
            dry_run=not is_live,
            live_success=live_success,
            live_error=live_error,
            fee_paid=fee_paid,
        )
        session.add(sim)
        tag = 'F' if leg['idx'] == 1 else 'neighbour'
        mode_tag = 'LIVE' if is_live else 'sim'
        print(f"   📝 Leg {leg['idx']} ({tag}, {mode_tag}): distance={leg['cand']['distance']:.2f}° ask=${leg['yes_ask']:.3f} "
              f"cost=${SIM_SIZE_USD:.2f} payout=${payout:.2f}  q={leg['mkt'].get('question','')[:50]}")


def main(pending_only: bool = False) -> None:
    print("=" * 70)
    print("Polymarket Dry Run — Market State Recorder")
    print("=" * 70)

    session = init_database(auto_migrate=not pending_only)

    # Load the most recent forecast for each location (metadata enrichment for the
    # matrix-provider path; record_dry_run's bias now comes from the matrix cell only)
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
        # Matrix-aware forecast fetch: the matrix must assign a non-NWS provider
        # with a provider_forecasts row for the target date, or we skip (defect 1b —
        # no forecasts-table fallback).
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
            """), {"city": pf_city(loc_id), "mode": mode_, "prov": provider,
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

        # Skip when the matrix names no usable provider or its forecast row is absent —
        # do NOT fall back to the forecasts-table aggregate (defect 1b).
        if fc is None:
            cell = matrix.get(loc_id, {}).get(mode_)
            if cell and cell.get("provider") not in (None, "nws"):
                print(f"   ⏭️  {loc_id}/{mode_}: matrix provider {cell.get('provider')} "
                      f"has no provider_forecasts row for target date — skipping")
            else:
                print(f"   ⏭️  {loc_id}/{mode_}: matrix cell has no usable provider — skipping")
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
