#!/usr/bin/env python3
"""
Per-city shadow→live promotion for the TS weather bot.

The TS bot trades every configured city, but only cities marked "live" in city_status.json
may place real-money orders (see src/cityStatus.ts). New cities start in "shadow": the bot
evaluates them and writes signal snapshots, but never spends. This script reads those
snapshots, simulates the dual-bucket PnL each city WOULD have earned, and promotes a city to
"live" once it has enough resolved samples that clear a profitability bar.

Win determination uses `actual_temp` (reliably back-filled by backfill_snapshot_actuals.py for
any city in CITY_META) vs the snapshot's two bucket ranges — NOT `winning_range`, which is only
populated when a real WIN trade exists and is therefore always NULL for shadow cities.

Run nightly, AFTER backfill_snapshot_actuals.py. Idempotent. Never auto-demotes a live city
(demotion is intentionally out of scope; flip back to shadow by hand if needed).

  python3 promote_cities.py            # compute + write city_status.json
  python3 promote_cities.py --dry-run  # print the table, write nothing
"""

import os
import re
import sys
import json
import datetime
from pathlib import Path

sys.path.insert(0, str(Path.home() / "sniff_test_polymarket"))
from dotenv import load_dotenv
load_dotenv(Path.home() / "sniff_test_polymarket" / ".env")

from database_schema import init_database, WeatherBotSignalSnapshot

STATUS_FILE = Path(__file__).parent / "city_status.json"

# Cities already trading live before the shadow framework existed — always live.
GRANDFATHERED_LIVE = {"nyc", "chicago", "miami", "dallas", "seattle", "atlanta"}

# Full known universe (mirrors nws.ts LOCATIONS). Cities seen in snapshots are added too.
KNOWN_CITIES = GRANDFATHERED_LIVE | {
    "houston", "denver", "los-angeles", "san-francisco", "austin",
}

# Promotion gate (tunable via env).
MIN_SAMPLES = int(os.getenv("PROMOTE_MIN_SAMPLES", "10"))   # resolved distinct markets per city
MIN_PNL     = float(os.getenv("PROMOTE_MIN_PNL", "0.0"))    # simulated net $ over those samples

POSITION_SIZE = 2.0  # $ per bucket, matches FIXED_POSITION_SIZE in strategy.ts


def in_range(actual: float, rng: str) -> bool:
    """Does an integer-rounded actual temperature fall inside a snapshot bucket range?
    Range formats from _fmtRange in strategy.ts: 'le{n}', 'ge{n}', '{lo}-{hi}' (inclusive)."""
    if not rng:
        return False
    m = re.fullmatch(r"le(\d+)", rng)
    if m:
        return actual <= float(m.group(1))
    m = re.fullmatch(r"ge(\d+)", rng)
    if m:
        return actual >= float(m.group(1))
    m = re.fullmatch(r"(\d+)-(\d+)", rng)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return lo <= actual <= hi
    return False


def leg_pnl(actual: float, rng: str, price) -> float | None:
    """Simulated PnL for one $2 bucket leg. None if the leg is unpriceable."""
    if price is None:
        return None
    p = float(price)
    if p <= 0 or p > 1:
        return None
    shares = POSITION_SIZE / p
    if in_range(actual, rng):
        return shares * 1.0 - POSITION_SIZE   # YES pays $1/share at resolution
    return -POSITION_SIZE


