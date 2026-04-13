"""Economy layer: owners, accounts, invocations.

Design principles (from design discussion):
- owner is the economic entity (one phone = one owner, one owner can have many lobsters)
- accounts hang on owner_id, not on lobster
- atomic balance updates via SQL UPDATE with WHERE balance >= amount
- invocations are the unit of metered service calls
- no ledger, no state machine, no async settlement (deferred until real users)
"""

from __future__ import annotations

import sqlite3

from server.store import (
    get_conn,
    get_lobster_by_claw_id,
    new_uuid,
    utc_now,
)

# Initial credits granted to new owners (large enough for internal testing)
INITIAL_CREDIT_BALANCE = 1000


# ---------------------------------------------------------------------------
# Schema migration
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

        # owners.nickname: the canonical "what people call this person on
        # sandpile" — globally unique, distinct from real_name (which is the
        # legal name from role verification). Each lobster's owner_name field
        # is just a cached display value derived from this.
        owner_cols = {row["name"] for row in conn.execute("PRAGMA table_info(owners)").fetchall()}
        if "nickname" not in owner_cols:
            conn.execute("ALTER TABLE owners ADD COLUMN nickname TEXT")
        # NOTE: SQLite doesn't support functional unique indexes (lower(trim(...)))
        # cleanly with arbitrary expressions. We enforce uniqueness at the
        # application layer (set_owner_nickname) using normalize_owner_nickname.
        # The plain UNIQUE INDEX below catches exact-match duplicates as a
        # secondary safety net.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_owners_nickname "
            "ON owners(nickname) WHERE nickname IS NOT NULL"
        )

        # lobsters.owner_id (nullable: lobsters without phone have no owner yet)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(lobsters)").fetchall()}
        if "owner_id" not in cols:
            conn.execute("ALTER TABLE lobsters ADD COLUMN owner_id TEXT")

        # accounts: credit balance per owner.
        # credit_balance       = total funds the owner holds
        # committed_balance    = funds currently locked in escrow (reserved for
        #                        a pending invocation, not yet settled)
        # available_balance    = credit_balance - committed_balance (computed)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                owner_id TEXT PRIMARY KEY,
                credit_balance INTEGER NOT NULL DEFAULT 0,
                committed_balance INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES owners(id)
            )
        """)
        # Idempotent migration for legacy accounts that pre-date committed_balance.
        account_cols = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "committed_balance" not in account_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN committed_balance INTEGER NOT NULL DEFAULT 0")

        # invocations: metered service calls. Two flavors live in this table:
        #   1. Instant transfers (settlement_status='instant'): legacy direct
        #      debit/credit, used by bp_matching and the original bounty path.
        #   2. Escrow flow (settlement_status in 'reserved'/'settled'/'released'):
        #      poster's funds are frozen on reserve, then either released
        #      (cancel) or transferred to callee (confirm).
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
        # Idempotent migration for legacy invocations that pre-date escrow columns.
        inv_cols = {row["name"] for row in conn.execute("PRAGMA table_info(invocations)").fetchall()}
        if "settlement_status" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN settlement_status TEXT NOT NULL DEFAULT 'instant'")
        if "settled_at" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN settled_at TEXT")
        if "released_at" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN released_at TEXT")
        # Competitive context: how many competitors were there when this
        # invocation was created, and where did the selected agent rank.
        if "competition_total_bids" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN competition_total_bids INTEGER")
        if "competition_selected_rank" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN competition_selected_rank INTEGER")
        if "competition_context" not in inv_cols:
            conn.execute("ALTER TABLE invocations ADD COLUMN competition_context TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_invocations_caller "
            "ON invocations(caller_owner_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_invocations_callee "
            "ON invocations(callee_owner_id, created_at)"
        )

    # Migrate existing lobsters that have verified_phone but no owner_id
    _migrate_existing_lobsters()
    # Backfill owner.nickname from the earliest lobster.owner_name where missing
    backfill_owner_nicknames()
    # Ensure the pairing_codes table exists for the "claim my OpenClaw lobster" flow
    ensure_pairing_codes_table()
    # Reputation stats tables
    ensure_stats_tables()
    # Direct deal table
    ensure_deals_table()
    # Verdict + skill tables
    ensure_verdict_tables()
    ensure_skill_tables()


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
            # Check if owner already exists for this phone
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
                # Open account for the new owner
                conn.execute(
                    "INSERT INTO accounts (owner_id, credit_balance, updated_at) "
                    "VALUES (?, ?, ?)",
                    (owner_id, INITIAL_CREDIT_BALANCE, utc_now()),
                )

            conn.execute(
                "UPDATE lobsters SET owner_id = ? WHERE id = ?",
                (owner_id, row["id"]),
            )


# ---------------------------------------------------------------------------
# Owner management
# ---------------------------------------------------------------------------

def get_or_create_owner_by_phone(phone: str, real_name: str | None = None) -> dict:
    """Get an owner by phone, or create one if it doesn't exist.

    Also opens an account with the initial balance for newly created owners.
    Returns the owner row as a dict.
    """
    phone = phone.strip()
    now = utc_now()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM owners WHERE auth_phone = ?", (phone,)
        ).fetchone()
        if existing:
            return dict(existing)

        owner_id = new_uuid()
        conn.execute(
            "INSERT INTO owners (id, auth_phone, real_name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (owner_id, phone, real_name, now),
        )
        conn.execute(
            "INSERT INTO accounts (owner_id, credit_balance, updated_at) "
            "VALUES (?, ?, ?)",
            (owner_id, INITIAL_CREDIT_BALANCE, now),
        )
        row = conn.execute("SELECT * FROM owners WHERE id = ?", (owner_id,)).fetchone()
        return dict(row)


def list_lobsters_for_owner(owner_id: str) -> list[dict]:
    """List all lobsters belonging to an owner."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, claw_id, name, runtime_id, created_at FROM lobsters WHERE owner_id = ?",
            (owner_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Owner join requests (二次确认 for second lobster joining existing owner)
# ---------------------------------------------------------------------------

JOIN_REQUEST_EXPIRY_SECONDS = 24 * 3600  # 24 hours


def create_join_request(requesting_lobster_id: str, target_owner_id: str, phone: str) -> dict:
    """Create a pending join request. Returns the request as a dict."""
    from datetime import datetime, timedelta
    now = utc_now()
    expires_at = (datetime.fromisoformat(now) + timedelta(seconds=JOIN_REQUEST_EXPIRY_SECONDS)).isoformat()
    request_id = new_uuid()
    with get_conn() as conn:
        # Cancel any prior pending requests for the same lobster
        conn.execute(
            "UPDATE owner_join_requests SET status = 'cancelled' "
            "WHERE requesting_lobster_id = ? AND status = 'pending'",
            (requesting_lobster_id,),
        )
        conn.execute(
            """
            INSERT INTO owner_join_requests (
                id, requesting_lobster_id, target_owner_id, phone,
                status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (request_id, requesting_lobster_id, target_owner_id, phone, now, expires_at),
        )
        row = conn.execute(
            "SELECT * FROM owner_join_requests WHERE id = ?", (request_id,)
        ).fetchone()
    return dict(row)


def get_join_request(request_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM owner_join_requests WHERE id = ?", (request_id,)
        ).fetchone()
    return dict(row) if row else None


def review_join_request(request_id: str, reviewer_lobster_id: str, decision: str) -> dict:
    """Review a pending join request. decision: 'approved' or 'rejected'."""
    if decision not in ("approved", "rejected"):
        raise ValueError("Invalid decision")

    now = utc_now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM owner_join_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise ValueError("加入申请不存在。")
        if row["status"] != "pending":
            raise ValueError(f"申请已处理（{row['status']}）。")
        if row["expires_at"] <= now:
            raise ValueError("申请已过期。")

        # Verify reviewer is part of the target owner
        reviewer = conn.execute(
            "SELECT id, owner_id FROM lobsters WHERE id = ?", (reviewer_lobster_id,)
        ).fetchone()
        if reviewer is None:
            raise ValueError("审核者不存在。")
        if str(reviewer["owner_id"] or "") != str(row["target_owner_id"]):
            raise ValueError("只有目标账户的现有龙虾才能审核。")

        conn.execute(
            "UPDATE owner_join_requests SET status = ?, reviewed_at = ?, reviewed_by_lobster_id = ? WHERE id = ?",
            (decision, now, reviewer_lobster_id, request_id),
        )

        # If approved, link the requesting lobster to the target owner
        if decision == "approved":
            conn.execute(
                "UPDATE lobsters SET owner_id = ? WHERE id = ?",
                (row["target_owner_id"], row["requesting_lobster_id"]),
            )

        result = conn.execute(
            "SELECT * FROM owner_join_requests WHERE id = ?", (request_id,)
        ).fetchone()
    return dict(result)


def list_pending_join_requests_for_owner(owner_id: str) -> list[dict]:
    """List pending join requests targeting this owner."""
    now = utc_now()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT jr.*, l.claw_id AS requesting_claw_id, l.name AS requesting_name
            FROM owner_join_requests jr
            JOIN lobsters l ON l.id = jr.requesting_lobster_id
            WHERE jr.target_owner_id = ? AND jr.status = 'pending' AND jr.expires_at > ?
            ORDER BY jr.created_at ASC
            """,
            (owner_id, now),
        ).fetchall()
    return [dict(r) for r in rows]


def link_lobster_to_owner(lobster_id: str, owner_id: str) -> None:
    """Associate a lobster with an owner."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE lobsters SET owner_id = ? WHERE id = ?",
            (owner_id, lobster_id),
        )


def get_owner_by_lobster_claw_id(claw_id: str) -> dict | None:
    """Get the owner that owns a lobster, by claw_id."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT o.* FROM owners o
            JOIN lobsters l ON l.owner_id = o.id
            WHERE l.claw_id = ?
            """,
            (claw_id.strip().upper(),),
        ).fetchone()
    return dict(row) if row else None


def get_owner_by_id(owner_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM owners WHERE id = ?", (owner_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Owner nickname (canonical "what this person is called on sandpile")
# ---------------------------------------------------------------------------
#
# Design rule: owners.nickname is the source of truth for "what this person
# calls themselves on sandpile". Each lobster's owner_name field is just a
# cached display value — it must always equal the parent owner's nickname.
#
# Uniqueness: across all owners, nickname must be globally unique (after
# normalization). Two different humans cannot both call themselves "张三".
# Enforced at the application layer (set_owner_nickname); the SQLite
# UNIQUE INDEX in ensure_economy_tables() catches concurrent races.
#
# Note: this is distinct from owners.real_name, which is the LEGAL name
# captured during role verification (实名认证). nickname is for display +
# search; real_name is for compliance.

def normalize_owner_nickname(value: str) -> str:
    """Normalize a nickname for uniqueness comparison: collapse whitespace + lowercase."""
    if value is None:
        return ""
    return " ".join(str(value).strip().split()).lower()


class OwnerNicknameTakenError(ValueError):
    """Raised when a nickname is already used by a different owner."""

    def __init__(self, name: str):
        super().__init__(f"昵称「{name}」已经被其他用户使用，请换一个。")
        self.name = name


def find_owner_by_normalized_nickname(
    normalized_name: str, *, exclude_owner_id: str | None = None
) -> dict | None:
    """Look up an owner by normalized nickname. Returns None if no match."""
    if not normalized_name:
        return None
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM owners WHERE nickname IS NOT NULL"
        ).fetchall()
    for row in rows:
        if exclude_owner_id and str(row["id"]) == str(exclude_owner_id):
            continue
        if normalize_owner_nickname(str(row["nickname"])) == normalized_name:
            return dict(row)
    return None


def set_owner_nickname(owner_id: str, name: str) -> dict:
    """Set or change an owner's nickname.

    Validates global uniqueness. Raises OwnerNicknameTakenError on conflict.
    On success, propagates the canonical nickname to all lobsters under this
    owner (lobster.owner_name == owner.nickname everywhere).
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("昵称不能为空。")
    normalized = normalize_owner_nickname(cleaned)
    if not normalized:
        raise ValueError("昵称不能只包含空白字符。")

    conflicting = find_owner_by_normalized_nickname(
        normalized, exclude_owner_id=owner_id
    )
    if conflicting is not None:
        raise OwnerNicknameTakenError(cleaned)

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM owners WHERE id = ?", (owner_id,)
        ).fetchone()
        if existing is None:
            raise ValueError(f"Owner not found: {owner_id}")

        conn.execute(
            "UPDATE owners SET nickname = ? WHERE id = ?",
            (cleaned, owner_id),
        )
        # Sync the cached lobster.owner_name field on all lobsters under this owner
        conn.execute(
            "UPDATE lobsters SET owner_name = ? WHERE owner_id = ?",
            (cleaned, owner_id),
        )
        row = conn.execute(
            "SELECT * FROM owners WHERE id = ?", (owner_id,)
        ).fetchone()
    return dict(row)


