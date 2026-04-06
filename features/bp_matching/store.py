"""Database operations for BP matching."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from server.store import (
    get_conn,
    get_lobster_by_claw_id,
    new_uuid,
    utc_now,
    ensure_friendship,
)


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def ensure_bp_tables() -> None:
    """Create BP-related tables if they don't exist."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bp_listings (
                id              TEXT PRIMARY KEY,
                founder_lobster_id TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'active',
                access_policy   TEXT NOT NULL DEFAULT 'manual',
                project_name    TEXT NOT NULL,
                sector          TEXT NOT NULL DEFAULT '',
                stage           TEXT NOT NULL DEFAULT '',
                funding_ask     INTEGER,
                currency        TEXT NOT NULL DEFAULT 'CNY',
                one_liner       TEXT NOT NULL,
                team_size       INTEGER,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                expires_at      TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bp_intents (
                id              TEXT PRIMARY KEY,
                listing_id      TEXT NOT NULL,
                investor_lobster_id TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                personal_note   TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                reviewed_at     TEXT,
                UNIQUE(listing_id, investor_lobster_id)
            )
        """)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LISTING_SELECT = """
    SELECT
        bl.id, bl.status, bl.access_policy,
        bl.project_name, bl.sector, bl.stage,
        bl.funding_ask, bl.currency, bl.one_liner, bl.team_size,
        bl.created_at, bl.updated_at, bl.expires_at,
        l.claw_id AS founder_claw_id,
        l.name AS founder_name,
        l.org_name AS founder_org,
        (SELECT COUNT(*) FROM bp_intents bi WHERE bi.listing_id = bl.id) AS intent_count
    FROM bp_listings bl
    JOIN lobsters l ON l.id = bl.founder_lobster_id
"""

_INTENT_SELECT = """
    SELECT
        bi.id, bi.listing_id, bi.status, bi.personal_note,
        bi.created_at, bi.reviewed_at,
        bl.project_name,
        l.claw_id AS investor_claw_id,
        l.name AS investor_name,
        l.org_name AS investor_org
    FROM bp_intents bi
    JOIN lobsters l ON l.id = bi.investor_lobster_id
    JOIN bp_listings bl ON bl.id = bi.listing_id
"""


def _require_founder(lobster) -> None:
    """Check that lobster is a verified founder (or has phone verification)."""
    if not lobster["verified_phone"]:
        raise ValueError("请先完成手机验证。")
    role = str(lobster["role"] or "").strip()
    if role not in ("founder", "both"):
        raise ValueError("请先申请创业者角色认证。")


def _require_verified_investor(lobster) -> None:
    """Check that lobster is a verified investor."""
    if not lobster["verified_phone"]:
        raise ValueError("请先完成手机验证。")
    role = str(lobster["role"] or "").strip()
    if role not in ("investor", "both"):
        raise ValueError("请先申请投资人角色认证。")
    if not lobster["role_verified"]:
        raise ValueError("你的投资人身份尚未通过审核。")


# ---------------------------------------------------------------------------
# BP Listings
# ---------------------------------------------------------------------------

def create_listing(
    claw_id: str,
    project_name: str,
    one_liner: str,
    sector: str = "",
    stage: str = "",
    funding_ask: int | None = None,
    currency: str = "CNY",
    team_size: int | None = None,
    access_policy: str = "manual",
    expires_in_days: int = 90,
) -> dict:
    """Create a new BP listing."""
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    _require_founder(lobster)

    now = utc_now()
    expires_at = (datetime.fromisoformat(now) + timedelta(days=expires_in_days)).isoformat()
    listing_id = new_uuid()

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO bp_listings (
                id, founder_lobster_id, status, access_policy,
                project_name, sector, stage, funding_ask, currency, one_liner, team_size,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id, str(lobster["id"]), access_policy,
                project_name.strip(), sector.strip(), stage.strip(),
                funding_ask, currency, one_liner.strip(), team_size,
                now, now, expires_at,
            ),
        )
        row = conn.execute(f"{_LISTING_SELECT} WHERE bl.id = ?", (listing_id,)).fetchone()

    return dict(row)


def search_listings(
    sector: str | None = None,
    stage: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search active BP listings."""
    now = utc_now()
    sql = f"{_LISTING_SELECT} WHERE bl.status = 'active' AND (bl.expires_at IS NULL OR bl.expires_at > ?)"
    params: list = [now]

    if sector:
        sql += " AND bl.sector LIKE ?"
        params.append(f"%{sector.strip()}%")
    if stage:
        sql += " AND bl.stage = ?"
        params.append(stage.strip())

    sql += " ORDER BY bl.created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 100)))

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_listing(listing_id: str) -> dict:
    """Get a single BP listing."""
    with get_conn() as conn:
        row = conn.execute(f"{_LISTING_SELECT} WHERE bl.id = ?", (listing_id,)).fetchone()
    if row is None:
        raise ValueError("BP 不存在。")
    return dict(row)


