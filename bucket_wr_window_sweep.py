#!/usr/bin/env python3
"""
Bucket win-rate backfill + debias-window sweep.

Backfills the dashboard's "1-bucket vs 2-bucket" win-rate comparison from
archived forecasts + actuals (so N grows from ~1-12 into the hundreds), and
sweeps the debias window (3/7/14/30 days) as a second axis.

Win-rate only — no Polymarket prices needed. Win = actual temperature lands in
the chosen bucket's half-open [lo, hi). Settlement truth uses the SAME actuals
routing production grades on (US -> IEM CLI / `outcomes`; non-US -> `outcomes`),
reused from provider_backtest.

Method (per city/mode, verified against the live system):
  - Provider is FIXED to the deployed one in provider_matrix.json[city][mode].
  - bias_W(D) = mean(actual - forecast) over [D-W, D-1]  (point-in-time, the
    same mean-error correction provider_backtest.score() computes at 30d).
  - corrected = raw_forecast + bias_W(D);  F = floor-aligned bucket of corrected.
  - Leg 2 = in-bucket-position neighbour (corrected in HIGH half of F -> upper,
    LOW half -> lower), mirroring polymarket_dry_run.select_entry_pair.

See docs/superpowers/specs/2026-06-25-weather-bucket-wr-window-sweep-design.md
(in the sniff_test_polymarket repo).

Usage:
    python bucket_wr_window_sweep.py                 # all cities, 120 days
    python bucket_wr_window_sweep.py --days 180
    python bucket_wr_window_sweep.py --cities beijing,tokyo,paris
    python bucket_wr_window_sweep.py --json out.json
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections import defaultdict
from typing import Optional

# ── Pure logic (unit-tested; no I/O) ─────────────────────────────────────────

WINDOWS = (3, 7, 14, 30)


def width_for(unit: str) -> float:
    """Polymarket bucket width in the city's native unit: 1°C non-US, 2°F US."""
    return 1.0 if unit.upper().startswith("C") else 2.0


def bucket_lo(t: float, width: float) -> float:
    """Floor-aligned lower edge of the half-open [lo, lo+width) bucket holding t.

    Matches the real market boundaries: °C buckets are 1° integer-aligned
    ([30,31)); °F buckets are 2° even-aligned ([66,68))."""
    import math
    return math.floor(t / width) * width


def in_bucket(actual: float, lo: float, width: float) -> bool:
    """True if actual lands in the half-open [lo, lo+width) — how markets resolve."""
    return lo <= actual < lo + width


def neighbour_lo(corrected: float, f_lo: float, width: float) -> float:
    """Lower edge of the in-bucket-position 2nd-leg neighbour.

    corrected in the HIGH half of F's interval (pos >= 0.5) -> upper neighbour
    (f_lo + width); LOW half -> lower neighbour (f_lo - width). Mirrors
    polymarket_dry_run.select_entry_pair (commit 92aeb22)."""
    pos = (corrected - f_lo) / width
    return f_lo + width if pos >= 0.5 else f_lo - width


def trailing_bias(
    err_by_date: dict[datetime.date, float],
    target: datetime.date,
    window: int,
    min_samples: int,
) -> Optional[float]:
    """Mean (actual - forecast) error over [target-window, target-1].

    Point-in-time: strictly before `target`, no lookahead. Returns None if
    fewer than `min_samples` errors are available in the window."""
    lo = target - datetime.timedelta(days=window)
    hi = target - datetime.timedelta(days=1)
    errs = [e for d, e in err_by_date.items() if lo <= d <= hi]
    if len(errs) < min_samples:
        return None
    return sum(errs) / len(errs)


def evaluate_day(
    raw_forecast: float, actual: float, bias: float, width: float
) -> tuple[bool, bool, float]:
    """Apply bias, bucket, pick neighbour, settle both legs against `actual`.

    Returns (win_F, win_neighbour, abs_debiased_error)."""
    corrected = raw_forecast + bias
    f_lo = bucket_lo(corrected, width)
    n_lo = neighbour_lo(corrected, f_lo, width)
    win_f = in_bucket(actual, f_lo, width)
    win_n = in_bucket(actual, n_lo, width)
    return win_f, win_n, abs(corrected - actual)


def sweep_city_mode(
    series: list[tuple[datetime.date, float, float]],
    unit: str,
    min_samples: int = 5,
    windows: tuple[int, ...] = WINDOWS,
) -> dict[int, dict]:
    """Walk-forward window sweep for one (city, mode).

    series: sorted list of (date, raw_forecast, actual), native unit.
    Returns {window: {n, f_wins, leg_wins, legs, derr_sum, derr_n}}.
    """
    width = width_for(unit)
    err_by_date = {d: a - f for d, f, a in series}
    out = {w: {"n": 0, "f_wins": 0, "leg_wins": 0, "legs": 0,
               "derr_sum": 0.0, "derr_n": 0} for w in windows}
    for target, raw_f, actual in series:
        for w in windows:
            # A W-day window can hold at most W daily samples, so the production
            # 30d floor (min_samples=5) is unsatisfiable for short windows. Scale
            # the floor to the window so 3d/7d populate (and reveal their noise).
            eff_min = min(min_samples, w)
            bias = trailing_bias(err_by_date, target, w, eff_min)
            if bias is None:
                continue
            win_f, win_n, derr = evaluate_day(raw_f, actual, bias, width)
            agg = out[w]
            agg["n"] += 1
            agg["f_wins"] += int(win_f)
            agg["legs"] += 2
            agg["leg_wins"] += int(win_f) + int(win_n)
            agg["derr_sum"] += derr
            agg["derr_n"] += 1
    return out


