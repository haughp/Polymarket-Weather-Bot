#!/usr/bin/env python3
"""Aggregate captured precip forecasts into the precipitation provider matrix.

The precip analogue of provider_backtest.py — but a forward-CAPTURE aggregator,
not an archive replay (there is no archived precip-forecast source; see
capture_precip_ensemble.py). It joins captured PrecipProviderForecast snapshots
to resolved Polymarket bucket ladders and verified PrecipOutcome actuals, scores
each (city, provider) on Brier-on-bucket-hit (primary) + MAE-mm / bias-mm /
bucket-hit, then selects the best provider per city and writes
precip_provider_matrix.json.

Selection (mirrors build_matrix() in provider_backtest.py, adapted for monthly
cadence):
  - MIN_SAMPLES=8 months for an INDEPENDENT per-city pick; below that, fall back
    to a cross-city POOLED ranking (precip skill is mostly model-physics) and
    flag the cell pooled=True.
  - IMPROVEMENT_THRESHOLD=0.10 (10%) hysteresis on Brier vs the incumbent.
  - eligibility encodes BOTH data confidence (HK/Seoul ERA5 truth is biased
    27-32% → never eligible) AND maturity (pooled/thin cells are observation-only
    and must not drive live trades).

Usage:
    python3 precip_provider_backtest.py --months 12 --matrix
    python3 precip_provider_backtest.py --no-matrix   # score only, don't write JSON
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from collections import defaultdict

from database_schema import (
    init_database,
    PrecipProviderForecast,
    PrecipProviderPerformance,
    PrecipOutcome,
)
from precip_forecast_pipeline import PRECIP_LOCATIONS, MM_TO_INCHES
from precip_backtest import fetch_resolved_precip_markets
from polymarket_precip_dry_run import parse_precip_range

MATRIX_PATH = os.path.join(os.path.dirname(__file__), "precip_provider_matrix.json")
PROVIDERS = ["ecmwf_ifs025", "ecmwf_aifs025", "gfs025", "icon_seamless", "gem_global"]

MIN_SAMPLES = 8            # months for an independent per-city pick
IMPROVEMENT_THRESHOLD = 0.10  # 10% Brier improvement required to switch incumbent


# ── Bucket / metric math (pure functions — unit-tested in test_precip_matrix.py) ──

def assign_bucket(value: float, buckets: list[tuple[float | None, float | None]]) -> int | None:
    """Index of the bucket whose [low, high) range contains `value`, else None.

    Buckets are (low, high) in native units; None = open-ended (-inf / +inf).
    """
    for i, (lo, hi) in enumerate(buckets):
        low = lo if lo is not None else float("-inf")
        high = hi if hi is not None else float("inf")
        if low <= value < high or (hi is None and value >= low) or (lo is None and value < high):
            return i
    return None


def ensemble_bucket_probs(p05: float, p50: float, p95: float,
                          buckets: list[tuple[float | None, float | None]]) -> list[float]:
    """Probability mass per bucket from a 3-point ensemble summary (p05/p50/p95).

    Approximates the ensemble CDF with a piecewise-linear fit through the three
    quantiles (0.05, 0.50, 0.95) and integrates the bucket edges against it. This
    is the precip analogue of using the member spread as the probability source.
    """
    pts = sorted([(p05, 0.05), (p50, 0.50), (p95, 0.95)])
    xs = [x for x, _ in pts]
    qs = [q for _, q in pts]

    def cdf(x: float) -> float:
        if x <= xs[0]:
            # extrapolate below p05 toward 0 over a symmetric tail width
            width = max(1e-6, xs[1] - xs[0])
            return max(0.0, qs[0] - (xs[0] - x) / width * qs[0])
        if x >= xs[-1]:
            width = max(1e-6, xs[-1] - xs[-2])
            return min(1.0, qs[-1] + (x - xs[-1]) / width * (1.0 - qs[-1]))
        for i in range(len(xs) - 1):
            if xs[i] <= x <= xs[i + 1]:
                span = max(1e-6, xs[i + 1] - xs[i])
                frac = (x - xs[i]) / span
                return qs[i] + frac * (qs[i + 1] - qs[i])
        return 0.5

    probs = []
    for lo, hi in buckets:
        low = lo if lo is not None else float("-inf")
        high = hi if hi is not None else float("inf")
        p = cdf(high) - cdf(low)
        probs.append(max(0.0, p))
    total = sum(probs)
    if total <= 0:
        return [1.0 / len(buckets)] * len(buckets)
    return [p / total for p in probs]


def brier_for_sample(probs: list[float], winning_idx: int) -> float:
    """Multiclass Brier score for one sample: sum_k (p_k - onehot_k)^2."""
    return sum((p - (1.0 if k == winning_idx else 0.0)) ** 2 for k, p in enumerate(probs))


# ── Scoring ───────────────────────────────────────────────────────────────────

def _to_native(value_mm: float, units: str) -> float:
    return value_mm * MM_TO_INCHES if units == "inches" else value_mm


def score_provider_city(session, location_id: str, provider: str,
                        bucket_ladders: dict, months_back: int) -> dict | None:
    """Score one (city, provider) over the captured-forecast history.

    Returns {samples, brier, mae_mm, bias_mm, bucket_hit} or None if no samples.
    `bucket_ladders` maps (location_id, settlement_month_iso) -> list[(low,high)].
    """
    obs = PRECIP_LOCATIONS[location_id]
    units = obs["units"]
    cutoff = (datetime.date.today().replace(day=1) - datetime.timedelta(days=1)).replace(day=1)

    rows = (
        session.query(PrecipProviderForecast)
        .filter_by(location_id=location_id, provider=provider)
        .all()
    )
    abs_errs_mm, signed_errs_mm, briers, hits = [], [], [], []
    n = 0
    for r in rows:
        sm = r.settlement_month
        if sm > cutoff:
            continue  # month not yet resolved
        months_delta = (cutoff.year - sm.year) * 12 + (cutoff.month - sm.month)
        if months_delta >= months_back:
            continue

        outcome = (
            session.query(PrecipOutcome)
            .filter_by(location_id=location_id, settlement_month=sm)
            .first()
        )
        if not outcome or outcome.actual_precip is None:
            continue
        actual_native = float(outcome.actual_precip)  # stored in native units
        # forecast totals are stored in mm
        total_mm = float(r.total_forecast_mm)
        actual_mm = actual_native / MM_TO_INCHES if units == "inches" else actual_native

        abs_errs_mm.append(abs(total_mm - actual_mm))
        signed_errs_mm.append(total_mm - actual_mm)

        ladder = bucket_ladders.get((location_id, sm.isoformat()))
        if ladder:
            win_idx = assign_bucket(actual_native, ladder)
            if win_idx is not None:
                p05 = _to_native(float(r.remaining_p05_mm), units)
                p50 = _to_native(total_mm, units)
                p95 = _to_native(float(r.remaining_p95_mm), units)
                # remaining_p05/p95 are the remaining-month band; total uses p50 as
                # the centre. Reconstruct a month-end band around the total.
                band_lo = total_mm - (float(r.remaining_p50_mm) - float(r.remaining_p05_mm))
                band_hi = total_mm + (float(r.remaining_p95_mm) - float(r.remaining_p50_mm))
                probs = ensemble_bucket_probs(
                    _to_native(band_lo, units), p50, _to_native(band_hi, units), ladder
                )
                briers.append(brier_for_sample(probs, win_idx))
                pred_idx = assign_bucket(p50, ladder)
                hits.append(1.0 if pred_idx == win_idx else 0.0)
        n += 1

    if n == 0:
        return None
    return {
        "samples": n,
        "brier": round(sum(briers) / len(briers), 4) if briers else None,
        "mae_mm": round(sum(abs_errs_mm) / len(abs_errs_mm), 3) if abs_errs_mm else None,
        "bias_mm": round(sum(signed_errs_mm) / len(signed_errs_mm), 3) if signed_errs_mm else None,
        "bucket_hit": round(sum(hits) / len(hits), 4) if hits else None,
    }


def _inflate(metric: float | None, samples: int) -> float:
    """Small-sample inflation: thin cells look less trustworthy to the gate."""
    if metric is None:
        return float("inf")
    if samples >= MIN_SAMPLES:
        return metric
    return metric * (MIN_SAMPLES / max(1, samples)) ** 0.5


# ── Selection ─────────────────────────────────────────────────────────────────

def build_matrix(scores: dict, incumbent: dict) -> dict:
    """Pick the best provider per city with pooled fallback + hysteresis.

    `scores`: {city: {provider: metrics}}. `incumbent`: prior matrix cities dict.
    """
    # Pooled cross-city ranking by Brier (then mae_mm), excluding low-confidence
    # ERA5 cities so a biased-truth city can't distort the pool.
    pool = defaultdict(lambda: {"brier": [], "mae_mm": []})
    for city, provs in scores.items():
        if PRECIP_LOCATIONS[city].get("data_confidence") == "low":
            continue
        for prov, m in provs.items():
            if m["brier"] is not None:
                pool[prov]["brier"].append(m["brier"])
            if m["mae_mm"] is not None:
                pool[prov]["mae_mm"].append(m["mae_mm"])
    pooled_rank = sorted(
        pool.items(),
        key=lambda kv: (
            sum(kv[1]["brier"]) / len(kv[1]["brier"]) if kv[1]["brier"] else float("inf"),
            sum(kv[1]["mae_mm"]) / len(kv[1]["mae_mm"]) if kv[1]["mae_mm"] else float("inf"),
        ),
    )
    pooled_best = pooled_rank[0][0] if pooled_rank else None

    out = {}
    for city, provs in scores.items():
        is_low_conf = PRECIP_LOCATIONS[city].get("data_confidence") == "low"
        # Independent pick: providers with enough samples AND a Brier, ranked by Brier.
        independent = sorted(
            [(p, m) for p, m in provs.items()
             if m["samples"] >= MIN_SAMPLES and m["brier"] is not None],
            key=lambda kv: kv[1]["brier"],
        )

        if independent:
            best_prov, best_m = independent[0]
            pooled = False
            # Hysteresis vs incumbent.
            inc = incumbent.get(city, {})
            inc_prov, inc_brier = inc.get("provider"), inc.get("brier")
            if inc_prov and inc_prov in provs and provs[inc_prov]["brier"] is not None:
                if best_m["brier"] >= provs[inc_prov]["brier"] * (1 - IMPROVEMENT_THRESHOLD):
                    best_prov, best_m = inc_prov, provs[inc_prov]
        else:
            # Pooled fallback (observation-only): assign the pool winner if scored here.
            best_prov = pooled_best if pooled_best in provs else (
                max(provs, key=lambda p: provs[p]["samples"]) if provs else None
            )
            best_m = provs.get(best_prov)
            pooled = True

        if best_prov is None or best_m is None:
            continue

        eligible = (not is_low_conf) and (not pooled) and best_m["samples"] >= MIN_SAMPLES
        cell = {
            "provider": best_prov,
            "brier": best_m["brier"],
            "mae_mm": best_m["mae_mm"],
            "bias_mm": best_m["bias_mm"],
            "bucket_hit": best_m["bucket_hit"],
            "samples": best_m["samples"],
            "pooled": pooled,
            "eligible": eligible,
        }
        if is_low_conf:
            cell["data_confidence"] = "low"
        out[city] = cell
    return out


def load_incumbent() -> dict:
    if os.path.exists(MATRIX_PATH):
        with open(MATRIX_PATH) as f:
            return json.load(f).get("cities", {})
    return {}


def build_bucket_ladders(months_back: int) -> dict:
    """(location_id, settlement_month_iso) -> sorted list of (low, high) buckets.

    Sourced from resolved Polymarket precip markets (same fetch precip_backtest
    uses), so the ladder matches what each month actually settled against.
    """
    ladders = defaultdict(list)
    for rm in fetch_resolved_precip_markets(limit=1500):
        lo, hi = parse_precip_range(rm.question)
        if lo is None and hi is None:
            continue
        ladders[(rm.location_id, rm.settlement_month.isoformat())].append((lo, hi))
    # Sort each ladder by low edge for stable bucket indices.
    return {
        k: sorted(v, key=lambda b: (b[0] if b[0] is not None else float("-inf")))
        for k, v in ladders.items()
    }


def run(months_back: int = 12, write_matrix: bool = True) -> dict:
    session = init_database(auto_migrate=False)
    ladders = build_bucket_ladders(months_back)

    scores = {}
    for city in PRECIP_LOCATIONS:
        scores[city] = {}
        for prov in PROVIDERS:
            m = score_provider_city(session, city, prov, ladders, months_back)
            if m is not None:
                scores[city][prov] = m

    incumbent = load_incumbent()
    cities = build_matrix(scores, incumbent)

    # Persist per-(city, provider) performance rows for the audit trail.
    now = datetime.datetime.utcnow()
    for city, provs in scores.items():
        sel = cities.get(city, {})
        for prov, m in provs.items():
            session.add(PrecipProviderPerformance(
                location_id=city, provider=prov, window_months=months_back,
                samples=m["samples"], brier=m["brier"], mae_mm=m["mae_mm"],
                bias_mm=m["bias_mm"], bucket_hit=m["bucket_hit"],
                pooled=bool(sel.get("pooled")) if sel.get("provider") == prov else False,
                eligible=bool(sel.get("eligible")) if sel.get("provider") == prov else False,
                evaluated_at=now,
            ))
    session.commit()
    session.close()

    payload = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_months": months_back,
        "asof_day": 18,
        "metric": "brier",
        "providers": PROVIDERS,
        "cities": cities,
    }
    if write_matrix:
        with open(MATRIX_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"✅ wrote {MATRIX_PATH} ({len(cities)} city cells)")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the precipitation provider matrix.")
    ap.add_argument("--months", type=int, default=12, help="rolling window (months back)")
    ap.add_argument("--matrix", default=True, action=argparse.BooleanOptionalAction,
                    help="write precip_provider_matrix.json (default: True)")
    args = ap.parse_args()
    payload = run(months_back=args.months, write_matrix=args.matrix)

    print("\nProvider matrix (per city):")
    if not payload["cities"]:
        print("  (empty — no resolved samples yet; matrix is cold. Bot stays on default provider.)")
    for city, cell in payload["cities"].items():
        flag = "ELIGIBLE" if cell["eligible"] else ("pooled" if cell["pooled"] else "thin")
        print(f"  {city:10s} {cell['provider']:14s} brier={cell['brier']} "
              f"mae_mm={cell['mae_mm']} hit={cell['bucket_hit']} n={cell['samples']} [{flag}]")


if __name__ == "__main__":
    main()
