"""Owner, nickname, join-request, and pairing-code operations."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from server.store import get_conn, get_lobster_by_claw_id, new_uuid, utc_now

from .accounts import INITIAL_CREDIT_BALANCE


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