def finalize(agg: dict) -> dict:
    """Turn raw counters into reportable rates."""
    n, legs = agg["n"], agg["legs"]
    return {
        "n": n,
        "one_bucket_wr": (agg["f_wins"] / n) if n else None,
        "two_bucket_wr": (agg["leg_wins"] / legs) if legs else None,
        "avg_debiased_err": (agg["derr_sum"] / agg["derr_n"]) if agg["derr_n"] else None,
    }


# ── I/O wiring (reuses provider_backtest helpers) ────────────────────────────

def _load_helpers():
    """Imported lazily so the pure logic + tests need no network/DB/deps."""
    import provider_backtest as pb
    return pb


def deployed_provider(matrix: dict, city: str, mode: str) -> Optional[str]:
    cell = (matrix.get("cities", {}).get(city) or {}).get(mode) or {}
    return cell.get("provider")


def build_series(
    pb, city: str, cfg: dict, mode: str, provider: str,
    start: datetime.date, end: datetime.date, warmup: int,
) -> list[tuple[datetime.date, float, float]]:
    """Fetch forecast + actuals over [start-warmup, end] and zip into a sorted
    (date, raw_forecast, actual) series for the given mode."""
    fetch_start = start - datetime.timedelta(days=warmup)
    fc = pb.fetch_forecasts(cfg, fetch_start, end, lead=1, models=[provider])
    by_date = fc.get(provider, {})

    if cfg.get("db_actuals"):
        loc = pb._DB_LOC.get(city, city)
        actuals = pb.fetch_actuals_db(loc, fetch_start, end)
    elif city in pb.US_IN_OUTCOMES:
        loc = pb._DB_LOC.get(city, city)
        actuals = pb.fetch_actuals_db(loc, fetch_start, end)
    else:
        actuals = pb.fetch_actuals(cfg["station"], fetch_start, end)

    fk, ak = ("fmax", "high") if mode == "max" else ("fmin", "low")
    series = []
    for d_iso, fvals in sorted(by_date.items()):
        a = actuals.get(d_iso)
        if not a or a.get(ak) is None or fvals.get(fk) is None:
            continue
        d = datetime.date.fromisoformat(d_iso)
        series.append((d, float(fvals[fk]), float(a[ak])))
    return series


def run(cities: list[str], days: int, min_samples: int) -> dict:
    pb = _load_helpers()
    matrix = json.load(open(pb.MATRIX_PATH))
    end = datetime.date.today() - datetime.timedelta(days=1)
    start = end - datetime.timedelta(days=days)
    warmup = max(WINDOWS)

    results: dict = {"generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                     "range": {"start": start.isoformat(), "end": end.isoformat()},
                     "windows": list(WINDOWS), "min_samples": min_samples,
                     "cities": {}}

    for city in cities:
        cfg = pb.CITIES.get(city)
        if not cfg:
            print(f"  {city}: not in provider_backtest.CITIES — skip", file=sys.stderr)
            continue
        for mode in ("max", "min"):
            provider = deployed_provider(matrix, city, mode)
            if not provider:
                continue
            try:
                series = build_series(pb, city, cfg, mode, provider, start, end, warmup)
            except Exception as e:  # noqa: BLE001 — report and continue
                print(f"  {city}/{mode}: fetch failed ({e}) — skip", file=sys.stderr)
                continue
            if not series:
                continue
            swept = sweep_city_mode(series, cfg["unit"], min_samples)
            results["cities"].setdefault(city, {})[mode] = {
                "provider": provider, "unit": cfg["unit"],
                "series_days": len(series),
                "windows": {str(w): finalize(a) for w, a in swept.items()},
            }
            print(f"  {city}/{mode} ({provider}): {len(series)} days", file=sys.stderr)
    return results


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x*100:4.0f}%" if x is not None else "  – "


def print_report(results: dict) -> None:
    rng = results["range"]
    print(f"\nBucket WR window sweep  ·  {rng['start']} → {rng['end']}  "
          f"·  min_samples={results['min_samples']}\n")
    pooled = {w: {"f_wins": 0, "n": 0, "leg_wins": 0, "legs": 0}
              for w in results["windows"]}
    pooled_region = {"US": dict(pooled), "non-US": {w: {"f_wins": 0, "n": 0, "leg_wins": 0, "legs": 0} for w in results["windows"]}}

    for city in sorted(results["cities"]):
        for mode in ("max", "min"):
            cell = results["cities"][city].get(mode)
            if not cell:
                continue
            unit = "°" + cell["unit"].upper()
            print(f"{city} / {mode}   (provider={cell['provider']}, n_days={cell['series_days']})")
            print(f"  {'window':>6}  {'1bkt_WR':>8}  {'2bkt_WR':>8}  {'N':>4}  {'avg_dbias_err':>13}")
            for w in results["windows"]:
                r = cell["windows"][str(w)]
                tag = "  <- deployed" if w == 30 else ""
                err = f"{r['avg_debiased_err']:.2f}{unit}" if r["avg_debiased_err"] is not None else "–"
                print(f"  {str(w)+'d':>6}  {_fmt_pct(r['one_bucket_wr'])}  "
                      f"{_fmt_pct(r['two_bucket_wr'])}  {r['n']:>4}  {err:>13}{tag}")
            print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=120, help="lookback window (default 120)")
    ap.add_argument("--cities", default="", help="comma-separated subset (default: all)")
    ap.add_argument("--min-samples", type=int, default=5,
                    help="min trailing samples to estimate a window's bias (default 5)")
    ap.add_argument("--json", default="", help="write full results JSON to this path")
    args = ap.parse_args()

    pb = _load_helpers()
    cities = ([c.strip() for c in args.cities.split(",") if c.strip()]
              or list(pb.CITIES.keys()))

    results = run(cities, args.days, args.min_samples)
    print_report(results)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
