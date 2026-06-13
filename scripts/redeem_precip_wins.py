#!/usr/bin/env python3
"""
redeem_precip_wins.py — Redeem won precipitation positions back to USDC.

Flow:
  1. Find WIN positions in precip_strategy_trades (resolved_win=TRUE, redeemed_at IS NULL)
  2. Look up each market's conditionId via Gamma API using stored clob_token_no
  3. Fetch wallet's redeemable positions from data-api
  4. Match and call CTF.redeemPositions() for each
  5. Mark redeemed_at in DB

Usage:
    python scripts/redeem_precip_wins.py           # dry-run
    python scripts/redeem_precip_wins.py --live    # execute on-chain
"""

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=False)
load_dotenv(Path.home() / "sniff_test_polymarket" / ".env", override=False)

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
DB_DSN    = "dbname=gmgn_trading"

WALLET      = os.getenv("POLY_FUNDER_ADDRESS") or os.getenv("POLY_WALLET_ADDRESS", "")
PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
POLYGON_RPC = "https://polygon.drpc.org"

# Polygon contract addresses — same as sniff_test_polymarket/scripts/redeem_positions.py
CTF_CONTRACT        = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER    = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
NEG_RISK_EXCH_V1    = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_NAT            = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
PUSD                = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
USDC_E              = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NEG_RISK_COLLATERAL = "0x3A3BD7bb9528E159577F7C2e685CC81A765002E2"
HASH_ZERO           = b'\x00' * 32

CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id",      "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
    },
    {
        "name": "isApprovedForAll",
        "type": "function",
        "inputs": [
            {"name": "account",  "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
    },
    {
        "name": "setApprovalForAll",
        "type": "function",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]

NEG_RISK_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]


# ── Gnosis CTF helpers (mirrored from sniff_test_polymarket/scripts/redeem_positions.py) ──

def _gnosis_position_id(collateral_hex: str, condition_id_hex: str, index_set: int) -> int:
    from web3 import Web3
    cond   = bytes.fromhex(condition_id_hex.removeprefix("0x").zfill(64))
    idx    = index_set.to_bytes(32, "big")
    x1     = Web3.keccak(cond + idx)
    x1_int = int.from_bytes(x1, "big")
    odd    = (x1_int >> 255) != 0
    x2_int = x1_int & ((1 << 255) - 1)
    if odd:
        x2_int ^= ((1 << 256) - 1)
    collection_bytes = x2_int.to_bytes(32, "big")
    addr_bytes = bytes.fromhex(collateral_hex.removeprefix("0x").zfill(40))
    return int(Web3.keccak(addr_bytes + collection_bytes).hex(), 16)


def _is_neg_risk(pos: dict) -> bool:
    flag = pos.get("negativeRisk")
    if isinstance(flag, bool):
        return flag
    asset_id     = pos.get("asset")
    condition_id = pos.get("conditionId", "")
    if not asset_id or not condition_id:
        return False
    try:
        asset_int     = int(asset_id)
        outcome_index = int(pos.get("outcomeIndex", 0))
        index_set     = 1 if outcome_index == 0 else 2
        for collateral in [USDC_NAT, PUSD, USDC_E, NEG_RISK_COLLATERAL]:
            if asset_int == _gnosis_position_id(collateral, condition_id, index_set):
                return collateral == NEG_RISK_COLLATERAL
    except Exception:
        pass
    return False


def _detect_collateral(ctf, condition_id: str, outcome_index: int, wallet: str) -> str:
    index_set = 1 if outcome_index == 0 else 2
    for collateral in [USDC_NAT, PUSD, USDC_E, NEG_RISK_COLLATERAL]:
        try:
            pos_id = _gnosis_position_id(collateral, condition_id, index_set)
            bal    = ctf.functions.balanceOf(wallet, pos_id).call()
            if bal > 0:
                return collateral
        except Exception:
            continue
    return PUSD


# ── DB helpers ────────────────────────────────────────────────────────────────

def _connect():
    import psycopg2
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def _ensure_column(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE public.precip_strategy_trades
            ADD COLUMN IF NOT EXISTS redeemed_at TIMESTAMP
        """)


def _win_positions(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, location_id, clob_token_no, size_usd, fill_price
            FROM   public.precip_strategy_trades
            WHERE  resolved_win = TRUE
              AND  redeemed_at IS NULL
        """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _mark_redeemed(conn, row_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE public.precip_strategy_trades
            SET    redeemed_at = %s
            WHERE  id = %s
        """, (datetime.utcnow(), row_id))


# ── API helpers ───────────────────────────────────────────────────────────────

def _condition_id_for_token(token_no: str) -> str | None:
    """Look up the conditionId for a market via its NO token ID."""
    try:
        r = httpx.get(
            f"{GAMMA_API}/markets",
            params={"clob_token_ids": token_no},
            timeout=15,
        )
        r.raise_for_status()
        markets = r.json()
        if markets:
            return markets[0].get("conditionId") or markets[0].get("condition_id")
    except Exception as exc:
        print(f"   ⚠  Gamma API error: {exc}")
    return None


def _redeemable_positions(wallet: str) -> list[dict]:
    """Fetch all redeemable positions for the wallet from data-api."""
    try:
        r = httpx.get(
            f"{DATA_API}/positions",
            params={"user": wallet, "redeemable": "true"},
            timeout=20,
        )
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    except Exception as exc:
        print(f"   ⚠  data-api error: {exc}")
        return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main(live: bool = False) -> None:
    if not WALLET:
        print("❌ POLY_FUNDER_ADDRESS not set in environment — cannot redeem.")
        sys.exit(1)
    if live and not PRIVATE_KEY:
        print("❌ POLY_PRIVATE_KEY not set — cannot sign transactions.")
        sys.exit(1)

    conn = _connect()
    _ensure_column(conn)

    wins = _win_positions(conn)
    if not wins:
        print("✅ No WIN positions pending redemption.")
        conn.close()
        return

    print(f"{'[DRY-RUN] ' if not live else ''}Found {len(wins)} WIN position(s) to redeem.\n")

    # Build condition_id map (deduplicate tokens)
    condition_map: dict[str, str | None] = {}
    for pos in wins:
        tok = pos["clob_token_no"]
        if tok not in condition_map:
            condition_map[tok] = _condition_id_for_token(tok)
            time.sleep(0.3)

    # Fetch all redeemable positions from data-api
    redeemable = _redeemable_positions(WALLET)
    redeemable_by_condition: dict[str, dict] = {
        p.get("conditionId", "").lower(): p
        for p in redeemable
        if p.get("conditionId")
    }

    if live:
        from web3 import Web3
        w3  = Web3(Web3.HTTPProvider(POLYGON_RPC))
        acct = w3.eth.account.from_key(PRIVATE_KEY)
        ctf  = w3.eth.contract(address=Web3.to_checksum_address(CTF_CONTRACT), abi=CTF_ABI)
        neg_risk_exch = w3.eth.contract(
            address=Web3.to_checksum_address(NEG_RISK_EXCH_V1), abi=NEG_RISK_ABI
        )

    redeemed = 0
    for pos in wins:
        tok          = pos["clob_token_no"]
        condition_id = condition_map.get(tok)

        if not condition_id:
            print(f"   [ {pos['id']:>3} ] {pos['location_id']:<10}  ⚠  conditionId not found — skip")
            continue

        api_pos = redeemable_by_condition.get(condition_id.lower())
        if not api_pos:
            print(f"   [ {pos['id']:>3} ] {pos['location_id']:<10}  ⚠  not in data-api redeemable list — market may not be finalized yet")
            continue

        payout = float(api_pos.get("currentValue") or api_pos.get("payout") or api_pos.get("cashValue") or 0)
        neg    = _is_neg_risk(api_pos)
        print(
            f"   [ {pos['id']:>3} ] {pos['location_id']:<10}  {'NegRisk' if neg else 'Standard'}  "
            f"payout≈${payout:.2f}  conditionId={condition_id[:12]}…"
        )

        if not live:
            continue

        outcome_index = int(api_pos.get("outcomeIndex", 0))
        collateral    = _detect_collateral(ctf, condition_id, outcome_index, WALLET)
        cid_bytes     = bytes.fromhex(condition_id.removeprefix("0x").zfill(64))

        try:
            contract = neg_risk_exch if neg else ctf
            tx = contract.functions.redeemPositions(
                Web3.to_checksum_address(collateral),
                HASH_ZERO,
                cid_bytes,
                [1, 2],
            ).build_transaction({
                "from":  acct.address,
                "nonce": w3.eth.get_transaction_count(acct.address),
                "gas":   200_000,
            })
            signed = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status == 1:
                print(f"         ✅ Redeemed  tx={tx_hash.hex()[:16]}…")
                _mark_redeemed(conn, pos["id"])
                redeemed += 1
            else:
                print(f"         ❌ TX reverted  tx={tx_hash.hex()[:16]}…")

        except Exception as exc:
            print(f"         ❌ Redemption failed: {exc}")

    if live:
        print(f"\nRedeemed {redeemed}/{len(wins)} position(s).")
    else:
        print(f"\n[DRY-RUN] Would attempt to redeem {len(wins)} position(s). Run with --live to execute.")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Redeem won precip positions via CTF")
    parser.add_argument("--live", action="store_true", help="Execute on-chain (default: dry-run)")
    args = parser.parse_args()
    main(live=args.live)
