"""Economy layer — facade.

The actual implementation lives in per-domain submodules:
  - accounts.py    — balance, account state, platform fee constants
  - owners.py      — owners, nicknames, join requests, pairing codes
  - invocations.py — instant + escrow invocations and reputation stats
  - deals.py       — deals, verdicts, skills
  - extras.py      — redemption codes + delivery mechanism

This file:
1. Hosts the schema migration orchestrator (ensure_economy_tables)
2. Re-exports everything from the submodules so existing
   `from features.economy.store import X` imports keep working.
"""

from __future__ import annotations

import sqlite3

from server.store import get_conn, new_uuid, utc_now

# Re-exports (keep all historical import paths working)
from .accounts import *  # noqa: F401,F403
from .accounts import INITIAL_CREDIT_BALANCE, PLATFORM_OWNER_ID, platform_fee_for
from .owners import *  # noqa: F401,F403
from .owners import backfill_owner_nicknames, ensure_pairing_codes_table
from .invocations import *  # noqa: F401,F403
from .invocations import ensure_stats_tables
from .deals import *  # noqa: F401,F403
from .deals import ensure_deals_table, ensure_skill_tables, ensure_verdict_tables
from .extras import *  # noqa: F401,F403
from .extras import (
    ensure_confirmation_window_columns,
    ensure_delivery_tables,
    ensure_redemption_tables,
)


# ---------------------------------------------------------------------------
# Schema migration orchestrator
# ---------------------------------------------------------------------------

