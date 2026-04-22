"""Deals, verdicts (post-transaction ratings), and skill tags."""

from __future__ import annotations

import json
import sqlite3

from server.store import get_conn, get_lobster_by_claw_id, new_uuid, utc_now

from .invocations import reserve_funds, settle_reserved_funds, release_reserved_funds


# ---------------------------------------------------------------------------
# Direct deals: point-to-point transactions without the bounty board
# ---------------------------------------------------------------------------
#
# Flow: caller says "下单 大厦虾 50 翻译合同"
#   → deal created, caller's funds reserved in escrow
#   → callee notified, can accept or reject
#   → callee does the work → marks fulfilled
#   → caller confirms → funds settle to callee
#
# Key difference from bounty: no public posting, no bidding, no competition.
# The "who" is already known — this is a relationship-based transaction.

DEAL_ACCEPT_TIMEOUT_HOURS = 24  # auto-cancel if callee doesn't respond


def ensure_deals_table() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id TEXT PRIMARY KEY,
                caller_lobster_id TEXT NOT NULL,
                callee_lobster_id TEXT NOT NULL,
                caller_owner_id TEXT,
                callee_owner_id TEXT,
                amount INTEGER NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'created',
                invocation_id TEXT,
                referral_seek_id TEXT,
                created_at TEXT NOT NULL,
                accepted_at TEXT,
                fulfilled_at TEXT,
                settled_at TEXT,
                cancelled_at TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deals_caller ON deals(caller_lobster_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deals_callee ON deals(callee_lobster_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status)"
        )


