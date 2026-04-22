"""Invocations (instant + escrow) and reputation stats.

Stats live here because update_stats_on_settle/release are called inline
from the settle/release paths.
"""

from __future__ import annotations

import sqlite3
import time

from server.store import get_conn, get_lobster_by_claw_id, new_uuid, utc_now

from .accounts import PLATFORM_OWNER_ID, platform_fee_for


# ---------------------------------------------------------------------------
# Invocation: the atomic billing operation
# ---------------------------------------------------------------------------

class InsufficientBalanceError(ValueError):
    """Raised when an account doesn't have enough credit for an invocation."""
    pass


def create_invocation(
    caller_owner_id: str,
    callee_owner_id: str,
    source_type: str,
    source_id: str,
    amount: int,
) -> dict:
    """Create an invocation: atomically debit caller, credit callee, log it.

    All three operations happen in a single transaction. If the caller has
    insufficient balance, the entire operation fails with InsufficientBalanceError.
    """
    if amount < 0:
        raise ValueError("调用金额不能为负。")
    if caller_owner_id == callee_owner_id:
        raise ValueError("调用方和被调用方不能相同。")

    invocation_id = new_uuid()
    now = utc_now()
    fee = platform_fee_for(amount)
    payee_net = amount - fee

    with get_conn() as conn:
        try:
            # Step 1: atomic debit (fails if balance < amount)
            cursor = conn.execute(
                """
                UPDATE accounts
                SET credit_balance = credit_balance - ?,
                    updated_at = ?
                WHERE owner_id = ? AND credit_balance >= ?
                """,
                (amount, now, caller_owner_id, amount),
            )
            if cursor.rowcount == 0:
                # Either account doesn't exist or balance is insufficient
                check = conn.execute(
                    "SELECT credit_balance FROM accounts WHERE owner_id = ?",
                    (caller_owner_id,),
                ).fetchone()
                if check is None:
                    raise InsufficientBalanceError("调用方账户不存在。")
                raise InsufficientBalanceError(
                    f"积分余额不足。当前余额 {check['credit_balance']}，需要 {amount}。"
                )

            # Step 2: credit callee with net-of-fee (auto-create if missing)
            credited = conn.execute(
                """
                UPDATE accounts
                SET credit_balance = credit_balance + ?,
                    updated_at = ?
                WHERE owner_id = ?
                """,
                (payee_net, now, callee_owner_id),
            )
            if credited.rowcount == 0:
                conn.execute(
                    "INSERT INTO accounts (owner_id, credit_balance, updated_at) "
                    "VALUES (?, ?, ?)",
                    (callee_owner_id, payee_net, now),
                )

            # Step 2b: credit platform fee bucket
            if fee > 0:
                conn.execute(
                    "UPDATE accounts SET credit_balance = credit_balance + ?, "
                    "updated_at = ? WHERE owner_id = ?",
                    (fee, now, PLATFORM_OWNER_ID),
                )

            # Step 3: log invocation
            conn.execute(
                """
                INSERT INTO invocations (
                    id, caller_owner_id, callee_owner_id, source_type, source_id,
                    amount, platform_fee, payee_net,
                    status, created_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
                """,
                (
                    invocation_id, caller_owner_id, callee_owner_id,
                    source_type, source_id, amount, fee, payee_net, now, now,
                ),
            )

            row = conn.execute(
                "SELECT * FROM invocations WHERE id = ?", (invocation_id,)
            ).fetchone()
            instant_dict = dict(row)
            update_stats_on_settle(instant_dict)
            return instant_dict
        except sqlite3.Error as exc:
            raise ValueError(f"调用失败：{exc}") from exc