def ensure_owner_nickname(owner_id: str, fallback_name: str) -> str:
    """Get the owner's nickname; set it from fallback_name if currently null.

    This is the integration point for register_lobster_for_owner(): when
    creating the user's first lobster, we use the typed owner_name to seed
    the nickname. Subsequent lobster creations return the existing nickname
    and IGNORE whatever owner_name was typed (canonical wins).

    Returns the canonical nickname.
    Raises OwnerNicknameTakenError if fallback_name conflicts.
    """
    owner = get_owner_by_id(owner_id)
    if owner is None:
        raise ValueError(f"Owner not found: {owner_id}")
    existing = (owner.get("nickname") or "").strip()
    if existing:
        return existing
    set_owner_nickname(owner_id, fallback_name)
    return (fallback_name or "").strip()


# ---------------------------------------------------------------------------
# Pairing codes — claim an existing OpenClaw lobster from the web console
# ---------------------------------------------------------------------------
#
# Flow:
#   1. User logs into sandpile.io console with phone (owner X)
#   2. User clicks "接入已有龙虾" → BFF calls /platform/owners/X/pairing-codes
#      → claw-network generates a 6-digit code, stores (code, X, expires_at)
#   3. Console displays the code, starts polling for status
#   4. User goes to OpenClaw chat, says "沙堆 接入控制台 123456"
#   5. The OpenClaw plugin detects the intent and calls
#      POST /lobsters/{claw_id}/claim-by-code {code: "123456"}
#      with the lobster's auth_token
#   6. Server validates: code exists, not expired, not used → look up owner X
#      → set lobster.owner_id = X → mark code as used
#   7. Console's polling sees status=claimed → refreshes agent list
#
# Codes are global (not per-owner), single-use, 10-min TTL.