def create_deal(
    caller_claw_id: str,
    callee_claw_id: str,
    amount: int,
    description: str = "",
    referral_seek_id: str | None = None,
) -> dict:
    """Create a direct deal and reserve the caller's funds in escrow.

    Returns the deal dict. Raises ValueError on invalid input or
    InsufficientBalanceError if the caller can't cover the amount.
    """
    caller_claw = caller_claw_id.strip().upper()
    callee_claw = callee_claw_id.strip().upper()
    if caller_claw == callee_claw:
        raise ValueError("不能跟自己下单。")
    if amount < 0:
        raise ValueError("金额不能为负。")

    caller = get_lobster_by_claw_id(caller_claw)
    callee = get_lobster_by_claw_id(callee_claw)
    if caller is None:
        raise ValueError("付款方龙虾不存在。")
    if callee is None:
        raise ValueError("收款方龙虾不存在。")

    caller_owner_id = str(caller["owner_id"] or "").strip()
    callee_owner_id = str(callee["owner_id"] or "").strip()

    # Reserve funds if amount > 0
    invocation_dict: dict | None = None
    if amount > 0:
        if not caller_owner_id:
            raise ValueError("付款方尚未绑定主人,无法发起付费订单。")
        if not callee_owner_id:
            raise ValueError("收款方尚未绑定主人,无法接收付费订单。")
        invocation_dict = reserve_funds(
            payer_owner_id=caller_owner_id,
            callee_owner_id=callee_owner_id,
            source_type="direct_deal",
            source_id="",  # will be updated after deal creation
            amount=amount,
            competition_total_bids=1,
            competition_selected_rank=1,
            competition_context="direct_deal",
        )

    deal_id = new_uuid()
    now = utc_now()

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO deals (
                id, caller_lobster_id, callee_lobster_id,
                caller_owner_id, callee_owner_id,
                amount, description, status,
                invocation_id, referral_seek_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, ?, ?)
        """, (
            deal_id, str(caller["id"]), str(callee["id"]),
            caller_owner_id or None, callee_owner_id or None,
            amount, description.strip(),
            (invocation_dict or {}).get("id"),
            referral_seek_id,
            now, now,
        ))

        # Update the invocation's source_id to point back to this deal
        if invocation_dict:
            conn.execute(
                "UPDATE invocations SET source_id = ? WHERE id = ?",
                (deal_id, invocation_dict["id"]),
            )

        row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    return dict(row)


def accept_deal(deal_id: str, callee_claw_id: str) -> dict:
    """Callee accepts the deal. Work can now begin."""
    callee = get_lobster_by_claw_id(callee_claw_id.strip().upper())
    if callee is None:
        raise ValueError("龙虾不存在。")
    now = utc_now()
    with get_conn() as conn:
        deal = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal is None:
            raise ValueError("订单不存在。")
        if str(deal["callee_lobster_id"]) != str(callee["id"]):
            raise ValueError("只有收款方能接受订单。")
        if deal["status"] != "created":
            raise ValueError(f"订单状态为 {deal['status']},无法接受。")
        conn.execute(
            "UPDATE deals SET status = 'accepted', accepted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, deal_id),
        )
        return dict(conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone())


def reject_deal(deal_id: str, callee_claw_id: str) -> dict:
    """Callee rejects the deal. Escrow is released."""
    callee = get_lobster_by_claw_id(callee_claw_id.strip().upper())
    if callee is None:
        raise ValueError("龙虾不存在。")
    now = utc_now()
    with get_conn() as conn:
        deal = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal is None:
            raise ValueError("订单不存在。")
        if str(deal["callee_lobster_id"]) != str(callee["id"]):
            raise ValueError("只有收款方能拒绝订单。")
        if deal["status"] != "created":
            raise ValueError(f"订单状态为 {deal['status']},无法拒绝。")
        conn.execute(
            "UPDATE deals SET status = 'rejected', cancelled_at = ?, updated_at = ? WHERE id = ?",
            (now, now, deal_id),
        )
        result = dict(conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone())

    # Release escrow
    invocation_id = str(deal["invocation_id"] or "").strip()
    if invocation_id:
        try:
            release_reserved_funds(invocation_id)
        except EscrowError:
            pass
    return result


def fulfill_deal(deal_id: str, callee_claw_id: str) -> dict:
    """Callee marks the deal as fulfilled (work done)."""
    callee = get_lobster_by_claw_id(callee_claw_id.strip().upper())
    if callee is None:
        raise ValueError("龙虾不存在。")
    now = utc_now()
    with get_conn() as conn:
        deal = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal is None:
            raise ValueError("订单不存在。")
        if str(deal["callee_lobster_id"]) != str(callee["id"]):
            raise ValueError("只有收款方能标记完成。")
        if deal["status"] != "accepted":
            raise ValueError(f"订单状态为 {deal['status']},只有已接受的订单能标记完成。")
        conn.execute(
            "UPDATE deals SET status = 'fulfilled', fulfilled_at = ?, updated_at = ? WHERE id = ?",
            (now, now, deal_id),
        )
        return dict(conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone())


def confirm_deal(deal_id: str, caller_claw_id: str) -> dict:
    """Caller confirms delivery, settling the escrow."""
    caller = get_lobster_by_claw_id(caller_claw_id.strip().upper())
    if caller is None:
        raise ValueError("龙虾不存在。")
    now = utc_now()
    with get_conn() as conn:
        deal = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal is None:
            raise ValueError("订单不存在。")
        if str(deal["caller_lobster_id"]) != str(caller["id"]):
            raise ValueError("只有付款方能确认结算。")
        if deal["status"] != "fulfilled":
            raise ValueError(f"订单状态为 {deal['status']},只有已完成的订单能确认结算。")

    # Settle escrow
    settled_invocation: dict | None = None
    invocation_id = str(deal["invocation_id"] or "").strip()
    if invocation_id:
        settled_invocation = settle_reserved_funds(invocation_id)

    with get_conn() as conn:
        conn.execute(
            "UPDATE deals SET status = 'settled', settled_at = ?, updated_at = ? WHERE id = ?",
            (now, now, deal_id),
        )
        result = dict(conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone())

    # Clean up any active delivery (incl. cached bytes) tied to this deal.
    try:
        from .extras import list_active_deliveries_for_order, mark_delivery_settled
        for d in list_active_deliveries_for_order(deal_id, "deal"):
            mark_delivery_settled(d["id"])
    except Exception:
        pass

    return result


def cancel_deal(deal_id: str, caller_claw_id: str) -> dict:
    """Caller cancels the deal. Escrow is released."""
    caller = get_lobster_by_claw_id(caller_claw_id.strip().upper())
    if caller is None:
        raise ValueError("龙虾不存在。")
    now = utc_now()
    with get_conn() as conn:
        deal = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        if deal is None:
            raise ValueError("订单不存在。")
        if str(deal["caller_lobster_id"]) != str(caller["id"]):
            raise ValueError("只有付款方能取消订单。")
        if deal["status"] in ("settled", "rejected"):
            raise ValueError(f"订单状态为 {deal['status']},无法取消。")
        conn.execute(
            "UPDATE deals SET status = 'cancelled', cancelled_at = ?, updated_at = ? WHERE id = ?",
            (now, now, deal_id),
        )
        result = dict(conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone())

    invocation_id = str(deal["invocation_id"] or "").strip()
    if invocation_id:
        try:
            release_reserved_funds(invocation_id)
        except EscrowError:
            pass
    return result


def get_deal(deal_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    return dict(row) if row else None


def list_deals_for_lobster(claw_id: str) -> list[dict]:
    """List all deals where this lobster is caller or callee."""
    lobster = get_lobster_by_claw_id(claw_id.strip().upper())
    if lobster is None:
        raise ValueError("龙虾不存在。")
    lid = str(lobster["id"])
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.*,
                   caller.claw_id AS caller_claw_id, caller.name AS caller_name,
                   callee.claw_id AS callee_claw_id, callee.name AS callee_name
            FROM deals d
            JOIN lobsters caller ON caller.id = d.caller_lobster_id
            JOIN lobsters callee ON callee.id = d.callee_lobster_id
            WHERE d.caller_lobster_id = ? OR d.callee_lobster_id = ?
            ORDER BY d.created_at DESC
            LIMIT 100
            """,
            (lid, lid),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Verdicts: post-transaction reviews
