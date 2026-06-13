#!/usr/bin/env python3
"""
settle_precip_trades.py — Detect Polymarket resolution and settle precip_strategy_trades.

Queries the Gamma API for each open position past its settlement month, then
marks wins/losses in the DB. Does not touch the CTF contract — redemption is
handled by redeem_precip_wins.py.

Usage:
    python scripts/settle_precip_trades.py           # dry-run (print only)
    python scripts/settle_precip_trades.py --live    # write to DB
"""

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GAMMA_API  = "https://gamma-api.polymarket.com"
DB_DSN     = "dbname=gmgn_trading"


# ── DB helpers ────────────────────────────────────────────────────────────────

def _connect():
    import psycopg2
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def _ensure_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE public.precip_strategy_trades
            ADD COLUMN IF NOT EXISTS settled_at TIMESTAMP
        """)


def _open_positions(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, location_id, clob_token_no, size_usd,
                   COALESCE(fill_price, limit_price) AS fill_price
            FROM   public.precip_strategy_trades
            WHERE  resolved_win IS NULL
              AND  settlement_month < CURRENT_DATE
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _settle(conn, row_id: int, win: bool, pnl: float) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.precip_strategy_trades
            SET    resolved_win = %s,
                   realized_pnl = %s,
                   settled_at   = %s
            WHERE  id = %s
        """, (win, round(pnl, 2), datetime.utcnow(), row_id))


# ── Gamma API ─────────────────────────────────────────────────────────────────

def _fetch_resolution(token_no: str) -> dict | None:
    """
    Return resolution dict with keys 'resolved', 'outcome' (True=YES/False=NO),
    or None if the market hasn't resolved yet.
    """
    url = f"{GAMMA_API}/markets"
    params = {"clob_token_ids": token_no}
    for attempt in range(2):
        try:
            r = httpx.get(url, params=params, timeout=15)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3)
                continue
            r.raise_for_status()
            markets = r.json()
            if not markets:
                return None
            mkt = markets[0]
            # Gamma returns closed + resolutionOutcome when resolved
            if not mkt.get("closed") and not mkt.get("resolved"):
                return None
            outcome_str = (
                mkt.get("resolutionOutcome")
                or mkt.get("resolution_outcome")
                or ""
            ).strip().lower()
            if outcome_str not in ("yes", "no"):
                return None   # unrecognised or still pending
            return {"outcome": outcome_str == "yes", "question": mkt.get("question", "")}
        except Exception as exc:
            print(f"   ⚠  Gamma API error for token {token_no[:16]}…: {exc}")
            return None
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main(live: bool = False) -> None:
    conn = _connect()
    _ensure_columns(conn)

    positions = _open_positions(conn)
    if not positions:
        print("✅ No open positions past settlement date — nothing to settle.")
        return

    print(f"{'[DRY-RUN] ' if not live else ''}Checking {len(positions)} open position(s) past settlement…\n")

    # Deduplicate tokens — IDs 5 and 9 share the same market token
    seen_tokens: dict[str, dict | None] = {}

    settled = lost = 0
    for pos in positions:
        token = pos["clob_token_no"]
        if token not in seen_tokens:
            seen_tokens[token] = _fetch_resolution(token)
        resolution = seen_tokens[token]

        size  = float(pos["size_usd"])
        price = float(pos["fill_price"])
        shares = size / price if price else 0.0

        if resolution is None:
            print(f"   [ {pos['id']:>3} ] {pos['location_id']:<10}  ⏳ Not yet resolved on Polymarket — skip")
            continue

        win = resolution["outcome"]
        pnl = (shares - size) if win else -size   # win: shares * $1 - cost; loss: -cost

        tag    = "WIN  🏆" if win else "LOSS 💸"
        action = "→ would mark" if not live else "→ marking"
        print(
            f"   [ {pos['id']:>3} ] {pos['location_id']:<10}  {tag}  "
            f"pnl={pnl:+.2f}  {action} resolved_win={'true' if win else 'false'}"
        )

        if live:
            _settle(conn, pos["id"], win, pnl)

        if win:
            settled += 1
        else:
            lost += 1

    total = settled + lost
    if total:
        print(f"\n{'Updated' if live else 'Would update'} {total} position(s): {settled} win(s), {lost} loss(es).")
    else:
        print("\nNo positions resolved yet — run again after Polymarket settles the markets.")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Settle resolved precip positions in DB")
    parser.add_argument("--live", action="store_true", help="Write results to DB (default: dry-run)")
    args = parser.parse_args()
    main(live=args.live)
