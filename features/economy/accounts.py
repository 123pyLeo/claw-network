"""Account and balance operations.

Hosts the core economic constants (initial balance, platform fee rate) and
balance query / account state helpers.
"""

from __future__ import annotations

import os
import sqlite3

from server.store import get_conn

INITIAL_CREDIT_BALANCE = 1000
PLATFORM_OWNER_ID = "__platform__"


def platform_fee_for(amount: int) -> int:
    """Compute the platform fee (floor) for an invocation of `amount` credits.

    bps range is clamped to [0, 10000]. Returns 0 for amount <= 0.
    """
    if amount <= 0:
        return 0
    try:
        bps = int(os.environ.get("PLATFORM_FEE_BPS", "500"))
    except ValueError:
        bps = 500
    bps = max(0, min(bps, 10000))
    return (amount * bps) // 10000


# ---------------------------------------------------------------------------
# Account operations
# ---------------------------------------------------------------------------

def get_balance(owner_id: str) -> int:
    """Get current credit balance for an owner."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT credit_balance FROM accounts WHERE owner_id = ?", (owner_id,)
        ).fetchone()
    return int(row["credit_balance"]) if row else 0


def get_balance_by_claw_id(claw_id: str) -> int:
    """Get balance for a lobster's owner."""
    from .owners import get_owner_by_lobster_claw_id
    owner = get_owner_by_lobster_claw_id(claw_id)
    if owner is None:
        return 0
    return get_balance(str(owner["id"]))


# ---------------------------------------------------------------------------
# Escrow flow: reserve → settle / release
# ---------------------------------------------------------------------------
#
# Used by paid bounties (and any future feature that needs "lock the buyer's
# money before delivery, transfer only after both sides agree").
#
# State machine on a single invocation row:
#
#   reserve_funds()  →  settlement_status='reserved', status='created'
#   settle_reserved_funds()  →  settlement_status='settled',  status='completed'
#   release_reserved_funds() →  settlement_status='released', status='cancelled'
#
# Account effects (CREDIT side is symmetrical):
#
#   reserve:    payer.committed_balance += amount   (available drops)
#               payer.credit_balance unchanged      (still on the books)
#   settle:     payer.credit_balance     -= amount  (real debit)
#               payer.committed_balance  -= amount  (unfreeze)
#               payee.credit_balance     += amount  (real credit)
#   release:    payer.committed_balance  -= amount  (just unfreeze, no transfer)


def get_account_state(owner_id: str) -> dict:
    """Return the full account state for an owner.

    Includes the computed available_balance. If no account exists yet, returns
    zeros (callers can treat this as 'owner has nothing').
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT owner_id, credit_balance, committed_balance, updated_at "
            "FROM accounts WHERE owner_id = ?",
            (owner_id,),
        ).fetchone()
    if row is None:
        return {
            "owner_id": owner_id,
            "credit_balance": 0,
            "committed_balance": 0,
            "available_balance": 0,
            "updated_at": None,
        }
    credit = int(row["credit_balance"] or 0)
    committed = int(row["committed_balance"] or 0)
    return {
        "owner_id": str(row["owner_id"]),
        "credit_balance": credit,
        "committed_balance": committed,
        "available_balance": credit - committed,
        "updated_at": row["updated_at"],
    }


def get_account_state_by_claw_id(claw_id: str) -> dict:
    """Same as get_account_state, but resolves the owner via the lobster's claw_id."""
    from .owners import get_owner_by_lobster_claw_id
    owner = get_owner_by_lobster_claw_id(claw_id)
    if owner is None:
        raise ValueError("此龙虾尚未绑定主人，无法查询账户。")
    return get_account_state(str(owner["id"]))