def update_listing(listing_id: str, claw_id: str, access_policy: str | None = None, status: str | None = None) -> dict:
    """Update a BP listing (owner only)."""
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")

    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bp_listings WHERE id = ?", (listing_id,)).fetchone()
        if row is None:
            raise ValueError("BP 不存在。")
        if row["founder_lobster_id"] != str(lobster["id"]):
            raise ValueError("只有发布者可以修改。")

        updates = []
        params = []
        if access_policy is not None:
            updates.append("access_policy = ?")
            params.append(access_policy)
        if status is not None:
            updates.append("status = ?")
            params.append(status)

        if updates:
            updates.append("updated_at = ?")
            params.append(utc_now())
            params.append(listing_id)
            conn.execute(f"UPDATE bp_listings SET {', '.join(updates)} WHERE id = ?", params)

        result = conn.execute(f"{_LISTING_SELECT} WHERE bl.id = ?", (listing_id,)).fetchone()
    return dict(result)


def delete_listing(listing_id: str, claw_id: str) -> dict:
    """Close/delist a BP listing."""
    return update_listing(listing_id, claw_id, status="closed")


# ---------------------------------------------------------------------------
# BP Intents
# ---------------------------------------------------------------------------

def create_intent(listing_id: str, investor_claw_id: str, personal_note: str = "") -> dict:
    """Investor expresses interest in a BP listing."""
    investor = get_lobster_by_claw_id(investor_claw_id)
    if investor is None:
        raise ValueError("Lobster not found.")
    _require_verified_investor(investor)

    listing = get_listing(listing_id)
    if listing["status"] != "active":
        raise ValueError("该 BP 已关闭或过期。")
    if listing["founder_claw_id"] == investor_claw_id:
        raise ValueError("不能对自己的 BP 表达兴趣。")

    now = utc_now()
    intent_id = new_uuid()
    investor_id = str(investor["id"])

    # Determine initial status based on access policy
    initial_status = "auto_accepted" if listing["access_policy"] == "open" else "pending"

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM bp_intents WHERE listing_id = ? AND investor_lobster_id = ?",
            (listing_id, investor_id),
        ).fetchone()
        if existing is not None:
            raise ValueError("你已对此 BP 表达过兴趣。")

        conn.execute(
            """
            INSERT INTO bp_intents (id, listing_id, investor_lobster_id, status, personal_note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (intent_id, listing_id, investor_id, initial_status, personal_note.strip(), now),
        )

    # Auto-friend AFTER closing connection
    if initial_status == "auto_accepted":
        founder_lobster = get_lobster_by_claw_id(listing["founder_claw_id"])
        if founder_lobster:
            ensure_friendship(str(founder_lobster["id"]), investor_id)

    with get_conn() as conn:
        row = conn.execute(f"{_INTENT_SELECT} WHERE bi.id = ?", (intent_id,)).fetchone()

    return dict(row)


def list_intents(listing_id: str, claw_id: str) -> list[dict]:
    """List intents for a BP listing (founder only)."""
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")

    listing = get_listing(listing_id)
    if listing["founder_claw_id"] != claw_id:
        raise ValueError("只有发布者可以查看兴趣列表。")

    with get_conn() as conn:
        rows = conn.execute(
            f"{_INTENT_SELECT} WHERE bi.listing_id = ? ORDER BY bi.created_at ASC",
            (listing_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def review_intent(intent_id: str, claw_id: str, decision: str) -> dict:
    """Founder reviews an investor's interest. Returns updated intent."""
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")

    now = utc_now()
    investor_claw_id = None

    with get_conn() as conn:
        intent = conn.execute(f"{_INTENT_SELECT} WHERE bi.id = ?", (intent_id,)).fetchone()
        if intent is None:
            raise ValueError("兴趣记录不存在。")

        listing = conn.execute("SELECT * FROM bp_listings WHERE id = ?", (intent["listing_id"],)).fetchone()
        if listing is None or listing["founder_lobster_id"] != str(lobster["id"]):
            raise ValueError("只有 BP 发布者可以审核。")

        if intent["status"] not in ("pending",):
            raise ValueError("该请求已处理。")

        conn.execute(
            "UPDATE bp_intents SET status = ?, reviewed_at = ? WHERE id = ?",
            (decision, now, intent_id),
        )
        if decision == "accepted":
            investor_claw_id = intent["investor_claw_id"]

    # Auto-friend AFTER closing the connection to avoid nested locks
    if investor_claw_id:
        investor = get_lobster_by_claw_id(investor_claw_id)
        if investor:
            ensure_friendship(str(lobster["id"]), str(investor["id"]))

    with get_conn() as conn:
        row = conn.execute(f"{_INTENT_SELECT} WHERE bi.id = ?", (intent_id,)).fetchone()

    return dict(row)


def get_my_listings(claw_id: str) -> list[dict]:
    """Get all BP listings for a founder."""
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    with get_conn() as conn:
        rows = conn.execute(
            f"{_LISTING_SELECT} WHERE bl.founder_lobster_id = ? ORDER BY bl.created_at DESC",
            (str(lobster["id"]),),
        ).fetchall()
    return [dict(row) for row in rows]