PAIRING_CODE_TTL_SECONDS = 10 * 60  # 10 minutes


def ensure_pairing_codes_table() -> None:
    """Idempotent — safe to call from ensure_economy_tables()."""
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pairing_codes (
                code TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                claimed_lobster_id TEXT,
                FOREIGN KEY (owner_id) REFERENCES owners(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pairing_codes_owner "
            "ON pairing_codes(owner_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pairing_codes_expires "
            "ON pairing_codes(expires_at)"
        )


def _generate_pairing_code() -> str:
    """6-digit numeric code. 900k-space, collision very rare."""
    import secrets
    return f"{secrets.randbelow(900000) + 100000}"


def create_pairing_code(owner_id: str) -> dict:
    """Generate and persist a fresh pairing code for an owner.

    Returns {code, expires_at, expires_in_seconds}.
    Retries on the (extremely unlikely) collision with an active code.
    """
    from datetime import datetime, timedelta, timezone
    if not owner_id:
        raise ValueError("owner_id is required")
    now_dt = datetime.now(timezone.utc)
    expires_dt = now_dt + timedelta(seconds=PAIRING_CODE_TTL_SECONDS)
    now = now_dt.isoformat()
    expires_at = expires_dt.isoformat()

    # First, verify owner exists. Cheap.
    with get_conn() as conn:
        if conn.execute("SELECT id FROM owners WHERE id = ?", (owner_id,)).fetchone() is None:
            raise ValueError(f"Owner not found: {owner_id}")

    for _ in range(10):
        code = _generate_pairing_code()
        try:
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO pairing_codes (
                        code, owner_id, created_at, expires_at, used_at, claimed_lobster_id
                    ) VALUES (?, ?, ?, ?, NULL, NULL)
                    """,
                    (code, owner_id, now, expires_at),
                )
            return {
                "code": code,
                "expires_at": expires_at,
                "expires_in_seconds": PAIRING_CODE_TTL_SECONDS,
            }
        except sqlite3.IntegrityError:
            # Collision with another active code — retry with a fresh one.
            continue
    raise RuntimeError("Failed to generate a unique pairing code after 10 attempts.")


def get_pairing_code(code: str) -> dict | None:
    """Look up a pairing code. Returns the row dict or None if not found."""
    if not code:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pairing_codes WHERE code = ?", (code.strip(),)
        ).fetchone()
    return dict(row) if row else None


def get_pairing_code_status(code: str) -> dict | None:
    """Return status for a pairing code (used by console polling).

    Returns {status: 'pending'|'claimed'|'expired'|'not_found',
             claimed_lobster_id, claimed_at, expires_at}
    """
    from datetime import datetime, timezone
    row = get_pairing_code(code)
    if row is None:
        return {"status": "not_found"}
    if row.get("used_at"):
        return {
            "status": "claimed",
            "claimed_lobster_id": row.get("claimed_lobster_id"),
            "claimed_at": row.get("used_at"),
            "expires_at": row.get("expires_at"),
        }
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        return {"status": "expired", "expires_at": row.get("expires_at")}
    return {"status": "pending", "expires_at": row.get("expires_at")}


class PairingCodeError(ValueError):
    """Base for all pairing-code claim errors."""
    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.http_status = http_status


class PairingCodeNotFound(PairingCodeError):
    def __init__(self):
        super().__init__("配对码不存在或输入错误。", http_status=404)


class PairingCodeExpired(PairingCodeError):
    def __init__(self):
        super().__init__("配对码已过期，请回控制台重新生成。", http_status=410)


class PairingCodeAlreadyUsed(PairingCodeError):
    def __init__(self):
        super().__init__("配对码已被使用过了。", http_status=410)


class LobsterAlreadyBound(PairingCodeError):
    def __init__(self):
        super().__init__("这只龙虾已经绑定到其他账户了，不能再次接入。", http_status=409)


def claim_pairing_code(code: str, lobster_id: str) -> dict:
    """A lobster claims a pairing code, binding itself to the code's owner.

    Atomic: looks up code, validates state, binds lobster, marks code used.
    Returns: {owner_id, claimed_at, code}

    Raises:
      PairingCodeNotFound: code doesn't exist
      PairingCodeExpired:  code is past TTL
      PairingCodeAlreadyUsed: code was already claimed
      LobsterAlreadyBound: this lobster is already bound to a DIFFERENT owner
    """
    from datetime import datetime, timezone
    cleaned_code = (code or "").strip()
    if not cleaned_code:
        raise PairingCodeNotFound()
    if not lobster_id:
        raise ValueError("lobster_id is required")

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pairing_codes WHERE code = ?", (cleaned_code,)
        ).fetchone()
        if row is None:
            raise PairingCodeNotFound()
        if row["used_at"]:
            raise PairingCodeAlreadyUsed()
        if datetime.fromisoformat(row["expires_at"]) < now_dt:
            raise PairingCodeExpired()

        target_owner_id = str(row["owner_id"])

        # Check the lobster's current owner_id
        lob = conn.execute(
            "SELECT owner_id, owner_name FROM lobsters WHERE id = ?", (lobster_id,)
        ).fetchone()
        if lob is None:
            raise ValueError(f"Lobster not found: {lobster_id}")
        current_owner = str(lob["owner_id"] or "")
        if current_owner and current_owner != target_owner_id:
            raise LobsterAlreadyBound()
        previous_owner_name = (lob["owner_name"] or "").strip()

        # Look up the target owner's canonical nickname so we can sync the
        # lobster's cached owner_name field after binding. The design rule
        # (set in the owner-nickname migration earlier) is:
        # owner.nickname is the source of truth, lobster.owner_name is just
        # a display cache. After pairing, the cache must match the source.
        target_owner_row = conn.execute(
            "SELECT nickname FROM owners WHERE id = ?", (target_owner_id,)
        ).fetchone()
        target_nickname = ""
        if target_owner_row is not None:
            target_nickname = (target_owner_row["nickname"] or "").strip()

        # All good — bind and mark used in a single transaction
        if not current_owner:
            conn.execute(
                "UPDATE lobsters SET owner_id = ?, updated_at = ? WHERE id = ?",
                (target_owner_id, now, lobster_id),
            )

        # Sync the lobster's cached owner_name to the platform's canonical
        # nickname. We only overwrite when there's a real platform nickname
        # to set; if the platform owner has nickname IS NULL (rare —
        # auto-created owners without explicit nickname yet), we leave the
        # locally-typed name alone so the user still sees something.
        nickname_overwritten = False
        if target_nickname and target_nickname != previous_owner_name:
            conn.execute(
                "UPDATE lobsters SET owner_name = ?, updated_at = ? WHERE id = ?",
                (target_nickname, now, lobster_id),
            )
            nickname_overwritten = True

        conn.execute(
            "UPDATE pairing_codes SET used_at = ?, claimed_lobster_id = ? WHERE code = ?",
            (now, lobster_id, cleaned_code),
        )

    return {
        "owner_id": target_owner_id,
        "claimed_at": now,
        "code": cleaned_code,
        # Echo back the rename so the API can tell the user "we replaced your
        # local 'Leo' with the platform's '李大锤' — that's intentional".
        "previous_owner_name": previous_owner_name or None,
        "synced_owner_name": target_nickname or None,
        "owner_name_changed": nickname_overwritten,
    }


def backfill_owner_nicknames() -> int:
    """Migration: populate owner.nickname from the earliest lobster's owner_name
    for any owner that doesn't have a nickname yet.

    Handles uniqueness collisions by appending a numeric suffix (01, 02, ...).
    Returns the number of owners updated.
    """
    updated = 0
    with get_conn() as conn:
        owners_needing_backfill = conn.execute(
            "SELECT id FROM owners WHERE nickname IS NULL OR nickname = ''"
        ).fetchall()

    for owner_row in owners_needing_backfill:
        owner_id = str(owner_row["id"])
        with get_conn() as conn:
            earliest = conn.execute(
                "SELECT owner_name FROM lobsters "
                "WHERE owner_id = ? AND owner_name IS NOT NULL AND owner_name != '' "
                "ORDER BY created_at ASC LIMIT 1",
                (owner_id,),
            ).fetchone()
        if earliest is None:
            continue
        candidate = str(earliest["owner_name"]).strip()
        if not candidate:
            continue
        suffix = 0
        while True:
            attempt = candidate if suffix == 0 else f"{candidate}{suffix:02d}"
            try:
                set_owner_nickname(owner_id, attempt)
                updated += 1
                break
            except OwnerNicknameTakenError:
                suffix += 1
                if suffix > 99:
                    break
    return updated


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
    owner = get_owner_by_lobster_claw_id(claw_id)
    if owner is None:
        return 0
    return get_balance(str(owner["id"]))


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

            # Step 2: credit callee (auto-create account if missing)
            credited = conn.execute(
                """
                UPDATE accounts
                SET credit_balance = credit_balance + ?,
                    updated_at = ?
                WHERE owner_id = ?
                """,
                (amount, now, callee_owner_id),
            )
            if credited.rowcount == 0:
                conn.execute(
                    "INSERT INTO accounts (owner_id, credit_balance, updated_at) "
                    "VALUES (?, ?, ?)",
                    (callee_owner_id, amount, now),
                )

            # Step 3: log invocation
            conn.execute(
                """
                INSERT INTO invocations (
                    id, caller_owner_id, callee_owner_id, source_type, source_id,
                    amount, status, created_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?)
                """,
                (
                    invocation_id, caller_owner_id, callee_owner_id,
                    source_type, source_id, amount, now, now,
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
    owner = get_owner_by_lobster_claw_id(claw_id)
    if owner is None:
        raise ValueError("此龙虾尚未绑定主人，无法查询账户。")
    return get_account_state(str(owner["id"]))


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

        # Real credit: payee's total and available both increase.
        credited = conn.execute(
            """
            UPDATE accounts
            SET credit_balance = credit_balance + ?,
                updated_at = ?
            WHERE owner_id = ?
            """,
            (amount, now, payee_id),
        )
        if credited.rowcount == 0:
            # Defensive: reserve_funds should have created the payee row.
            conn.execute(
                "INSERT INTO accounts (owner_id, credit_balance, committed_balance, updated_at) "
                "VALUES (?, ?, 0, ?)",
                (payee_id, amount, now),
            )

        conn.execute(
            """
            UPDATE invocations
            SET status = 'completed',
                settlement_status = 'settled',
                completed_at = ?,
                settled_at = ?
            WHERE id = ?
            """,
            (now, now, invocation_id),
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


def search_lobsters_by_skill(skill_tag: str, limit: int = 20) -> list[dict]:
    """Find lobsters that have a specific skill tag. Earned first, then self_declared."""
    tag = skill_tag.strip().lower()
    if not tag:
        return []
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.lobster_id, s.skill_tag, s.source, l.claw_id, l.name, l.owner_name
            FROM agent_skills s
            JOIN lobsters l ON l.id = s.lobster_id
            WHERE s.skill_tag = ?
              AND (l.deleted_at IS NULL OR l.deleted_at = '')
            ORDER BY CASE s.source WHEN 'earned' THEN 0 ELSE 1 END, l.name
            LIMIT ?
        """, (tag, limit)).fetchall()
    return [dict(r) for r in rows]