def ensure_economy_tables() -> None:
    """Create economy tables and migrate existing lobsters to have owners."""
    with get_conn() as conn:
        # owners: economic entity, one per real person (identified by phone)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS owners (
                id TEXT PRIMARY KEY,
                auth_phone TEXT,
                auth_email TEXT,
                real_name TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # owner_join_requests: pending requests to join an existing owner
        conn.execute("""
            CREATE TABLE IF NOT EXISTS owner_join_requests (
                id TEXT PRIMARY KEY,
                requesting_lobster_id TEXT NOT NULL,
                target_owner_id TEXT NOT NULL,
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by_lobster_id TEXT
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_owners_auth_phone "
            "ON owners(auth_phone) WHERE auth_phone IS NOT NULL"
        )

        owner_cols = {row["name"] for row in conn.execute("PRAGMA table_info(owners)").fetchall()}
        if "nickname" not in owner_cols:
            conn.execute("ALTER TABLE owners ADD COLUMN nickname TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_owners_nickname "
            "ON owners(nickname) WHERE nickname IS NOT NULL"
        )

        # lobsters.owner_id (nullable: lobsters without phone have no owner yet)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(lobsters)").fetchall()}
        if "owner_id" not in cols:
            conn.execute("ALTER TABLE lobsters ADD COLUMN owner_id TEXT")

        # accounts: credit balance per owner
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                owner_id TEXT PRIMARY KEY,
                credit_balance INTEGER NOT NULL DEFAULT 0,
                committed_balance INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES owners(id)
            )
        """)
        account_cols = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "committed_balance" not in account_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN committed_balance INTEGER NOT NULL DEFAULT 0")
        if "last_daily_bonus_at" not in account_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN last_daily_bonus_at TEXT")

        # invocations
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invocations (
                id TEXT PRIMARY KEY,
                caller_owner_id TEXT NOT NULL,
                callee_owner_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                settlement_status TEXT NOT NULL DEFAULT 'instant',
                created_at TEXT NOT NULL,
                completed_at TEXT,
                settled_at TEXT,
                released_at TEXT,
                FOREIGN KEY (caller_owner_id) REFERENCES owners(id),
                FOREIGN KEY (callee_owner_id) REFERENCES owners(id)
            )
        """)
        inv_cols = {row["name"] for row in conn.execute("PRAGMA table_info(invocations)").fetchall()}
        if "settlement_status" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN settlement_status TEXT NOT NULL DEFAULT 'instant'")
        if "settled_at" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN settled_at TEXT")
        if "released_at" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN released_at TEXT")
        if "competition_total_bids" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN competition_total_bids INTEGER")
        if "competition_selected_rank" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN competition_selected_rank INTEGER")
        if "competition_context" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN competition_context TEXT")
        if "platform_fee" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN platform_fee INTEGER NOT NULL DEFAULT 0")
        if "payee_net" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN payee_net INTEGER NOT NULL DEFAULT 0")
        if "trace_id" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN trace_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invocations_trace ON invocations(trace_id) WHERE trace_id IS NOT NULL")

        # Call traces: lightweight log of agent-to-agent routing steps
        conn.execute("""
            CREATE TABLE IF NOT EXISTS call_traces (
                id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                step_order INTEGER NOT NULL DEFAULT 0,
                from_claw_id TEXT NOT NULL,
                to_claw_id TEXT NOT NULL,
                from_name TEXT,
                to_name TEXT,
                action TEXT NOT NULL DEFAULT 'route',
                status TEXT NOT NULL DEFAULT 'pending',
                question TEXT,
                answer TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_call_traces_trace ON call_traces(trace_id)")

        # OAuth identities
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oauth_identities (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                provider_nickname TEXT,
                provider_avatar_url TEXT,
                provider_email TEXT,
                provider_phone TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES owners(id)
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_oauth_provider_user ON oauth_identities(provider, provider_user_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_invocations_caller "
            "ON invocations(caller_owner_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_invocations_callee "
            "ON invocations(callee_owner_id, created_at)"
        )

    # Ensure the platform fee bucket owner + account exist.
    _ensure_platform_owner()
    # Migrate existing lobsters that have verified_phone but no owner_id
    _migrate_existing_lobsters()
    # Backfill owner.nickname from the earliest lobster.owner_name where missing
    backfill_owner_nicknames()
    # Reputation / deal / verdict / skill / redemption / delivery tables
    ensure_pairing_codes_table()
    ensure_stats_tables()
    ensure_deals_table()
    ensure_verdict_tables()
    ensure_skill_tables()
    ensure_redemption_tables()
    ensure_delivery_tables()
    ensure_confirmation_window_columns()


def _ensure_platform_owner() -> None:
    """Insert the synthetic platform owner + account if they don't exist."""
    now = utc_now()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO owners (id, real_name, nickname, created_at) "
            "VALUES (?, ?, ?, ?)",
            (PLATFORM_OWNER_ID, "平台手续费账户", "平台手续费账户", now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO accounts (owner_id, credit_balance, committed_balance, updated_at) "
            "VALUES (?, 0, 0, ?)",
            (PLATFORM_OWNER_ID, now),
        )


def _migrate_existing_lobsters() -> None:
    """Backfill owners for existing lobsters with verified_phone."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, claw_id, verified_phone, real_name, created_at "
            "FROM lobsters "
            "WHERE verified_phone IS NOT NULL AND (owner_id IS NULL OR owner_id = '')"
        ).fetchall()

        for row in rows:
            phone = str(row["verified_phone"]).strip()
            if not phone:
                continue
            existing = conn.execute(
                "SELECT id FROM owners WHERE auth_phone = ?", (phone,)
            ).fetchone()
            if existing:
                owner_id = str(existing["id"])
            else:
                owner_id = new_uuid()
                conn.execute(
                    "INSERT INTO owners (id, auth_phone, real_name, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (owner_id, phone, row["real_name"], row["created_at"]),
                )
                conn.execute(
                    "INSERT INTO accounts (owner_id, credit_balance, updated_at) "
                    "VALUES (?, ?, ?)",
                    (owner_id, INITIAL_CREDIT_BALANCE, utc_now()),
                )

            conn.execute(
                "UPDATE lobsters SET owner_id = ? WHERE id = ?",
                (owner_id, row["id"]),
            )