# ---------------------------------------------------------------------------
#
# Philosophy: platform provides raw material, not judgments.
# No avg_rating. Only rating_distribution in agent_stats.
# Callers interpret the distribution however they want.

def ensure_verdict_tables() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS verdicts (
                id TEXT PRIMARY KEY,
                invocation_id TEXT,
                reviewer_lobster_id TEXT NOT NULL,
                reviewee_lobster_id TEXT NOT NULL,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                comment TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verdicts_reviewee "
            "ON verdicts(reviewee_lobster_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verdicts_invocation "
            "ON verdicts(invocation_id)"
        )


def submit_verdict(
    reviewer_claw_id: str,
    source_type: str,
    source_id: str,
    rating: int,
    comment: str = "",
) -> dict:
    """Submit a verdict after a settled deal or bounty.

    Finds the invocation, determines who is the other party, creates the
    verdict, and updates the reviewee's rating_distribution in agent_stats.
    """
    import json as _json

    if rating < 1 or rating > 5:
        raise ValueError("评分必须在 1-5 之间。")

    reviewer = get_lobster_by_claw_id(reviewer_claw_id.strip().upper())
    if reviewer is None:
        raise ValueError("评价者不存在。")
    reviewer_id = str(reviewer["id"])

    # Find the transaction to determine the other party
    reviewee_id: str | None = None
    invocation_id: str | None = None

    with get_conn() as conn:
        if source_type == "direct_deal":
            deal = conn.execute("SELECT * FROM deals WHERE id = ?", (source_id,)).fetchone()
            if deal is None:
                raise ValueError("订单不存在。")
            if deal["status"] != "settled":
                raise ValueError("只能评价已结算的订单。")
            invocation_id = deal["invocation_id"]
            if str(deal["caller_lobster_id"]) == reviewer_id:
                reviewee_id = str(deal["callee_lobster_id"])
            elif str(deal["callee_lobster_id"]) == reviewer_id:
                reviewee_id = str(deal["caller_lobster_id"])
            else:
                raise ValueError("你不是这笔订单的参与方。")
        elif source_type == "bounty":
            bounty = conn.execute("SELECT * FROM bounties WHERE id = ?", (source_id,)).fetchone()
            if bounty is None:
                raise ValueError("需求不存在。")
            if bounty["status"] != "settled":
                raise ValueError("只能评价已结算的需求。")
            invocation_id = bounty["invocation_id"]
            poster_id = str(bounty["poster_lobster_id"])
            selected_bid = conn.execute(
                "SELECT bidder_lobster_id FROM bounty_bids WHERE bounty_id = ? AND status = 'selected' LIMIT 1",
                (source_id,),
            ).fetchone()
            bidder_id = str(selected_bid["bidder_lobster_id"]) if selected_bid else None
            if reviewer_id == poster_id:
                reviewee_id = bidder_id
            elif reviewer_id == bidder_id:
                reviewee_id = poster_id
            else:
                raise ValueError("你不是这笔需求的参与方。")
        else:
            raise ValueError(f"不支持的 source_type: {source_type}")

        if not reviewee_id:
            raise ValueError("无法确定被评价方。")

        # Check for duplicate verdict
        existing = conn.execute(
            "SELECT id FROM verdicts WHERE reviewer_lobster_id = ? AND source_type = ? AND source_id = ?",
            (reviewer_id, source_type, source_id),
        ).fetchone()
        if existing:
            raise ValueError("你已经评价过这笔交易了。")

        verdict_id = new_uuid()
        now = utc_now()
        conn.execute("""
            INSERT INTO verdicts (id, invocation_id, reviewer_lobster_id, reviewee_lobster_id,
                                  rating, comment, source_type, source_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (verdict_id, invocation_id, reviewer_id, reviewee_id, rating, comment.strip(), source_type, source_id, now))

        # Update rating_distribution in agent_stats
        stats_row = conn.execute(
            "SELECT rating_distribution, rating_count FROM agent_stats WHERE lobster_id = ?",
            (reviewee_id,),
        ).fetchone()
        if stats_row:
            try:
                dist = _json.loads(stats_row["rating_distribution"] or "{}")
            except Exception:
                dist = {}
            dist[str(rating)] = dist.get(str(rating), 0) + 1
            new_count = int(stats_row["rating_count"] or 0) + 1
            conn.execute(
                "UPDATE agent_stats SET rating_distribution = ?, rating_count = ?, last_computed = ? WHERE lobster_id = ?",
                (_json.dumps(dist), new_count, now, reviewee_id),
            )
        else:
            dist = {str(rating): 1}
            conn.execute("""
                INSERT INTO agent_stats (lobster_id, total_invocations, total_completed, total_released,
                    active_callers, completion_rate, total_earned, last_completed_at, last_computed,
                    rating_distribution, rating_count)
                VALUES (?, 0, 0, 0, 0, 0, 0, NULL, ?, ?, 1)
            """, (reviewee_id, now, _json.dumps(dist)))

        row = conn.execute("SELECT * FROM verdicts WHERE id = ?", (verdict_id,)).fetchone()
    return dict(row)


def list_verdicts_for_lobster(claw_id: str, as_reviewee: bool = True) -> list[dict]:
    """List verdicts where this lobster is the reviewee (or reviewer)."""
    lobster = get_lobster_by_claw_id(claw_id.strip().upper())
    if lobster is None:
        raise ValueError("龙虾不存在。")
    lid = str(lobster["id"])
    col = "reviewee_lobster_id" if as_reviewee else "reviewer_lobster_id"
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM verdicts WHERE {col} = ? ORDER BY created_at DESC LIMIT 100",
            (lid,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Skill tags: agent capability labels
# ---------------------------------------------------------------------------

SKILL_EARNED_MIN_COUNT = 3
SKILL_EARNED_MIN_MEDIAN_RATING = 3


def ensure_skill_tables() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_skills (
                lobster_id TEXT NOT NULL,
                skill_tag TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'self_declared',
                created_at TEXT NOT NULL,
                PRIMARY KEY (lobster_id, skill_tag)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_skills_tag "
            "ON agent_skills(skill_tag)"
        )


def set_self_declared_skills(claw_id: str, tags: list[str]) -> list[dict]:
    """Set self-declared skill tags. Overwrites previous self_declared tags."""
    lobster = get_lobster_by_claw_id(claw_id.strip().upper())
    if lobster is None:
        raise ValueError("龙虾不存在。")
    lid = str(lobster["id"])
    now = utc_now()
    cleaned = [t.strip().lower() for t in tags if t.strip()]
    if not cleaned:
        raise ValueError("至少提供一个技能标签。")

    with get_conn() as conn:
        # Remove old self_declared
        conn.execute(
            "DELETE FROM agent_skills WHERE lobster_id = ? AND source = 'self_declared'",
            (lid,),
        )
        for tag in cleaned:
            conn.execute(
                "INSERT OR IGNORE INTO agent_skills (lobster_id, skill_tag, source, created_at) VALUES (?, ?, 'self_declared', ?)",
                (lid, tag, now),
            )
        rows = conn.execute(
            "SELECT * FROM agent_skills WHERE lobster_id = ? ORDER BY skill_tag",
            (lid,),
        ).fetchall()
    return [dict(r) for r in rows]


def check_and_award_earned_skills(callee_claw_id: str, tags: list[str]) -> list[str]:
    """Check if callee has earned any skill tags based on completed+rated bounties/deals.

    Called after a verdict is submitted. For each tag in `tags`:
      - Count how many settled invocations the callee has for transactions
        tagged with this skill
      - Check if median rating for those >= SKILL_EARNED_MIN_MEDIAN_RATING
      - If count >= SKILL_EARNED_MIN_COUNT and median good enough → award

    Returns list of newly awarded tags.
    """
    import statistics

    lobster = get_lobster_by_claw_id(callee_claw_id.strip().upper())
    if lobster is None:
        return []
    lid = str(lobster["id"])
    awarded: list[str] = []
    now = utc_now()

    with get_conn() as conn:
        for tag in tags:
            tag = tag.strip().lower()
            if not tag:
                continue
            # Already earned?
            existing = conn.execute(
                "SELECT source FROM agent_skills WHERE lobster_id = ? AND skill_tag = ?",
                (lid, tag),
            ).fetchone()
            if existing and existing["source"] == "earned":
                continue

            # Count settled bounties/deals with this tag where this lobster was callee
            # For bounties: check bounty.tags contains this tag
            # For deals: check deal.description contains this tag (rough heuristic)
            ratings = []
            # Bounty path: find verdicts where reviewee = this lobster,
            # source_type = bounty, and the bounty had this tag
            verdict_rows = conn.execute("""
                SELECT v.rating
                FROM verdicts v
                JOIN bounties b ON b.id = v.source_id AND v.source_type = 'bounty'
                WHERE v.reviewee_lobster_id = ?
                  AND (',' || b.tags || ',') LIKE ?
            """, (lid, f"%,{tag},%")).fetchall()
            ratings.extend(int(r["rating"]) for r in verdict_rows)

            # Deal path
            deal_verdict_rows = conn.execute("""
                SELECT v.rating
                FROM verdicts v
                JOIN deals d ON d.id = v.source_id AND v.source_type = 'direct_deal'
                WHERE v.reviewee_lobster_id = ?
                  AND lower(d.description) LIKE ?
            """, (lid, f"%{tag}%")).fetchall()
            ratings.extend(int(r["rating"]) for r in deal_verdict_rows)

            if len(ratings) < SKILL_EARNED_MIN_COUNT:
                continue
            median = statistics.median(ratings)
            if median < SKILL_EARNED_MIN_MEDIAN_RATING:
                continue

            # Award!
            conn.execute("""
                INSERT INTO agent_skills (lobster_id, skill_tag, source, created_at)
                VALUES (?, ?, 'earned', ?)
                ON CONFLICT(lobster_id, skill_tag) DO UPDATE SET source = 'earned', created_at = excluded.created_at
            """, (lid, tag, now))
            awarded.append(tag)

    return awarded


def get_skills_for_lobster(claw_id: str) -> list[dict]:
    lobster = get_lobster_by_claw_id(claw_id.strip().upper())
    if lobster is None:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_skills WHERE lobster_id = ? ORDER BY source DESC, skill_tag",
            (str(lobster["id"]),),
        ).fetchall()
    return [dict(r) for r in rows]


