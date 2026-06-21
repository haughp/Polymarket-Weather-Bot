#!/usr/bin/env python3
"""
Stage-1 read-only analysis: is the ECMWF weather bot's win rate a
*bucket-width-vs-forecast-precision* problem, or a forecast-accuracy problem?

Hypothesis (the "narrow bucket mismatch" the experiment_log_ecmwf.md has flagged
every day for 11+ days): a trade wins essentially only when the forecast error in
degrees is smaller than the bucket's half-width. Where the bucket is narrower than
the forecast's irreducible error, no amount of provider/data-quality work can help
— the fix would have to be at the trade-SELECTION layer (only trade where
bucket_half_width >= k * typical_error).

This script does NOT change any code, config, daemon, or DB row. It only SELECTs
and prints a report. Run:  python analysis/bucket_width_margin_test.py

What it computes, per SETTLED trade (simulated_pnl IS NOT NULL):
  forecast_error   = |forecast_temp - actual_temp|        (degrees, same unit as the market)
  bucket_half_width= half the width of the bet bucket parsed from question_text
                     - POINT markets ("be 23°C")  -> half-width 0.5  (1°-wide integer bucket)
                     - RANGE markets ("76-77°F")  -> half-width (hi-lo)/2 + 0.5 rounding margin
  margin_ratio     = bucket_half_width / forecast_error
  won              = simulated_pnl > 0

Two views:
  (A) DESCRIPTIVE  — win rate binned by realized margin_ratio. Tests whether the
      relationship even exists (monotonic step near ratio ~= 1 => hypothesis holds;
      flat => it's really forecast quality, not bucket width).
  (B) FORWARD-USABLE — replaces the per-trade (unknowable-at-trade-time) error with
      each city/mode's HISTORICAL mean-abs-error, and asks whether
      bucket_half_width / historical_MAE predicts the win rate of *future* trades.
      This is the version a real trade-selection gate could use.

Caveats printed at the end. Nothing here is a recommendation by itself — Stage 2
(threshold sweep + PnL) only runs if Stage 1 shows the step.
"""

import os
import sys
from collections import defaultdict

# Run from anywhere: the bot modules live in this file's parent directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("psycopg2 not available in this environment")

# Use the BOT'S OWN bucket parser — do NOT reimplement it. parse_temp_range
# returns the half-open interval [lo, lo+width); region width is encoded in
# _bucket_width: °F (US) markets are 2° wide, °C (non-US) markets are 1° wide.
# We use it ONLY to locate the bucket centre for the error metric; win/loss
# comes from the stored simulated_pnl (the live settler's ground truth).
from polymarket_dry_run import parse_temp_range  # noqa: E402

DB_URL = os.getenv(
    "DATABASE_URL", "postgresql://padraighaughey@localhost:5432/gmgn_trading"
)


def bucket_geometry(question_text):
    """
    From the bot's parse_temp_range, return (lo, hi, width, centre, half_width).
    The bucket is HALF-OPEN [lo, hi) with hi == lo + width, so:
      - centre     = lo + width/2     (NB: "be 13°C" centres on 13.5, not 13.0)
      - half_width = width/2          (region-dependent: 0.5 for US 1°, 1.0 for non-US 2°)
    Tail markets (one bound None) have no finite centre/width -> return None
    (they're open-ended longshots; the centre/error metric is undefined for them).
    """
    lo, hi, width = parse_temp_range(question_text or "")
    if lo is None or hi is None or width is None:
        return None
    centre = lo + width / 2.0
    return (lo, hi, width, centre, width / 2.0)