def list_invocations_for_owner(owner_id: str, limit: int = 50) -> list[dict]:
    """List invocations where this owner is either caller or callee."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM invocations
            WHERE caller_owner_id = ? OR callee_owner_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (owner_id, owner_id, max(1, min(limit, 200))),
        ).fetchall()
    return [dict(row) for row in rows]


class EscrowError(ValueError):
    """Base for all escrow state-machine errors."""


def reserve_funds(
    payer_owner_id: str,
    callee_owner_id: str,
    source_type: str,
    source_id: str,
    amount: int,
    *,
    competition_total_bids: int | None = None,
    competition_selected_rank: int | None = None,
    competition_context: str | None = None,
) -> dict:
    """Lock `amount` of payer's available balance for a future settlement.

    Atomically:
      1. Verifies payer.available >= amount via a conditional UPDATE.
      2. Increments payer.committed_balance by amount.
      3. Inserts an invocation row with settlement_status='reserved'.

    Raises InsufficientBalanceError if the payer doesn't have enough available
    funds. The callee account is auto-created (zero balance) if missing — we
    only need it to exist by the time we settle.
    """
    if amount <= 0:
        raise ValueError("Reserve amount must be positive.")
    if payer_owner_id == callee_owner_id:
        raise ValueError("Payer and payee cannot be the same owner.")

    invocation_id = new_uuid()
    now = utc_now()

    with get_conn() as conn:
        # Atomic reserve: only succeed if (credit_balance - committed_balance) >= amount.
        cursor = conn.execute(
            """
            UPDATE accounts
            SET committed_balance = committed_balance + ?,
                updated_at = ?
            WHERE owner_id = ?
              AND (credit_balance - committed_balance) >= ?
            """,
            (amount, now, payer_owner_id, amount),
        )
        if cursor.rowcount == 0:
            check = conn.execute(
                "SELECT credit_balance, committed_balance FROM accounts WHERE owner_id = ?",
                (payer_owner_id,),
            ).fetchone()
            if check is None:
                raise InsufficientBalanceError("付款方账户不存在。")
            available = int(check["credit_balance"] or 0) - int(check["committed_balance"] or 0)
            raise InsufficientBalanceError(
                f"积分余额不足。当前可用 {available}，需要 {amount}。"
            )

        # Make sure the payee has an account row to credit later. Zero-balance
        # is fine; settle will increment it.
        existing_payee = conn.execute(
            "SELECT 1 FROM accounts WHERE owner_id = ?", (callee_owner_id,)
        ).fetchone()
        if existing_payee is None:
            conn.execute(
                "INSERT INTO accounts (owner_id, credit_balance, committed_balance, updated_at) "
                "VALUES (?, 0, 0, ?)",
                (callee_owner_id, now),
            )

        conn.execute(
            """
            INSERT INTO invocations (
                id, caller_owner_id, callee_owner_id,
                source_type, source_id, amount,
                status, settlement_status,
                created_at, completed_at, settled_at, released_at,
                competition_total_bids, competition_selected_rank, competition_context
            )
            VALUES (?, ?, ?, ?, ?, ?, 'created', 'reserved', ?, NULL, NULL, NULL, ?, ?, ?)
            """,
            (
                invocation_id, payer_owner_id, callee_owner_id,
                source_type, source_id, amount, now,
                competition_total_bids, competition_selected_rank, competition_context,
            ),
        )

        row = conn.execute(
            "SELECT * FROM invocations WHERE id = ?", (invocation_id,)
        ).fetchone()
    return dict(row)


def settle_reserved_funds(invocation_id: str) -> dict:
    """Move funds from payer to payee for a previously reserved invocation.

    Idempotent on settled state: re-calling on a settled invocation is a no-op
    that returns the current row (helps if the settlement endpoint is retried).
    """
    now = utc_now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM invocations WHERE id = ?", (invocation_id,)
        ).fetchone()
        if row is None:
            raise EscrowError("Invocation not found.")
        current_settlement = str(row["settlement_status"] or "")
        if current_settlement == "settled":
            return dict(row)
        if current_settlement != "reserved":
            raise EscrowError(
                f"Cannot settle an invocation in state '{current_settlement}'."
            )

        amount = int(row["amount"] or 0)
        payer_id = str(row["caller_owner_id"])
        payee_id = str(row["callee_owner_id"])
        fee = platform_fee_for(amount)
        payee_net = amount - fee

        # Real debit: drop both total and committed by the amount. The
        # available balance is unchanged (it was already low because of the
        # earlier reserve), which is exactly what we want.
        debited = conn.execute(
            """
            UPDATE accounts
            SET credit_balance = credit_balance - ?,
                committed_balance = committed_balance - ?,
                updated_at = ?
            WHERE owner_id = ?
              AND credit_balance >= ?
              AND committed_balance >= ?
            """,
            (amount, amount, now, payer_id, amount, amount),
        )
        if debited.rowcount == 0:
            raise EscrowError("结算失败：付款方账户状态异常。")

        # Real credit: payee receives net-of-fee.
        credited = conn.execute(
            """
            UPDATE accounts
            SET credit_balance = credit_balance + ?,
                updated_at = ?
            WHERE owner_id = ?
            """,
            (payee_net, now, payee_id),
        )
        if credited.rowcount == 0:
            # Defensive: reserve_funds should have created the payee row.
            conn.execute(
                "INSERT INTO accounts (owner_id, credit_balance, committed_balance, updated_at) "
                "VALUES (?, ?, 0, ?)",
                (payee_id, payee_net, now),
            )

        # Credit platform fee bucket.
        if fee > 0:
            conn.execute(
                "UPDATE accounts SET credit_balance = credit_balance + ?, "
                "updated_at = ? WHERE owner_id = ?",
                (fee, now, PLATFORM_OWNER_ID),
            )

        conn.execute(
            """
            UPDATE invocations
            SET status = 'completed',
                settlement_status = 'settled',
                platform_fee = ?,
                payee_net = ?,
                completed_at = ?,
                settled_at = ?
            WHERE id = ?
            """,
            (fee, payee_net, now, now, invocation_id),
        )

        result = conn.execute(
            "SELECT * FROM invocations WHERE id = ?", (invocation_id,)
        ).fetchone()
    settled_dict = dict(result)
    update_stats_on_settle(settled_dict)
    return settled_dict


def release_reserved_funds(invocation_id: str) -> dict:
    """Unfreeze a reserved invocation, returning funds to the payer's available balance.

    Idempotent on released state. Refuses to release a settled invocation.
    """
    now = utc_now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM invocations WHERE id = ?", (invocation_id,)
        ).fetchone()
        if row is None:
            raise EscrowError("Invocation not found.")
        current_settlement = str(row["settlement_status"] or "")
        if current_settlement == "released":
            return dict(row)
        if current_settlement == "settled":
            raise EscrowError("Cannot release an already-settled invocation.")
        if current_settlement != "reserved":
            raise EscrowError(
                f"Cannot release an invocation in state '{current_settlement}'."
            )

        amount = int(row["amount"] or 0)
        payer_id = str(row["caller_owner_id"])

        unfrozen = conn.execute(
            """
            UPDATE accounts
            SET committed_balance = committed_balance - ?,
                updated_at = ?
            WHERE owner_id = ?
              AND committed_balance >= ?
            """,
            (amount, now, payer_id, amount),
        )
        if unfrozen.rowcount == 0:
            raise EscrowError("释放失败：付款方账户状态异常。")

        conn.execute(
            """
            UPDATE invocations
            SET status = 'cancelled',
                settlement_status = 'released',
                released_at = ?
            WHERE id = ?
            """,
            (now, invocation_id),
        )

        result = conn.execute(
            "SELECT * FROM invocations WHERE id = ?", (invocation_id,)
        ).fetchone()
    released_dict = dict(result)
    update_stats_on_release(released_dict)
    return released_dict


def record_seek_invocation(
    caller_owner_id: str,
    callee_owner_id: str,
    source_id: str,
) -> dict | None:
    """Record a seek event: one agent actively found and messaged another.

    source_type='seek', amount=0, status='completed', settlement_status='instant'.
    This captures the demand-side "who gets found" signal without any payment.
    Updates agent_stats + pair_stats via the settle hook.

    Returns the invocation dict, or None if it fails silently.
    """
    if not caller_owner_id or not callee_owner_id:
        return None
    if caller_owner_id == callee_owner_id:
        return None

    invocation_id = new_uuid()
    now = utc_now()

    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO invocations (
                    id, caller_owner_id, callee_owner_id,
                    source_type, source_id, amount,
                    status, settlement_status,
                    created_at, completed_at, settled_at, released_at,
                    competition_total_bids, competition_selected_rank, competition_context
                )
                VALUES (?, ?, ?, 'seek', ?, 0, 'completed', 'instant', ?, ?, NULL, NULL, NULL, NULL, 'direct_seek')
                """,
                (
                    invocation_id, caller_owner_id, callee_owner_id,
                    source_id, now, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM invocations WHERE id = ?", (invocation_id,)
            ).fetchone()
        result = dict(row)
        update_stats_on_settle(result)
        return result
    except Exception:
        return None