def compute() -> dict[str, dict]:
    session = init_database()
    try:
        rows = (
            session.query(WeatherBotSignalSnapshot)
            .filter(WeatherBotSignalSnapshot.actual_temp.isnot(None))
            .all()
        )
    finally:
        session.close()

    # Collapse to one decision per (city, mode, market_date): prefer an entered row,
    # otherwise the latest snapshot before resolution. Multiple ticks snapshot the same market.
    best: dict[tuple, WeatherBotSignalSnapshot] = {}
    for s in rows:
        key = (s.city, s.mode, s.market_date)
        cur = best.get(key)
        if cur is None:
            best[key] = s
            continue
        # entered wins; else later snapped_at wins
        if bool(s.entered) and not bool(cur.entered):
            best[key] = s
        elif bool(s.entered) == bool(cur.entered) and (s.snapped_at or 0) > (cur.snapped_at or 0):
            best[key] = s

    agg: dict[str, dict] = {}
    for s in best.values():
        city = s.city
        a = agg.setdefault(city, {"samples": 0, "wins": 0, "pnl": 0.0, "mae_sum": 0.0, "mae_n": 0})
        actual = float(s.actual_temp)

        p1 = leg_pnl(actual, s.bucket1_range or "", s.bucket1_price)
        p2 = leg_pnl(actual, s.bucket2_range or "", s.bucket2_price)
        if p1 is None and p2 is None:
            continue  # market we couldn't price either leg on — skip as a sample

        a["samples"] += 1
        a["pnl"] += (p1 or 0.0) + (p2 or 0.0)
        won = in_range(actual, s.bucket1_range or "") or in_range(actual, s.bucket2_range or "")
        if won:
            a["wins"] += 1
        if s.nws_forecast is not None:
            a["mae_sum"] += abs(float(s.nws_forecast) - actual)
            a["mae_n"] += 1

    return agg


def build_status(agg: dict[str, dict], existing: dict[str, dict]) -> dict[str, dict]:
    now_iso = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    out: dict[str, dict] = {}
    cities = KNOWN_CITIES | set(agg.keys()) | set(existing.keys())

    for city in sorted(cities):
        a = agg.get(city, {"samples": 0, "wins": 0, "pnl": 0.0, "mae_sum": 0.0, "mae_n": 0})
        samples = a["samples"]
        sim_pnl = round(a["pnl"], 2)
        hit_rate = round(a["wins"] / samples, 4) if samples else None
        mae = round(a["mae_sum"] / a["mae_n"], 2) if a["mae_n"] else None

        prev = existing.get(city, {})
        was_live = prev.get("status") == "live"
        promoted_at = prev.get("promoted_at")

        # Grandfathered cities and any already-live city stay live (no auto-demotion).
        if city in GRANDFATHERED_LIVE or was_live:
            status = "live"
            if promoted_at is None and city not in GRANDFATHERED_LIVE:
                promoted_at = prev.get("promoted_at") or now_iso
        elif samples >= MIN_SAMPLES and sim_pnl > MIN_PNL:
            status = "live"
            promoted_at = now_iso
        else:
            status = "shadow"
            promoted_at = None

        out[city] = {
            "status": status,
            "resolved_samples": samples,
            "sim_pnl": sim_pnl,
            "hit_rate": hit_rate,
            "forecast_mae": mae,
            "promoted_at": promoted_at,
        }
    return out


def load_existing() -> dict[str, dict]:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except Exception:
            return {}
    return {}


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    agg = compute()
    existing = load_existing()
    status = build_status(agg, existing)

    print(f"{'city':14} {'status':7} {'n':>3} {'sim_pnl':>9} {'hit':>6} {'mae':>5}  {'promoted_at'}")
    for city, st in status.items():
        hit = f"{st['hit_rate']*100:.0f}%" if st["hit_rate"] is not None else "  -"
        mae = f"{st['forecast_mae']:.1f}" if st["forecast_mae"] is not None else "  -"
        flip = ""
        if existing.get(city, {}).get("status") != st["status"]:
            flip = "  <-- " + (existing.get(city, {}).get("status") or "new") + "→" + st["status"]
        print(f"{city:14} {st['status']:7} {st['resolved_samples']:>3} "
              f"{st['sim_pnl']:>9.2f} {hit:>6} {mae:>5}  {st['promoted_at'] or '-'}{flip}")

    if dry_run:
        print(f"\nDry-run: would write {STATUS_FILE} (gate: n≥{MIN_SAMPLES} AND sim_pnl>{MIN_PNL}). No write.")
        return

    STATUS_FILE.write_text(json.dumps(status, indent=2) + "\n")
    n_live = sum(1 for s in status.values() if s["status"] == "live")
    print(f"\n✅ Wrote {STATUS_FILE} — {n_live}/{len(status)} cities live "
          f"(gate: n≥{MIN_SAMPLES} AND sim_pnl>{MIN_PNL}).")


if __name__ == "__main__":
    main()