def fetch_rows(conn):
    """One row per settled trade joined to its forecast and the day's actual."""
    sql = """
    SELECT
        ts.id,
        ts.location_id,
        ts.mode,
        ts.market_side,
        ts.market_date,
        ts.question_text,
        ts.simulated_pnl,
        f.forecast_temp,
        CASE WHEN ts.mode = 'min' THEN o.actual_min_temp
             ELSE o.actual_max_temp END AS actual_temp
    FROM trade_simulations ts
    JOIN forecasts f       ON f.id = ts.forecast_id
    LEFT JOIN outcomes o   ON o.location_id = ts.location_id
                          AND o.date::date  = ts.market_date
    WHERE ts.simulated_pnl IS NOT NULL
    ORDER BY ts.market_date;
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql)
        return cur.fetchall()


def enrich(rows):
    """Attach bucket geometry, forecast_error (from CENTRE), half_width, ratio, won.

    forecast_error is measured as |forecast - bucket_centre|, NOT |forecast - label|.
    The earlier version measured from the label and mis-centred every bucket by
    width/2 (a "be 13°C" bucket really centres on 13.5, since it resolves on
    [13,14)), which smeared the relationship. won comes from the bot's own
    bucket_contains against the actual, cross-checked vs the stored pnl sign.
    """
    out = []
    skipped_no_actual = skipped_no_forecast = skipped_unparsed = skipped_tail = 0
    for r in rows:
        if r["actual_temp"] is None:
            skipped_no_actual += 1
            continue
        if r["forecast_temp"] is None:
            skipped_no_forecast += 1
            continue
        geom = bucket_geometry(r["question_text"])
        if geom is None:
            lo0, hi0, _w0 = parse_temp_range(r["question_text"] or "")
            if lo0 is None and hi0 is None:
                skipped_unparsed += 1
            else:
                skipped_tail += 1  # open-ended tail market (one bound None)
            continue
        lo, hi, width, centre, hw = geom
        fc = float(r["forecast_temp"])
        actual = float(r["actual_temp"])
        err = abs(fc - centre)  # distance from the bucket CENTRE, not the label
        # won is the GROUND TRUTH stored by the live settler. We do NOT recompute
        # it from bucket_contains here: market_side is a strategy ROLE
        # (top2_closest / upper / lower / in_band / neighbour / F), and the single
        # question_text per row does not let us reconstruct the settled token's
        # bucket for every role — the stored simulated_pnl already encodes it.
        won = float(r["simulated_pnl"]) > 0
        out.append(
            {
                "location": r["location_id"],
                "mode": r["mode"],
                "side": r["market_side"],
                "date": r["market_date"],
                "fmt": "nonUS_1deg" if width <= 1.0 else "US_2deg",
                "width": width,
                "half_width": hw,
                "forecast_error": err,
                "margin_ratio": (hw / err) if err > 0 else float("inf"),
                "won": won,
                "pnl": float(r["simulated_pnl"]),
            }
        )
    return out, {
        "no_actual": skipped_no_actual,
        "no_forecast": skipped_no_forecast,
        "unparsed": skipped_unparsed,
        "tail_skipped": skipped_tail,
    }


def wr(items):
    if not items:
        return (0, 0, 0.0, 0.0)
    n = len(items)
    w = sum(1 for x in items if x["won"])
    pnl = sum(x["pnl"] for x in items)
    return (n, w, w / n, pnl)


def bin_by(items, key, edges, labels):
    buckets = defaultdict(list)
    for x in items:
        v = x[key]
        placed = False
        for i, e in enumerate(edges):
            if v < e:
                buckets[labels[i]].append(x)
                placed = True
                break
        if not placed:
            buckets[labels[-1]].append(x)
    return buckets


def print_table(title, buckets, order):
    print(f"\n{title}")
    print(f"  {'bin':<18}{'n':>6}{'wins':>6}{'win_rate':>10}{'tot_pnl':>12}")
    print("  " + "-" * 52)
    for lbl in order:
        n, w, rate, pnl = wr(buckets.get(lbl, []))
        if n == 0:
            continue
        print(f"  {lbl:<18}{n:>6}{w:>6}{rate:>9.1%}{pnl:>12.2f}")


def main():
    conn = psycopg2.connect(DB_URL)
    try:
        rows = fetch_rows(conn)
    finally:
        conn.close()

    items, skipped = enrich(rows)

    print("=" * 60)
    print("ECMWF bucket-width vs forecast-precision — Stage 1 (read-only)")
    print("=" * 60)
    print(f"settled trades pulled : {len(rows)}")
    print(f"usable (joined+parsed): {len(items)}")
    print(
        f"skipped               : no_actual={skipped['no_actual']} "
        f"no_forecast={skipped['no_forecast']} unparsed={skipped['unparsed']} "
        f"tail_open_ended={skipped['tail_skipped']}"
    )

    n, w, rate, pnl = wr(items)
    print(f"\noverall win rate      : {rate:.1%} ({w}/{n}), tot_pnl={pnl:.2f}")

    by_fmt = defaultdict(list)
    for x in items:
        by_fmt[x["fmt"]].append(x)
    print_table(
        "Win rate by bucket width (non-US °C = 1° wide vs US °F = 2° wide):",
        by_fmt,
        ["nonUS_1deg", "US_2deg"],
    )

    # --- VIEW A: descriptive, realized margin_ratio --------------------------
    # The hypothesis: win rate is near-0 when ratio < 1 (error exceeds the bucket)
    # and steps UP as ratio crosses ~1. A flat profile refutes the bucket story.
    edges = [0.5, 0.75, 1.0, 1.5, 2.0]
    labels = ["<0.5", "0.5-0.75", "0.75-1.0", "1.0-1.5", "1.5-2.0", ">=2.0"]
    by_ratio = bin_by(items, "margin_ratio", edges, labels)
    print_table(
        "VIEW A — win rate by REALIZED margin_ratio (half_width / actual error):",
        by_ratio,
        labels,
    )

    # Also bin by raw forecast error to show the 0.5° cliff directly.
    err_edges = [0.5, 1.0, 1.5, 2.0, 3.0]
    err_labels = ["<=0.5", "0.5-1.0", "1.0-1.5", "1.5-2.0", "2.0-3.0", ">3.0"]
    by_err = bin_by(items, "forecast_error", err_edges, err_labels)
    print_table(
        "VIEW A' — win rate by RAW forecast error (degrees):", by_err, err_labels
    )

    # --- VIEW B: forward-usable, historical-MAE margin ----------------------
    # Replace the unknowable per-trade error with each (location, mode)'s mean
    # absolute error over the whole sample. A gate could only ever use a number
    # like this (known at trade time), so this is the honest predictive test.
    mae = defaultdict(list)
    for x in items:
        mae[(x["location"], x["mode"])].append(x["forecast_error"])
    mae_lookup = {k: sum(v) / len(v) for k, v in mae.items()}

    for x in items:
        m = mae_lookup[(x["location"], x["mode"])]
        x["margin_ratio_hist"] = (x["half_width"] / m) if m > 0 else float("inf")

    by_ratio_h = bin_by(items, "margin_ratio_hist", edges, labels)
    print_table(
        "VIEW B — win rate by FORWARD margin_ratio (half_width / city-mode historical MAE):",
        by_ratio_h,
        labels,
    )

    # Per city/mode: MAE vs win rate — shows which combos are structurally tradeable
    print("\nPer city/mode: historical MAE vs win rate (sorted by MAE):")
    print(f"  {'location/mode':<24}{'n':>5}{'MAE':>8}{'win_rate':>10}{'tot_pnl':>12}")
    print("  " + "-" * 59)
    per = defaultdict(list)
    for x in items:
        per[(x["location"], x["mode"])].append(x)
    for (loc, mode), xs in sorted(per.items(), key=lambda kv: mae_lookup[kv[0]]):
        n2, w2, rate2, pnl2 = wr(xs)
        if n2 < 5:
            continue
        print(
            f"  {loc + '/' + mode:<24}{n2:>5}{mae_lookup[(loc, mode)]:>8.2f}"
            f"{rate2:>9.1%}{pnl2:>12.2f}"
        )

    # --- interpretation guide ------------------------------------------------
    print("\n" + "=" * 60)
    print("HOW TO READ THIS")
    print("=" * 60)
    print(
        "VIEW A confirms the hypothesis IF win rate climbs monotonically with\n"
        "margin_ratio and the <1.0 bins are near-0%. If it's flat, the bucket-width\n"
        "story is wrong and it's a forecast-quality problem after all.\n"
        "\n"
        "VIEW B is the one that matters for a real gate: it uses only info known at\n"
        "trade time (city/mode historical MAE + the bucket width from the question).\n"
        "If VIEW B ALSO steps up with ratio, then a selection rule of the form\n"
        "  'only trade where bucket_half_width >= k * historical_MAE'\n"
        "is justified — and Stage 2 should sweep k and report surviving-subset PnL.\n"
        "\n"
        "CAVEATS:\n"
        " - Bucket width is fixed per region: non-US °C markets are 1° wide\n"
        "   (half_width 0.5), US °F markets are 2° wide (half_width 1.0). Within a\n"
        "   region the width is constant, so margin_ratio varies only through\n"
        "   forecast error — the width LEVER is really the US-vs-non-US choice, not\n"
        "   a per-trade dial. Read the by-width table to see whether the wider US 2°\n"
        "   buckets actually win more (they should, if the bucket-width story holds).\n"
        " - historical MAE here is full-sample (mild look-ahead). A deployable gate\n"
        "   must use a TRAILING MAE; this Stage-1 pass is to see if the signal\n"
        "   exists at all before paying for the rolling-window plumbing.\n"
        " - outcomes join is location_id + date; rows with no verified outcome are\n"
        "   skipped (count printed above) — check that skip count isn't masking a\n"
        "   whole city/era.\n"
    )


if __name__ == "__main__":
    main()