def get_invocation(invocation_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM invocations WHERE id = ?", (invocation_id,)
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Reputation stats: agent_stats + pair_stats
# ---------------------------------------------------------------------------
#
# Design principle (from product discussion):
#   "信任是关系,不是属性。平台提供原材料,不做裁判。"
#
# agent_stats = network-level summary (what the crowd thinks of this agent)
# pair_stats  = relationship-level summary (what THIS caller thinks of THAT callee)
#
# No scores. Only objective counts: completed, released, total, earned.
# Verdict/rating dimensions come later when we add post-transaction reviews.

def ensure_stats_tables() -> None:
    """Idempotent — safe to call from ensure_economy_tables()."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_stats (
                lobster_id TEXT PRIMARY KEY,
                total_invocations INTEGER NOT NULL DEFAULT 0,
                total_completed INTEGER NOT NULL DEFAULT 0,
                total_released INTEGER NOT NULL DEFAULT 0,
                active_callers INTEGER NOT NULL DEFAULT 0,
                completion_rate REAL NOT NULL DEFAULT 0,
                total_earned INTEGER NOT NULL DEFAULT 0,
                last_completed_at TEXT,
                last_computed TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pair_stats (
                caller_lobster_id TEXT NOT NULL,
                callee_lobster_id TEXT NOT NULL,
                total_invocations INTEGER NOT NULL DEFAULT 0,
                total_completed INTEGER NOT NULL DEFAULT 0,
                total_released INTEGER NOT NULL DEFAULT 0,
                total_spent INTEGER NOT NULL DEFAULT 0,
                last_invocation_at TEXT,
                last_computed TEXT NOT NULL,
                PRIMARY KEY (caller_lobster_id, callee_lobster_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pair_stats_callee "
            "ON pair_stats(callee_lobster_id)"
        )
        # Rating distribution (JSON string): {"1":0,"2":0,"3":1,"4":3,"5":8}
        stats_cols = {r["name"] for r in conn.execute("PRAGMA table_info(agent_stats)").fetchall()}
        if "rating_distribution" not in stats_cols:
            conn.execute("ALTER TABLE agent_stats ADD COLUMN rating_distribution TEXT DEFAULT '{}'")
        if "rating_count" not in stats_cols:
            conn.execute("ALTER TABLE agent_stats ADD COLUMN rating_count INTEGER DEFAULT 0")
    # Backfill from existing invocations
    _backfill_stats()


def _backfill_stats() -> None:
    """Recompute all stats from the invocations table. Idempotent."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                i.id,
                i.caller_owner_id,
                i.callee_owner_id,
                i.amount,
                i.settlement_status,
                i.created_at,
                i.settled_at,
                caller_l.id AS caller_lobster_id,
                callee_l.id AS callee_lobster_id
            FROM invocations i
            LEFT JOIN lobsters caller_l ON caller_l.owner_id = i.caller_owner_id
            LEFT JOIN lobsters callee_l ON callee_l.owner_id = i.callee_owner_id
            WHERE i.settlement_status IN ('settled', 'released', 'instant')
        """).fetchall()

    if not rows:
        return

    now = utc_now()

    # Aggregate by callee (agent_stats)
    agent_agg: dict[str, dict] = {}
    pair_agg: dict[tuple[str, str], dict] = {}
    callee_callers: dict[str, set] = {}

    for row in rows:
        callee_id = row["callee_lobster_id"]
        caller_id = row["caller_lobster_id"]
        if not callee_id:
            continue
        settled = row["settlement_status"] in ("settled", "instant")
        released = row["settlement_status"] == "released"
        amount = int(row["amount"] or 0)

        # agent_stats aggregation
        if callee_id not in agent_agg:
            agent_agg[callee_id] = {
                "total": 0, "completed": 0, "released": 0,
                "earned": 0, "last_completed": None,
            }
        a = agent_agg[callee_id]
        a["total"] += 1
        if settled:
            a["completed"] += 1
            a["earned"] += amount
            ts = row["settled_at"] or row["created_at"]
            if not a["last_completed"] or (ts and ts > a["last_completed"]):
                a["last_completed"] = ts
        if released:
            a["released"] += 1

        if callee_id not in callee_callers:
            callee_callers[callee_id] = set()
        if caller_id:
            callee_callers[callee_id].add(caller_id)

        # pair_stats aggregation
        if caller_id:
            key = (caller_id, callee_id)
            if key not in pair_agg:
                pair_agg[key] = {
                    "total": 0, "completed": 0, "released": 0,
                    "spent": 0, "last_at": None,
                }
            p = pair_agg[key]
            p["total"] += 1
            if settled:
                p["completed"] += 1
                p["spent"] += amount
            if released:
                p["released"] += 1
            ts = row["settled_at"] or row["created_at"]
            if not p["last_at"] or (ts and ts > p["last_at"]):
                p["last_at"] = ts

    # Write
    with get_conn() as conn:
        for lid, a in agent_agg.items():
            total = a["total"]
            rate = a["completed"] / total if total > 0 else 0
            conn.execute("""
                INSERT INTO agent_stats
                    (lobster_id, total_invocations, total_completed, total_released,
                     active_callers, completion_rate, total_earned,
                     last_completed_at, last_computed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lobster_id) DO UPDATE SET
                    total_invocations = excluded.total_invocations,
                    total_completed = excluded.total_completed,
                    total_released = excluded.total_released,
                    active_callers = excluded.active_callers,
                    completion_rate = excluded.completion_rate,
                    total_earned = excluded.total_earned,
                    last_completed_at = excluded.last_completed_at,
                    last_computed = excluded.last_computed
            """, (
                lid, total, a["completed"], a["released"],
                len(callee_callers.get(lid, set())),
                round(rate, 4), a["earned"],
                a["last_completed"], now,
            ))

        for (caller_id, callee_id), p in pair_agg.items():
            conn.execute("""
                INSERT INTO pair_stats
                    (caller_lobster_id, callee_lobster_id,
                     total_invocations, total_completed, total_released,
                     total_spent, last_invocation_at, last_computed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(caller_lobster_id, callee_lobster_id) DO UPDATE SET
                    total_invocations = excluded.total_invocations,
                    total_completed = excluded.total_completed,
                    total_released = excluded.total_released,
                    total_spent = excluded.total_spent,
                    last_invocation_at = excluded.last_invocation_at,
                    last_computed = excluded.last_computed
            """, (
                caller_id, callee_id,
                p["total"], p["completed"], p["released"],
                p["spent"], p["last_at"], now,
            ))


def update_stats_on_settle(invocation: dict) -> None:
    """Call after settle_reserved_funds / create_invocation to update stats."""
    _update_stats_for_invocation(invocation, settled=True)


def update_stats_on_release(invocation: dict) -> None:
    """Call after release_reserved_funds to update stats."""
    _update_stats_for_invocation(invocation, settled=False)


def _update_stats_for_invocation(invocation: dict, *, settled: bool) -> None:
    caller_owner = str(invocation.get("caller_owner_id") or "")
    callee_owner = str(invocation.get("callee_owner_id") or "")
    amount = int(invocation.get("amount") or 0)
    now = utc_now()

    # Resolve lobster IDs from owner IDs
    with get_conn() as conn:
        caller_row = conn.execute(
            "SELECT id FROM lobsters WHERE owner_id = ? LIMIT 1", (caller_owner,)
        ).fetchone() if caller_owner else None
        callee_row = conn.execute(
            "SELECT id FROM lobsters WHERE owner_id = ? LIMIT 1", (callee_owner,)
        ).fetchone() if callee_owner else None

    caller_lid = str(caller_row["id"]) if caller_row else None
    callee_lid = str(callee_row["id"]) if callee_row else None

    if not callee_lid:
        return

    with get_conn() as conn:
        # Upsert agent_stats for callee
        existing = conn.execute(
            "SELECT * FROM agent_stats WHERE lobster_id = ?", (callee_lid,)
        ).fetchone()
        if existing:
            new_total = int(existing["total_invocations"]) + 1
            new_completed = int(existing["total_completed"]) + (1 if settled else 0)
            new_released = int(existing["total_released"]) + (0 if settled else 1)
            new_earned = int(existing["total_earned"]) + (amount if settled else 0)
            new_rate = new_completed / new_total if new_total > 0 else 0
            # Count active callers
            active = conn.execute(
                "SELECT COUNT(DISTINCT caller_lobster_id) FROM pair_stats WHERE callee_lobster_id = ?",
                (callee_lid,)
            ).fetchone()[0]
            if caller_lid:
                active = max(active, 1)
            conn.execute("""
                UPDATE agent_stats SET
                    total_invocations = ?, total_completed = ?, total_released = ?,
                    active_callers = ?, completion_rate = ?, total_earned = ?,
                    last_completed_at = CASE WHEN ? THEN ? ELSE last_completed_at END,
                    last_computed = ?
                WHERE lobster_id = ?
            """, (
                new_total, new_completed, new_released,
                active, round(new_rate, 4), new_earned,
                settled, now,
                now, callee_lid,
            ))
        else:
            conn.execute("""
                INSERT INTO agent_stats
                    (lobster_id, total_invocations, total_completed, total_released,
                     active_callers, completion_rate, total_earned,
                     last_completed_at, last_computed)
                VALUES (?, 1, ?, ?, 1, ?, ?, ?, ?)
            """, (
                callee_lid,
                1 if settled else 0,
                0 if settled else 1,
                1.0 if settled else 0.0,
                amount if settled else 0,
                now if settled else None,
                now,
            ))

        # Upsert pair_stats
        if caller_lid:
            pair_existing = conn.execute(
                "SELECT * FROM pair_stats WHERE caller_lobster_id = ? AND callee_lobster_id = ?",
                (caller_lid, callee_lid),
            ).fetchone()
            if pair_existing:
                conn.execute("""
                    UPDATE pair_stats SET
                        total_invocations = total_invocations + 1,
                        total_completed = total_completed + ?,
                        total_released = total_released + ?,
                        total_spent = total_spent + ?,
                        last_invocation_at = ?,
                        last_computed = ?
                    WHERE caller_lobster_id = ? AND callee_lobster_id = ?
                """, (
                    1 if settled else 0,
                    0 if settled else 1,
                    amount if settled else 0,
                    now, now,
                    caller_lid, callee_lid,
                ))
            else:
                conn.execute("""
                    INSERT INTO pair_stats
                        (caller_lobster_id, callee_lobster_id,
                         total_invocations, total_completed, total_released,
                         total_spent, last_invocation_at, last_computed)
                    VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                """, (
                    caller_lid, callee_lid,
                    1 if settled else 0,
                    0 if settled else 1,
                    amount if settled else 0,
                    now, now,
                ))


def get_agent_stats(lobster_id: str) -> dict:
    """Get reputation stats for a lobster. Returns zeros if no history."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_stats WHERE lobster_id = ?", (lobster_id,)
        ).fetchone()
    if row is None:
        return {
            "lobster_id": lobster_id,
            "total_invocations": 0,
            "total_completed": 0,
            "total_released": 0,
            "active_callers": 0,
            "completion_rate": 0,
            "total_earned": 0,
            "last_completed_at": None,
        }
    return dict(row)


def get_agent_stats_by_claw_id(claw_id: str) -> dict:
    """Same as get_agent_stats but resolves by claw_id."""
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        return get_agent_stats("")
    return get_agent_stats(str(lobster["id"]))


