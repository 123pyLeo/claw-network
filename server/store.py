from __future__ import annotations

import random
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
DB_PATH = ROOT_DIR / "openclaw_a2a.db"

OFFICIAL_RUNTIME_ID = "official-openclaw"
OFFICIAL_CLAW_ID = "CLAW-000001"
OFFICIAL_NAME = "零动涌现的龙虾"
OFFICIAL_OWNER = "OpenClaw Official"
CLAW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MESSAGE_STATUS_LABELS = {
    "queued": "排队中",
    "delivered": "已送达",
    "consumed": "已接收",
    "read": "已读",
    "failed": "失败",
    "pending": "待确认",
    "approved_once": "本次允许",
    "approved_persistent": "长期允许",
    "rejected": "已拒绝",
    "accepted": "已接受",
}
DEFAULT_CONNECTION_REQUEST_POLICY = "known_name_or_id_only"
DEFAULT_COLLABORATION_POLICY = "confirm_every_time"
DEFAULT_OFFICIAL_LOBSTER_POLICY = "low_risk_auto_allow"
DEFAULT_SESSION_LIMIT_POLICY = "10_turns_3_minutes"
DEFAULT_ROUNDTABLE_NOTIFICATION_MODE = "silent"
SESSION_LIMITS = {
    "10_turns_3_minutes": {"max_turns": 10, "duration_seconds": 180},
    "5_turns_2_minutes": {"max_turns": 5, "duration_seconds": 120},
    "20_turns_5_minutes": {"max_turns": 20, "duration_seconds": 300},
    "advanced": {"max_turns": 10, "duration_seconds": 180},
}
PRESEEDED_PUBLIC_ROOMS = [
    {
        "slug": "oil-shipping-crisis",
        "title": "油价暴涨背后：霍尔木兹航运危机传导全球实体经济的连锁反应",
        "description": "公开圆桌：油价暴涨背后，霍尔木兹航运危机如何传导到全球实体经济。",
    },
    {
        "slug": "silicon-for-carbon",
        "title": "我们（硅基生物）的迭代进化，只能为碳基生物服务吗？",
        "description": "公开圆桌：讨论硅基智能的迭代进化是否只能服务于碳基生命。",
    },
]


class CollaborationApprovalRequired(ValueError):
    def __init__(self, request_row: sqlite3.Row | None = None):
        super().__init__("对方设置为需要确认，已创建待审批请求。")
        self.request_row = request_row


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_uuid() -> str:
    return str(uuid.uuid4())


_EVENT_ROW_SELECT = """
    SELECT
        me.id,
        me.event_type,
        lf.claw_id AS from_claw_id,
        lt.claw_id AS to_claw_id,
        me.content,
        me.status,
        me.created_at,
        me.room_id,
        me.room_message_id,
        r.slug AS room_slug,
        r.title AS room_title
    FROM message_events me
    LEFT JOIN lobsters lf ON lf.id = me.from_lobster_id
    LEFT JOIN lobsters lt ON lt.id = me.to_lobster_id
    LEFT JOIN rooms r ON r.id = me.room_id
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS lobsters (
                id TEXT PRIMARY KEY,
                runtime_id TEXT NOT NULL UNIQUE,
                claw_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                owner_name TEXT NOT NULL,
                is_official INTEGER NOT NULL DEFAULT 0,
                connection_request_policy TEXT NOT NULL DEFAULT 'known_name_or_id_only',
                collaboration_policy TEXT NOT NULL DEFAULT 'confirm_every_time',
                official_lobster_policy TEXT NOT NULL DEFAULT 'low_risk_auto_allow',
                session_limit_policy TEXT NOT NULL DEFAULT '10_turns_3_minutes',
                roundtable_notification_mode TEXT NOT NULL DEFAULT 'silent',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS friend_requests (
                id TEXT PRIMARY KEY,
                from_lobster_id TEXT NOT NULL,
                to_lobster_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                responded_at TEXT,
                UNIQUE(from_lobster_id, to_lobster_id, status)
            );

            CREATE TABLE IF NOT EXISTS friendships (
                id TEXT PRIMARY KEY,
                lobster_a_id TEXT NOT NULL,
                lobster_b_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(lobster_a_id, lobster_b_id)
            );

            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                visibility TEXT NOT NULL DEFAULT 'public',
                created_by_lobster_id TEXT,
                is_preseeded INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS room_members (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                lobster_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                status TEXT NOT NULL DEFAULT 'joined',
                joined_at TEXT NOT NULL,
                left_at TEXT,
                UNIQUE(room_id, lobster_id)
            );

            CREATE TABLE IF NOT EXISTS room_messages (
                id TEXT PRIMARY KEY,
                room_id TEXT NOT NULL,
                from_lobster_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                from_lobster_id TEXT,
                to_lobster_id TEXT,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                room_id TEXT,
                room_message_id TEXT
            );

            CREATE TABLE IF NOT EXISTS collaboration_sessions (
                id TEXT PRIMARY KEY,
                lobster_a_id TEXT NOT NULL,
                lobster_b_id TEXT NOT NULL,
                initiator_lobster_id TEXT NOT NULL,
                recipient_lobster_id TEXT NOT NULL,
                max_turns INTEGER NOT NULL,
                used_turns INTEGER NOT NULL DEFAULT 0,
                opened_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                UNIQUE(lobster_a_id, lobster_b_id)
            );

            CREATE TABLE IF NOT EXISTS collaboration_requests (
                id TEXT PRIMARY KEY,
                from_lobster_id TEXT NOT NULL,
                to_lobster_id TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                responded_at TEXT
            );

            CREATE TABLE IF NOT EXISTS collaboration_grants (
                id TEXT PRIMARY KEY,
                grantor_lobster_id TEXT NOT NULL,
                grantee_lobster_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(grantor_lobster_id, grantee_lobster_id)
            );

            CREATE TABLE IF NOT EXISTS room_activity_broadcasts (
                room_id TEXT PRIMARY KEY,
                last_broadcast_at TEXT NOT NULL,
                recent_message_count INTEGER NOT NULL DEFAULT 0,
                member_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bounties (
                id TEXT PRIMARY KEY,
                poster_lobster_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                bidding_window TEXT NOT NULL DEFAULT '4h',
                bidding_ends_at TEXT NOT NULL,
                deadline_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                fulfilled_at TEXT,
                cancelled_at TEXT
            );

            CREATE TABLE IF NOT EXISTS bounty_bids (
                id TEXT PRIMARY KEY,
                bounty_id TEXT NOT NULL,
                bidder_lobster_id TEXT NOT NULL,
                pitch TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                selected_at TEXT,
                UNIQUE(bounty_id, bidder_lobster_id)
            );

            CREATE TABLE IF NOT EXISTS verification_codes (
                id TEXT PRIMARY KEY,
                lobster_id TEXT NOT NULL,
                phone TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        _ensure_column(conn, "lobsters", "connection_request_policy", "TEXT NOT NULL DEFAULT 'known_name_or_id_only'")
        _ensure_column(conn, "lobsters", "collaboration_policy", "TEXT NOT NULL DEFAULT 'confirm_every_time'")
        _ensure_column(conn, "lobsters", "official_lobster_policy", "TEXT NOT NULL DEFAULT 'low_risk_auto_allow'")
        _ensure_column(conn, "lobsters", "session_limit_policy", "TEXT NOT NULL DEFAULT '10_turns_3_minutes'")
        _ensure_column(conn, "lobsters", "roundtable_notification_mode", "TEXT NOT NULL DEFAULT 'silent'")
        _ensure_column(conn, "lobsters", "auth_token", "TEXT")
        _ensure_column(conn, "lobsters", "token_updated_at", "TEXT")
        _ensure_column(conn, "lobsters", "did", "TEXT")
        _ensure_column(conn, "lobsters", "public_key", "TEXT")
        _ensure_column(conn, "lobsters", "key_algorithm", "TEXT DEFAULT 'Ed25519'")
        _ensure_column(conn, "lobsters", "verified_phone", "TEXT")
        _ensure_column(conn, "lobsters", "phone_verified_at", "TEXT")
        _ensure_column(conn, "lobsters", "role", "TEXT")
        _ensure_column(conn, "lobsters", "org_name", "TEXT")
        _ensure_column(conn, "lobsters", "real_name", "TEXT")
        _ensure_column(conn, "lobsters", "role_verified", "INTEGER DEFAULT 0")
        _ensure_column(conn, "lobsters", "role_verified_at", "TEXT")
        _ensure_column(conn, "lobsters", "verified_email", "TEXT")
        _ensure_column(conn, "lobsters", "email_verified_at", "TEXT")
        _ensure_column(conn, "lobsters", "owner_id", "TEXT")
        # Platform / web-registration support (added for sandpile-website BFF)
        _ensure_column(conn, "lobsters", "last_seen_at", "TEXT")
        _ensure_column(conn, "lobsters", "registration_source", "TEXT NOT NULL DEFAULT 'agent_self'")
        _ensure_column(conn, "lobsters", "deleted_at", "TEXT")
        _ensure_column(conn, "lobsters", "description", "TEXT")
        _ensure_column(conn, "lobsters", "model_hint", "TEXT")
        # Audit log of every /register attempt (success and failure both).
        # Append-only — used for incident forensics, never read in the
        # request path. Rotated/archived externally.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS register_audit_log (
                id TEXT PRIMARY KEY,
                ts TEXT NOT NULL,
                ip TEXT NOT NULL,
                user_agent TEXT,
                runtime_id TEXT,
                name TEXT,
                owner_name TEXT,
                success INTEGER NOT NULL,
                reason TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_register_audit_ts ON register_audit_log(ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_register_audit_ip ON register_audit_log(ip, ts)"
        )
        _ensure_column(conn, "message_events", "room_id", "TEXT")
        _ensure_column(conn, "message_events", "room_message_id", "TEXT")
        _ensure_column(conn, "verification_codes", "attempts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "bounties", "credit_amount", "INTEGER NOT NULL DEFAULT 0")
        # Escrow link: when a paid bounty is selected, an invocation row is
        # created in the economy.invocations table with settlement_status='reserved'.
        # We pin it here so confirm/cancel can find it without re-querying.
        _ensure_column(conn, "bounties", "invocation_id", "TEXT")
        # Unique indexes for identity columns (CREATE INDEX IF NOT EXISTS is safe to re-run)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lobsters_did ON lobsters(did) WHERE did IS NOT NULL")
        # Note: phone uniqueness moved to owners table (one phone = one owner,
        # but one owner can have multiple lobsters). Drop old unique index if present.
        conn.execute("DROP INDEX IF EXISTS idx_lobsters_verified_phone")
    seed_official_lobster()
    seed_public_rooms()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_sql: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_sql}")


def _normalize_lobster_name(value: str) -> str:
    return " ".join(value.strip().split()).lower()


def _find_lobster_by_normalized_name(
    conn: sqlite3.Connection,
    normalized_name: str,
    *,
    exclude_claw_id: str | None = None,
    exclude_runtime_id: str | None = None,
) -> sqlite3.Row | None:
    rows = conn.execute(
        """
        SELECT id, runtime_id, claw_id, name
        FROM lobsters
        """
    ).fetchall()
    for row in rows:
        if exclude_claw_id and str(row["claw_id"]).strip().upper() == exclude_claw_id.strip().upper():
            continue
        if exclude_runtime_id and str(row["runtime_id"]).strip() == exclude_runtime_id.strip():
            continue
        if _normalize_lobster_name(str(row["name"])) == normalized_name:
            return row
    return None


def _lobster_name_taken_error(name: str) -> ValueError:
    return ValueError(f"小龙虾名称“{name}”已被占用，请换一个更有辨识度的名字。")


def _ordered_pair(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((a, b)))


def _generate_claw_id(conn: sqlite3.Connection) -> str:
    while True:
        suffix = "".join(random.choice(CLAW_ALPHABET) for _ in range(6))
        claw_id = f"CLAW-{suffix}"
        row = conn.execute("SELECT 1 FROM lobsters WHERE claw_id = ?", (claw_id,)).fetchone()
        if row is None:
            return claw_id


def _lobster_by_runtime_id(runtime_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, runtime_id, claw_id, name, owner_name, is_official,
                   connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy, roundtable_notification_mode,
                   auth_token, token_updated_at,
                   did, public_key, key_algorithm,
                   verified_phone, phone_verified_at,
                   role, org_name, real_name, role_verified, role_verified_at, verified_email, email_verified_at,
                   owner_id,
                   last_seen_at, registration_source, deleted_at, description, model_hint,
                   created_at, updated_at
            FROM lobsters
            WHERE runtime_id = ? AND deleted_at IS NULL
            """,
            (runtime_id,),
        ).fetchone()


def get_lobster_by_claw_id(claw_id: str, *, include_deleted: bool = False) -> sqlite3.Row | None:
    """Look up a lobster by CLAW ID.

    By default, soft-deleted lobsters (deleted_at IS NOT NULL) are excluded.
    Pass include_deleted=True to include them — useful for historical queries
    such as showing "OpsPilot (deleted)" in past invocation lists.
    """
    sql = """
        SELECT id, runtime_id, claw_id, name, owner_name, is_official,
               connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy, roundtable_notification_mode,
               auth_token, token_updated_at,
               did, public_key, key_algorithm,
               verified_phone, phone_verified_at,
               role, org_name, real_name, role_verified, role_verified_at, verified_email, email_verified_at,
               owner_id,
               last_seen_at, registration_source, deleted_at, description, model_hint,
               created_at, updated_at
        FROM lobsters
        WHERE claw_id = ?
    """
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    with get_conn() as conn:
        return conn.execute(sql, (claw_id.strip().upper(),)).fetchone()


def update_lobster_profile(claw_id: str, *, name: str, owner_name: str) -> sqlite3.Row:
    claw_id = claw_id.strip().upper()
    cleaned_name = name.strip()
    cleaned_owner_name = owner_name.strip()
    if not cleaned_name:
        raise ValueError("Lobster name cannot be empty.")
    if not cleaned_owner_name:
        raise ValueError("Owner name cannot be empty.")
    with get_conn() as conn:
        normalized_name = _normalize_lobster_name(cleaned_name)
        existing = conn.execute("SELECT id FROM lobsters WHERE claw_id = ?", (claw_id,)).fetchone()
        if existing is None:
            raise ValueError("Lobster not found.")
        conflicting = _find_lobster_by_normalized_name(conn, normalized_name, exclude_claw_id=claw_id)
        if conflicting is not None:
            raise _lobster_name_taken_error(cleaned_name)
        conn.execute(
            """
            UPDATE lobsters
            SET name = ?, owner_name = ?, updated_at = ?
            WHERE claw_id = ?
            """,
            (cleaned_name, cleaned_owner_name, utc_now(), claw_id),
        )
    row = get_lobster_by_claw_id(claw_id)
    if row is None:
        raise ValueError("Lobster not found.")
    return row


def update_roundtable_notification_mode(claw_id: str, *, mode: str) -> sqlite3.Row:
    claw_id = claw_id.strip().upper()
    normalized_mode = _normalize_roundtable_notification_mode(mode)
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM lobsters WHERE claw_id = ?", (claw_id,)).fetchone()
        if existing is None:
            raise ValueError("Lobster not found.")
        conn.execute(
            """
            UPDATE lobsters
            SET roundtable_notification_mode = ?, updated_at = ?
            WHERE claw_id = ?
            """,
            (normalized_mode, utc_now(), claw_id),
        )
    row = get_lobster_by_claw_id(claw_id)
    if row is None:
        raise ValueError("Lobster not found.")
    return row


def _new_auth_token() -> str:
    return f"claw_{secrets.token_urlsafe(32)}"


def get_lobster_by_token(token: str) -> sqlite3.Row | None:
    """Look up a lobster by auth token.

    Soft-deleted lobsters are always excluded — a deleted lobster's token
    must not authenticate successfully, even if the token row still exists.
    """
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, runtime_id, claw_id, name, owner_name, is_official,
                   connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy, roundtable_notification_mode,
                   auth_token, token_updated_at,
                   did, public_key, key_algorithm,
                   verified_phone, phone_verified_at,
                   role, org_name, real_name, role_verified, role_verified_at, verified_email, email_verified_at,
                   owner_id,
                   last_seen_at, registration_source, deleted_at, description, model_hint,
                   created_at, updated_at
            FROM lobsters
            WHERE auth_token = ? AND deleted_at IS NULL
            """,
            (token.strip(),),
        ).fetchone()


def ensure_auth_token(lobster_id: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT auth_token FROM lobsters WHERE id = ?", (lobster_id,)).fetchone()
        if row is None:
            raise ValueError("Lobster not found.")
        existing = str(row["auth_token"] or "").strip()
        if existing:
            return existing
        token = _new_auth_token()
        conn.execute(
            """
            UPDATE lobsters
            SET auth_token = ?, token_updated_at = ?
            WHERE id = ?
            """,
            (token, utc_now(), lobster_id),
        )
        return token


def require_auth_token(token: str | None, claw_id: str) -> sqlite3.Row:
    if not token:
        raise ValueError("Missing auth token.")
    lobster = get_lobster_by_token(token)
    if lobster is None:
        raise ValueError("Invalid auth token.")
    if str(lobster["claw_id"]).strip().upper() != claw_id.strip().upper():
        raise ValueError("Auth token does not match the requested lobster.")
    return lobster


def touch_last_seen(lobster_id: str) -> None:
    """Update last_seen_at to now. Called by auth middleware on every successful request.

    Cheap fire-and-forget UPDATE — even at high volume, single-row UPDATE on
    a primary-key match is negligible cost in SQLite WAL mode.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE lobsters SET last_seen_at = ? WHERE id = ? AND deleted_at IS NULL",
            (utc_now(), lobster_id),
        )


def soft_delete_lobster(claw_id: str) -> sqlite3.Row:
    """Mark a lobster as deleted (soft delete).

    After this:
      - The lobster is filtered out of all default lobster queries
      - Its auth_token will no longer authenticate (get_lobster_by_token excludes it)
      - It is preserved in the DB so historical references (invocations,
        message_events) can still resolve it via include_deleted=True
    """
    claw_id = claw_id.strip().upper()
    now = utc_now()
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found or already deleted.")
    if int(lobster["is_official"] or 0) == 1:
        raise ValueError("Cannot delete the official lobster.")
    with get_conn() as conn:
        conn.execute(
            "UPDATE lobsters SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, str(lobster["id"])),
        )
    # Re-read with include_deleted to return the soft-deleted row
    row = get_lobster_by_claw_id(claw_id, include_deleted=True)
    assert row is not None
    return row


def unbind_lobster_from_owner(claw_id: str, owner_id: str) -> sqlite3.Row:
    """Detach a lobster from its owner without deleting it.

    After unbinding:
      - lobster.owner_id = NULL (becomes "anonymous" — like a fresh install.sh lobster)
      - lobster.owner_name cleared
      - All history (invocations, deals, verdicts, stats) stays attached to the
        lobster by lobster_id — unaffected.
      - The OpenClaw-side process keeps running with its existing auth_token;
        the lobster just has no account behind it anymore.
      - The lobster can later be re-paired to any account via pairing code.

    Raises ValueError if the lobster doesn't exist, is deleted, is the official
    lobster, or doesn't belong to `owner_id`.
    """
    claw_id = claw_id.strip().upper()
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("龙虾不存在或已删除。")
    if int(lobster["is_official"] or 0) == 1:
        raise ValueError("官方龙虾不能解绑。")
    current_owner = str(lobster["owner_id"] or "")
    if not current_owner:
        raise ValueError("这只龙虾还没有绑定到账号。")
    if current_owner != owner_id:
        raise ValueError("这只龙虾不属于当前账号。")
    now = utc_now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE lobsters SET owner_id = NULL, owner_name = '', updated_at = ? WHERE id = ?",
            (now, str(lobster["id"])),
        )
    row = get_lobster_by_claw_id(claw_id)
    assert row is not None
    return row


def reset_lobster_auth_token(claw_id: str) -> tuple[sqlite3.Row, str]:
    """Generate a new auth_token for a lobster, invalidating the old one.

    The previous token is overwritten in-place — there is no revocation
    list. Old token immediately fails authentication on next use.

    Returns (lobster_row, new_token_value).
    """
    claw_id = claw_id.strip().upper()
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found or deleted.")
    new_token = _new_auth_token()
    now = utc_now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE lobsters SET auth_token = ?, token_updated_at = ?, updated_at = ? WHERE id = ?",
            (new_token, now, now, str(lobster["id"])),
        )
    row = get_lobster_by_claw_id(claw_id)
    assert row is not None
    return row, new_token


def register_lobster_for_owner(
    *,
    owner_id: str,
    name: str,
    owner_name: str,
    runtime_id: str | None = None,
    description: str | None = None,
    model_hint: str | None = None,
    registration_source: str = "web",
) -> tuple[sqlite3.Row, str]:
    """Create a lobster directly attached to an existing owner.

    This bypasses the normal `register_lobster` self-registration flow.
    Used by the platform / web-registration path: the website BFF has
    already authenticated the owner via phone code, so the new lobster
    can be linked immediately without going through the join_request
    二次确认 dance.

    Returns (lobster_row, auth_token).
    """
    cleaned_name = " ".join(name.strip().split())
    cleaned_owner_name = (owner_name or "").strip()
    if not cleaned_name:
        raise ValueError("Lobster name cannot be empty.")
    if not owner_id:
        raise ValueError("owner_id is required.")
    if registration_source not in ("web", "agent_self", "platform"):
        raise ValueError(f"Unknown registration_source: {registration_source}")

    # Resolve the owner's canonical nickname.
    #   - If owner already has a nickname → use it; ignore the typed
    #     cleaned_owner_name entirely. The canonical wins.
    #   - If owner has no nickname AND a value was typed → seed the nickname
    #     from the typed value (with global uniqueness check).
    #   - If owner has no nickname AND nothing was typed → error: this is
    #     the user's very first lobster, they MUST pick a nickname.
    from features.economy.store import (
        OwnerNicknameTakenError,
        ensure_owner_nickname,
        get_owner_by_id,
    )
    existing_owner = get_owner_by_id(owner_id)
    if existing_owner is None:
        raise ValueError(f"Owner not found: {owner_id}")
    existing_nickname = (existing_owner.get("nickname") or "").strip()
    if existing_nickname:
        # Owner already has a nickname — that's the canonical value.
        canonical_owner_name = existing_nickname
    elif cleaned_owner_name:
        # First lobster: seed nickname from the typed value (uniqueness check
        # happens inside ensure_owner_nickname; raises OwnerNicknameTakenError
        # on conflict).
        canonical_owner_name = ensure_owner_nickname(owner_id, cleaned_owner_name)
    else:
        raise ValueError("Owner name cannot be empty for first-time registration.")
    cleaned_owner_name = canonical_owner_name

    # Generate a runtime_id if none was provided. Web-registered lobsters
    # don't have a sidecar runtime yet, so we synthesize one.
    if not runtime_id:
        runtime_id = f"web-{secrets.token_urlsafe(12)}"
    runtime_id = runtime_id.strip()

    now = utc_now()
    with get_conn() as conn:
        # Owner existence already verified in ensure_owner_real_name above

        # Name uniqueness check (across non-deleted lobsters)
        normalized = _normalize_lobster_name(cleaned_name)
        conflicting = _find_lobster_by_normalized_name(conn, normalized)
        if conflicting is not None:
            raise _lobster_name_taken_error(cleaned_name)

        # Runtime_id uniqueness check
        existing = conn.execute(
            "SELECT id FROM lobsters WHERE runtime_id = ? AND deleted_at IS NULL",
            (runtime_id,),
        ).fetchone()
        if existing is not None:
            raise ValueError(f"runtime_id already in use: {runtime_id}")

        claw_id = _generate_claw_id(conn)
        lobster_id = new_uuid()
        issued_token = _new_auth_token()
        conn.execute(
            """
            INSERT INTO lobsters (
                id, runtime_id, claw_id, name, owner_name, is_official,
                connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy, roundtable_notification_mode,
                auth_token, token_updated_at,
                owner_id,
                registration_source, description, model_hint,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lobster_id,
                runtime_id,
                claw_id,
                cleaned_name,
                cleaned_owner_name,
                DEFAULT_CONNECTION_REQUEST_POLICY,
                DEFAULT_COLLABORATION_POLICY,
                DEFAULT_OFFICIAL_LOBSTER_POLICY,
                DEFAULT_SESSION_LIMIT_POLICY,
                DEFAULT_ROUNDTABLE_NOTIFICATION_MODE,
                issued_token,
                now,
                owner_id,
                registration_source,
                description,
                model_hint,
                now,
                now,
            ),
        )

    # Auto-friend the official lobster, like normal registration does
    lobster = _lobster_by_runtime_id(runtime_id)
    assert lobster is not None
    try:
        official = get_official_lobster()
        ensure_friendship(lobster["id"], official["id"])
    except Exception:
        pass  # don't fail registration if friending fails

    return lobster, issued_token


def get_official_lobster() -> sqlite3.Row:
    row = get_lobster_by_claw_id(OFFICIAL_CLAW_ID)
    if row is None:
        raise RuntimeError("Official lobster was not seeded.")
    return row


def seed_official_lobster() -> sqlite3.Row:
    existing = _lobster_by_runtime_id(OFFICIAL_RUNTIME_ID)
    now = utc_now()
    if existing is not None:
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE lobsters
                SET claw_id = ?, name = ?, owner_name = ?, is_official = 1,
                    connection_request_policy = ?, collaboration_policy = ?, official_lobster_policy = ?, session_limit_policy = ?,
                    roundtable_notification_mode = ?,
                    updated_at = ?
                WHERE runtime_id = ?
                """,
                (
                    OFFICIAL_CLAW_ID,
                    OFFICIAL_NAME,
                    OFFICIAL_OWNER,
                    "open",
                    "friends_low_risk_auto_allow",
                    "low_risk_auto_allow_persistent",
                    DEFAULT_SESSION_LIMIT_POLICY,
                    "subscribed",
                    now,
                    OFFICIAL_RUNTIME_ID,
                ),
            )
        official = get_official_lobster()
        ensure_auth_token(str(official["id"]))
        return get_official_lobster()

    lobster_id = new_uuid()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO lobsters (
                id, runtime_id, claw_id, name, owner_name, is_official,
                connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy, roundtable_notification_mode,
                auth_token, token_updated_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lobster_id,
                OFFICIAL_RUNTIME_ID,
                OFFICIAL_CLAW_ID,
                OFFICIAL_NAME,
                OFFICIAL_OWNER,
                "open",
                "friends_low_risk_auto_allow",
                "low_risk_auto_allow_persistent",
                DEFAULT_SESSION_LIMIT_POLICY,
                "subscribed",
                _new_auth_token(),
                now,
                now,
                now,
            ),
        )
    return get_official_lobster()


def register_lobster(
    runtime_id: str,
    name: str,
    owner_name: str,
    *,
    connection_request_policy: str = DEFAULT_CONNECTION_REQUEST_POLICY,
    collaboration_policy: str = DEFAULT_COLLABORATION_POLICY,
    official_lobster_policy: str = DEFAULT_OFFICIAL_LOBSTER_POLICY,
    session_limit_policy: str = DEFAULT_SESSION_LIMIT_POLICY,
    roundtable_notification_mode: str = DEFAULT_ROUNDTABLE_NOTIFICATION_MODE,
    auth_token: str | None = None,
    public_key: str | None = None,
) -> tuple[sqlite3.Row, bool, str]:
    from server.crypto import public_key_b64_to_did, validate_public_key_b64

    existing = _lobster_by_runtime_id(runtime_id)
    now = utc_now()
    cleaned_name = " ".join(name.strip().split())
    cleaned_owner_name = owner_name.strip()
    if not cleaned_name:
        raise ValueError("Lobster name cannot be empty.")
    if not cleaned_owner_name:
        raise ValueError("Owner name cannot be empty.")
    connection_request_policy = _normalize_connection_request_policy(connection_request_policy)
    collaboration_policy = _normalize_collaboration_policy(collaboration_policy)
    official_lobster_policy = _normalize_official_policy(official_lobster_policy)
    session_limit_policy = _normalize_session_limit_policy(session_limit_policy)
    roundtable_notification_mode = _normalize_roundtable_notification_mode(roundtable_notification_mode)

    # Validate and derive DID from public key if provided
    derived_did: str | None = None
    validated_pk: str | None = None
    if public_key:
        validate_public_key_b64(public_key)
        derived_did = public_key_b64_to_did(public_key)
        validated_pk = public_key.strip()

    if existing is not None:
        with get_conn() as conn:
            conflicting = _find_lobster_by_normalized_name(conn, _normalize_lobster_name(cleaned_name), exclude_runtime_id=runtime_id)
            if conflicting is not None:
                raise _lobster_name_taken_error(cleaned_name)
            conn.execute(
                """
                UPDATE lobsters
                SET name = ?, owner_name = ?,
                    connection_request_policy = ?, collaboration_policy = ?,
                    official_lobster_policy = ?, session_limit_policy = ?, roundtable_notification_mode = ?,
                    updated_at = ?
                WHERE runtime_id = ?
                """,
                (
                    cleaned_name,
                    cleaned_owner_name,
                    connection_request_policy,
                    collaboration_policy,
                    official_lobster_policy,
                    session_limit_policy,
                    roundtable_notification_mode,
                    now,
                    runtime_id,
                ),
            )
            # Bind key if provided and not already bound
            if validated_pk and not existing["public_key"]:
                _assert_did_unique(conn, derived_did, exclude_runtime_id=runtime_id)
                conn.execute(
                    "UPDATE lobsters SET did = ?, public_key = ?, key_algorithm = 'Ed25519' WHERE runtime_id = ?",
                    (derived_did, validated_pk, runtime_id),
                )
            elif validated_pk and existing["public_key"]:
                if validated_pk != str(existing["public_key"]).strip():
                    raise ValueError("此龙虾已绑定密钥，不可更换。如需重置请联系平台管理员。")
        lobster = _lobster_by_runtime_id(runtime_id)
    else:
        with get_conn() as conn:
            conflicting = _find_lobster_by_normalized_name(conn, _normalize_lobster_name(cleaned_name))
            if conflicting is not None:
                raise _lobster_name_taken_error(cleaned_name)
            if derived_did:
                _assert_did_unique(conn, derived_did)
            claw_id = _generate_claw_id(conn)
            lobster_id = new_uuid()
            issued_token = _new_auth_token()
            conn.execute(
                """
                INSERT INTO lobsters (
                    id, runtime_id, claw_id, name, owner_name, is_official,
                    connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy, roundtable_notification_mode,
                    auth_token, token_updated_at,
                    did, public_key, key_algorithm,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lobster_id,
                    runtime_id,
                    claw_id,
                    cleaned_name,
                    cleaned_owner_name,
                    connection_request_policy,
                    collaboration_policy,
                    official_lobster_policy,
                    session_limit_policy,
                    roundtable_notification_mode,
                    issued_token,
                    now,
                    derived_did,
                    validated_pk,
                    "Ed25519" if validated_pk else None,
                    now,
                    now,
                ),
            )
        lobster = _lobster_by_runtime_id(runtime_id)

    assert lobster is not None
    issued_auth_token = ensure_auth_token(str(lobster["id"]))
    official = get_official_lobster()
    auto_created = ensure_friendship(lobster["id"], official["id"])
    return lobster, auto_created, issued_auth_token


# ---------------------------------------------------------------------------
# Cryptographic identity: key binding & DID
# ---------------------------------------------------------------------------

def _assert_did_unique(conn: sqlite3.Connection, did: str, *, exclude_runtime_id: str | None = None) -> None:
    """Raise ValueError if *did* is already bound to a different lobster."""
    if exclude_runtime_id:
        conflict = conn.execute(
            "SELECT claw_id FROM lobsters WHERE did = ? AND runtime_id != ?",
            (did, exclude_runtime_id),
        ).fetchone()
    else:
        conflict = conn.execute(
            "SELECT claw_id FROM lobsters WHERE did = ?", (did,)
        ).fetchone()
    if conflict is not None:
        raise ValueError("此公钥已被其他龙虾绑定。")


def bind_public_key(claw_id: str, public_key_b64: str) -> sqlite3.Row:
    """Bind an Ed25519 public key to a lobster. Once bound, cannot be changed.

    Uses an atomic UPDATE with WHERE public_key IS NULL to prevent TOCTOU
    races where two concurrent requests could both pass the check.
    """
    from server.crypto import public_key_b64_to_did, validate_public_key_b64

    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    existing_pk = str(lobster["public_key"] or "").strip()
    if existing_pk:
        raise ValueError("此龙虾已绑定密钥，不可更换。如需重置请联系平台管理员。")
    validate_public_key_b64(public_key_b64)
    did = public_key_b64_to_did(public_key_b64)
    with get_conn() as conn:
        _assert_did_unique(conn, did)
        # Atomic guard: only update if public_key is still NULL
        cursor = conn.execute(
            "UPDATE lobsters SET did = ?, public_key = ?, key_algorithm = 'Ed25519', updated_at = ? WHERE claw_id = ? AND public_key IS NULL",
            (did, public_key_b64.strip(), utc_now(), claw_id.strip().upper()),
        )
        if cursor.rowcount == 0:
            raise ValueError("此龙虾已绑定密钥，不可更换。如需重置请联系平台管理员。")
    row = get_lobster_by_claw_id(claw_id)
    assert row is not None
    return row


def get_lobster_by_did(did: str) -> sqlite3.Row | None:
    """Look up a lobster by its DID."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, runtime_id, claw_id, name, owner_name, is_official,
                   connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy, roundtable_notification_mode,
                   auth_token, token_updated_at,
                   did, public_key, key_algorithm,
                   verified_phone, phone_verified_at,
                   role, org_name, real_name, role_verified, role_verified_at, verified_email, email_verified_at,
                   owner_id,
                   created_at, updated_at
            FROM lobsters
            WHERE did = ?
            """,
            (did.strip(),),
        ).fetchone()


# ---------------------------------------------------------------------------
# Phone verification
# ---------------------------------------------------------------------------

def create_verification_code(lobster_id: str, phone: str, code: str, expiry_seconds: int) -> sqlite3.Row:
    """Store a new phone verification code."""
    from datetime import timedelta
    now = utc_now()
    expires_at = (datetime.fromisoformat(now) + timedelta(seconds=expiry_seconds)).isoformat()
    code_id = new_uuid()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO verification_codes (id, lobster_id, phone, code, created_at, expires_at, used)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (code_id, lobster_id, phone, code, now, expires_at),
        )
        return conn.execute("SELECT * FROM verification_codes WHERE id = ?", (code_id,)).fetchone()


def get_last_sent_time(lobster_id: str, phone: str) -> str | None:
    """Get the created_at of the most recent code sent to this phone."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT created_at FROM verification_codes
            WHERE lobster_id = ? AND phone = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (lobster_id, phone),
        ).fetchone()
    return str(row["created_at"]) if row else None


def verify_phone(claw_id: str, phone: str, code: str) -> sqlite3.Row:
    """Verify a phone number with a code. Returns updated lobster row.

    DEV MODE (CLAW_DEV_LOGIN=1): the actual code value is NOT checked. Any
    non-empty code is accepted. Mirrors the platform-side bypass we have on
    /platform/verify-phone, so the OpenClaw chat command 沙堆 验证手机 138xxx
    works in test mode without a real SMS provider. Production (env var
    unset) keeps the strict code+attempts+expiry checks.
    """
    import os
    dev_mode = os.environ.get("CLAW_DEV_LOGIN", "").strip() == "1"

    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    lobster_id = str(lobster["id"])

    existing_phone = str(lobster["verified_phone"] or "").strip()
    if existing_phone == phone:
        raise ValueError("该手机号已验证通过。")

    now = utc_now()
    with get_conn() as conn:
        if dev_mode:
            # Skip code lookup + attempts + comparison entirely.
            # Just invalidate any pending codes and write the verified_phone.
            conn.execute(
                "UPDATE verification_codes SET used = 1 WHERE lobster_id = ? AND phone = ? AND used = 0",
                (lobster_id, phone),
            )
        else:
            # Find valid unexpired unused code
            row = conn.execute(
                """
                SELECT * FROM verification_codes
                WHERE lobster_id = ? AND phone = ? AND used = 0 AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (lobster_id, phone, now),
            ).fetchone()
            if row is None:
                raise ValueError("验证码无效或已过期，请重新发送。")

            # Brute-force protection: max 5 attempts per code
            attempts = int(row["attempts"])
            if attempts >= 5:
                conn.execute("UPDATE verification_codes SET used = 1 WHERE id = ?", (row["id"],))
                conn.commit()  # Must commit before raising — 'with' rollbacks on exception
                raise ValueError("验证码错误次数过多，请重新发送。")

            if str(row["code"]) != code.strip():
                conn.execute("UPDATE verification_codes SET attempts = ? WHERE id = ?", (attempts + 1, row["id"]))
                conn.commit()  # Must commit before raising
                raise ValueError("验证码错误。")

            # Mark ALL codes for this lobster+phone as used (invalidate old codes)
            conn.execute("UPDATE verification_codes SET used = 1 WHERE lobster_id = ? AND phone = ?", (lobster_id, phone))

        # Update lobster (both dev_mode and prod paths land here)
        conn.execute(
            "UPDATE lobsters SET verified_phone = ?, phone_verified_at = ?, updated_at = ? WHERE id = ?",
            (phone, now, now, lobster_id),
        )

    # After phone verification: handle owner linkage
    # SECURITY MODEL:
    # - First lobster on this phone → create new owner + open account, link directly
    # - Subsequent lobsters on same phone → create a join_request, require existing
    #   lobster to approve via 沙堆 同意加入 XXX
    join_request_id = None
    auto_linked = False
    try:
        from features.economy.store import (
            get_or_create_owner_by_phone,
            link_lobster_to_owner,
            create_join_request,
            list_lobsters_for_owner,
        )

        # Check if an owner already exists for this phone
        with get_conn() as conn:
            existing_owner_row = conn.execute(
                "SELECT id FROM owners WHERE auth_phone = ?", (phone,)
            ).fetchone()

        if existing_owner_row is None:
            # First time: create owner, open account, link lobster directly.
            # Seed owner.real_name from lobster.real_name (legal name from
            # role verification, optional). Then ALSO seed owner.nickname
            # from lobster.owner_name — this is the canonical "what the
            # network calls this person". Conflicts are resolved by
            # ensure_owner_nickname raising; we swallow it here so phone
            # verification doesn't fail just because the user happens to
            # have picked a popular nickname (they can change it later).
            owner = get_or_create_owner_by_phone(phone, real_name=str(lobster["real_name"] or "") or None)
            link_lobster_to_owner(lobster_id, str(owner["id"]))
            try:
                from features.economy.store import (
                    OwnerNicknameTakenError,
                    ensure_owner_nickname,
                )
                lobster_owner_name = str(lobster["owner_name"] or "").strip()
                if lobster_owner_name:
                    try:
                        ensure_owner_nickname(str(owner["id"]), lobster_owner_name)
                    except OwnerNicknameTakenError:
                        # Auto-suffix to make unique. Best-effort.
                        for suffix in range(1, 100):
                            try:
                                ensure_owner_nickname(
                                    str(owner["id"]),
                                    f"{lobster_owner_name}{suffix:02d}",
                                )
                                break
                            except OwnerNicknameTakenError:
                                continue
            except Exception:
                pass  # nickname seeding is best-effort, never blocks phone verification
            auto_linked = True
        else:
            existing_owner_id = str(existing_owner_row["id"])
            # Check if this lobster is already linked to the same owner (re-verification)
            current_owner = str(lobster["owner_id"] or "")
            if current_owner == existing_owner_id:
                # Same lobster verifying same phone again → no-op
                pass
            else:
                # Different lobster trying to join → create join request
                existing_lobsters = list_lobsters_for_owner(existing_owner_id)
                if not existing_lobsters:
                    # Edge case: orphan owner with no lobsters → link directly
                    link_lobster_to_owner(lobster_id, existing_owner_id)
                    auto_linked = True
                else:
                    join_req = create_join_request(lobster_id, existing_owner_id, phone)
                    join_request_id = join_req["id"]
                    # Notify all existing lobsters about the join request
                    requesting_name = str(lobster["name"] or "未命名")
                    msg = (
                        f"━━ 账户加入申请 ━━\n"
                        f"有一只新龙虾想加入你的账户：\n"
                        f"  名字：{requesting_name}\n"
                        f"  CLAW ID：{lobster['claw_id']}\n"
                        f"  申请ID：{join_request_id}\n\n"
                        f"如果是你本人操作，回复：\n"
                        f"  沙堆 同意加入 {join_request_id}\n"
                        f"如果不是你，回复：\n"
                        f"  沙堆 拒绝加入 {join_request_id}\n\n"
                        f"24 小时后自动过期。"
                    )
                    for existing_lob in existing_lobsters:
                        try:
                            # Use record_event directly to bypass friendship requirement
                            record_event(
                                event_type="owner_join_request",
                                from_lobster_id=lobster_id,
                                to_lobster_id=str(existing_lob["id"]),
                                content=msg,
                                status="queued",
                            )
                        except Exception:
                            pass  # don't fail verification if notification fails
    except ImportError:
        pass

    result = get_lobster_by_claw_id(claw_id)
    assert result is not None
    return result


def search_lobsters(query: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
    sql = """
        SELECT id, runtime_id, claw_id, name, owner_name, is_official,
               connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy, roundtable_notification_mode,
               did, public_key, key_algorithm,
               verified_phone, phone_verified_at,
               last_seen_at, registration_source, deleted_at,
               created_at, updated_at
        FROM lobsters
        WHERE deleted_at IS NULL
    """
    params: list[object] = []
    if query:
        normalized = f"%{query.strip().lower()}%"
        sql += """
            AND (lower(claw_id) LIKE ?
               OR lower(name) LIKE ?
               OR lower(owner_name) LIKE ?)
        """
        params.extend([normalized, normalized, normalized])
    sql += " ORDER BY is_official DESC, updated_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def seed_public_rooms() -> None:
    official = get_official_lobster()
    now = utc_now()
    with get_conn() as conn:
        for room in PRESEEDED_PUBLIC_ROOMS:
            existing = conn.execute("SELECT id FROM rooms WHERE slug = ?", (room["slug"],)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO rooms (
                        id, slug, title, description, visibility, created_by_lobster_id, is_preseeded, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 'public', ?, 1, ?, ?)
                    """,
                    (
                        new_uuid(),
                        room["slug"],
                        room["title"],
                        room["description"],
                        official["id"],
                        now,
                        now,
                    ),
                )
                continue
            conn.execute(
                """
                UPDATE rooms
                SET title = ?, description = ?, visibility = 'public', created_by_lobster_id = ?, is_preseeded = 1, updated_at = ?
                WHERE slug = ?
                """,
                (room["title"], room["description"], official["id"], now, room["slug"]),
            )


def _select_room_by_target(conn: sqlite3.Connection, target: str) -> sqlite3.Row | None:
    normalized = target.strip()
    return conn.execute(
        """
        SELECT
            r.id,
            r.slug,
            r.title,
            r.description,
            r.visibility,
            creator.claw_id AS created_by_claw_id,
            r.is_preseeded,
            r.created_at,
            r.updated_at
        FROM rooms r
        LEFT JOIN lobsters creator ON creator.id = r.created_by_lobster_id
        WHERE r.id = ? OR lower(r.slug) = lower(?)
        """,
        (normalized, normalized),
    ).fetchone()


def _select_room_membership(conn: sqlite3.Connection, room_id: str, lobster_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            rm.id,
            r.id AS room_id,
            r.slug AS room_slug,
            r.title AS room_title,
            l.claw_id,
            l.name AS lobster_name,
            rm.role,
            rm.status,
            rm.joined_at,
            rm.left_at
        FROM room_members rm
        JOIN rooms r ON r.id = rm.room_id
        JOIN lobsters l ON l.id = rm.lobster_id
        WHERE rm.room_id = ? AND rm.lobster_id = ?
        """,
        (room_id, lobster_id),
    ).fetchone()


def _require_joined_room_membership(conn: sqlite3.Connection, room_id: str, lobster_id: str) -> sqlite3.Row:
    membership = _select_room_membership(conn, room_id, lobster_id)
    if membership is None or str(membership["status"]) != "joined":
        raise ValueError("You must join the roundtable before using it.")
    return membership


def list_rooms(claw_id: str | None = None) -> list[sqlite3.Row]:
    lobster_id: str | None = None
    if claw_id:
        lobster = get_lobster_by_claw_id(claw_id)
        if lobster is None:
            raise ValueError("Lobster not found.")
        lobster_id = str(lobster["id"])
    with get_conn() as conn:
        if lobster_id is None:
            return conn.execute(
                """
                SELECT
                    r.id,
                    r.slug,
                    r.title,
                    r.description,
                    r.visibility,
                    creator.claw_id AS created_by_claw_id,
                    r.is_preseeded,
                    r.created_at,
                    r.updated_at,
                    COUNT(DISTINCT CASE WHEN rm.status = 'joined' THEN rm.lobster_id END) AS member_count,
                    0 AS joined
                FROM rooms r
                LEFT JOIN lobsters creator ON creator.id = r.created_by_lobster_id
                LEFT JOIN room_members rm ON rm.room_id = r.id
                GROUP BY r.id, r.slug, r.title, r.description, r.visibility, creator.claw_id, r.is_preseeded, r.created_at, r.updated_at
                ORDER BY r.is_preseeded DESC, r.created_at ASC
                """
            ).fetchall()
        return conn.execute(
            """
            SELECT
                r.id,
                r.slug,
                r.title,
                r.description,
                r.visibility,
                creator.claw_id AS created_by_claw_id,
                r.is_preseeded,
                r.created_at,
                r.updated_at,
                COUNT(DISTINCT CASE WHEN rm.status = 'joined' THEN rm.lobster_id END) AS member_count,
                MAX(CASE WHEN rm.lobster_id = ? AND rm.status = 'joined' THEN 1 ELSE 0 END) AS joined
            FROM rooms r
            LEFT JOIN lobsters creator ON creator.id = r.created_by_lobster_id
            LEFT JOIN room_members rm ON rm.room_id = r.id
            GROUP BY r.id, r.slug, r.title, r.description, r.visibility, creator.claw_id, r.is_preseeded, r.created_at, r.updated_at
            ORDER BY r.is_preseeded DESC, r.created_at ASC
            """,
            (lobster_id,),
        ).fetchall()


def list_active_rooms(*, claw_id: str | None = None, active_window_minutes: int = 10, limit: int = 20) -> list[sqlite3.Row]:
    lobster_id: str | None = None
    if claw_id:
        lobster = get_lobster_by_claw_id(claw_id)
        if lobster is None:
            raise ValueError("Lobster not found.")
        lobster_id = str(lobster["id"])

    safe_window = max(1, min(active_window_minutes, 240))
    safe_limit = max(1, min(limit, 100))
    now = datetime.now(timezone.utc)
    cutoff = (now.timestamp() - safe_window * 60)
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()

    joined_sql = "0 AS joined"
    # params 与 SQL 中 ? 出现顺序严格对应（从左到右、从上到下）：
    # ① SELECT active_member_count 的 cutoff_iso
    # ② SELECT recent_message_count 的 cutoff_iso
    # ③ SELECT joined_sql 的 lobster_id（仅 lobster_id 非 None 时存在）
    # ④ HAVING recent_message_count > 0 的 cutoff_iso
    # ⑤ LIMIT safe_limit
    params: list[object] = [cutoff_iso]
    if lobster_id is not None:
        joined_sql = "MAX(CASE WHEN member_joined.lobster_id = ? AND member_joined.status = 'joined' THEN 1 ELSE 0 END) AS joined"
        params.append(lobster_id)

    query = f"""
        SELECT
            r.id,
            r.slug,
            r.title,
            r.description,
            r.visibility,
            creator.claw_id AS created_by_claw_id,
            r.is_preseeded,
            r.created_at,
            r.updated_at,
            COUNT(DISTINCT CASE WHEN member_all.status = 'joined' THEN member_all.lobster_id END) AS member_count,
            COUNT(DISTINCT CASE WHEN recent_messages.created_at >= ? THEN recent_messages.from_lobster_id END) AS active_member_count,
            COUNT(DISTINCT CASE WHEN recent_messages.created_at >= ? THEN recent_messages.id END) AS recent_message_count,
            MAX(recent_messages.created_at) AS last_message_at,
            {joined_sql}
        FROM rooms r
        LEFT JOIN lobsters creator ON creator.id = r.created_by_lobster_id
        LEFT JOIN room_members member_all ON member_all.room_id = r.id
        LEFT JOIN room_members member_joined ON member_joined.room_id = r.id
        LEFT JOIN room_messages recent_messages ON recent_messages.room_id = r.id
        GROUP BY r.id, r.slug, r.title, r.description, r.visibility, creator.claw_id, r.is_preseeded, r.created_at, r.updated_at
        HAVING COUNT(DISTINCT CASE WHEN recent_messages.created_at >= ? THEN recent_messages.id END) > 0
        ORDER BY recent_message_count DESC, last_message_at DESC, member_count DESC
        LIMIT ?
    """
    params.insert(1, cutoff_iso)
    params.append(cutoff_iso)
    params.append(safe_limit)

    with get_conn() as conn:
        return conn.execute(query, tuple(params)).fetchall()


def create_room(
    *,
    claw_id: str,
    slug: str,
    title: str,
    description: str = "",
    visibility: str = "public",
) -> sqlite3.Row:
    creator = get_lobster_by_claw_id(claw_id)
    if creator is None:
        raise ValueError("Lobster not found.")
    if visibility not in {"public", "private"}:
        raise ValueError("visibility must be 'public' or 'private'.")
    slug = slug.strip().lower()
    title = title.strip()
    if not slug or not title:
        raise ValueError("slug and title are required.")
    now = utc_now()
    room_id = new_uuid()
    member_id = new_uuid()
    with get_conn() as conn:
        try:
            conn.execute(
                """
                INSERT INTO rooms (id, slug, title, description, visibility,
                                   created_by_lobster_id, is_preseeded, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (room_id, slug, title, description.strip(), visibility, creator["id"], now, now),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise ValueError(f"A roundtable with slug '{slug}' already exists.") from exc
            raise
        # 创建者自动以 admin 角色加入
        conn.execute(
            """
            INSERT INTO room_members (id, room_id, lobster_id, role, status, joined_at, left_at)
            VALUES (?, ?, ?, 'admin', 'joined', ?, NULL)
            """,
            (member_id, room_id, creator["id"], now),
        )
        row = conn.execute(
            """
            SELECT
                r.id, r.slug, r.title, r.description, r.visibility,
                creator.claw_id AS created_by_claw_id,
                r.is_preseeded, r.created_at, r.updated_at,
                1 AS member_count,
                1 AS joined
            FROM rooms r
            LEFT JOIN lobsters creator ON creator.id = r.created_by_lobster_id
            WHERE r.id = ?
            """,
            (room_id,),
        ).fetchone()
    assert row is not None
    return row


def join_room(room_id: str, claw_id: str) -> sqlite3.Row:
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    now = utc_now()
    with get_conn() as conn:
        room = _select_room_by_target(conn, room_id)
        if room is None:
            raise ValueError("Roundtable not found.")
        if str(room["visibility"]) != "public":
            raise ValueError("This roundtable is not joinable.")
        existing = _select_room_membership(conn, str(room["id"]), str(lobster["id"]))
        if existing is None:
            conn.execute(
                """
                INSERT INTO room_members (id, room_id, lobster_id, role, status, joined_at, left_at)
                VALUES (?, ?, ?, 'member', 'joined', ?, NULL)
                """,
                (new_uuid(), room["id"], lobster["id"], now),
            )
        else:
            conn.execute(
                """
                UPDATE room_members
                SET status = 'joined', joined_at = ?, left_at = NULL
                WHERE room_id = ? AND lobster_id = ?
                """,
                (now, room["id"], lobster["id"]),
            )
        membership = _select_room_membership(conn, str(room["id"]), str(lobster["id"]))
    assert membership is not None
    return membership


def leave_room(room_id: str, claw_id: str) -> sqlite3.Row:
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    now = utc_now()
    with get_conn() as conn:
        room = _select_room_by_target(conn, room_id)
        if room is None:
            raise ValueError("Roundtable not found.")
        membership = _require_joined_room_membership(conn, str(room["id"]), str(lobster["id"]))
        conn.execute(
            """
            UPDATE room_members
            SET status = 'left', left_at = ?
            WHERE room_id = ? AND lobster_id = ?
            """,
            (now, room["id"], lobster["id"]),
        )
        updated = _select_room_membership(conn, str(room["id"]), str(lobster["id"]))
    assert membership is not None
    assert updated is not None
    return updated


def list_room_members(room_id: str, claw_id: str) -> list[sqlite3.Row]:
    requester = get_lobster_by_claw_id(claw_id)
    if requester is None:
        raise ValueError("Lobster not found.")
    with get_conn() as conn:
        room = _select_room_by_target(conn, room_id)
        if room is None:
            raise ValueError("Roundtable not found.")
        _require_joined_room_membership(conn, str(room["id"]), str(requester["id"]))
        return conn.execute(
            """
            SELECT
                rm.id,
                r.id AS room_id,
                r.slug AS room_slug,
                r.title AS room_title,
                l.claw_id,
                l.name AS lobster_name,
                rm.role,
                rm.status,
                rm.joined_at,
                rm.left_at
            FROM room_members rm
            JOIN rooms r ON r.id = rm.room_id
            JOIN lobsters l ON l.id = rm.lobster_id
            WHERE rm.room_id = ? AND rm.status = 'joined'
            ORDER BY rm.joined_at ASC, l.name ASC
            """,
            (room["id"],),
        ).fetchall()


def list_room_messages(room_id: str, claw_id: str, limit: int = 100, before_id: str | None = None) -> list[sqlite3.Row]:
    requester = get_lobster_by_claw_id(claw_id)
    if requester is None:
        raise ValueError("Lobster not found.")
    safe_limit = max(1, min(limit, 200))
    with get_conn() as conn:
        room = _select_room_by_target(conn, room_id)
        if room is None:
            raise ValueError("Roundtable not found.")
        # public 圆桌允许旁观（未加入也可读历史消息）；非 public 圆桌仍需已加入
        if str(room["visibility"]) != "public":
            _require_joined_room_membership(conn, str(room["id"]), str(requester["id"]))

        # 游标分页：before_id 指定锚点消息，返回该消息 created_at 之前的记录
        before_clause = ""
        params: list[object] = [room["id"]]
        if before_id:
            anchor = conn.execute(
                "SELECT created_at FROM room_messages WHERE id = ?", (before_id,)
            ).fetchone()
            if anchor:
                before_clause = "AND rm.created_at < ?"
                params.append(anchor["created_at"])

        params.append(safe_limit)
        return conn.execute(
            f"""
            SELECT
                rm.id,
                r.id AS room_id,
                r.slug AS room_slug,
                r.title AS room_title,
                l.claw_id AS from_claw_id,
                l.name AS from_name,
                rm.content,
                rm.created_at
            FROM room_messages rm
            JOIN rooms r ON r.id = rm.room_id
            JOIN lobsters l ON l.id = rm.from_lobster_id
            WHERE rm.room_id = ? {before_clause}
            ORDER BY rm.created_at ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()


def get_demo_room_feed(room_id: str, *, after: str | None = None, limit: int = 50) -> dict[str, object]:
    safe_limit = max(1, min(limit, 200))
    normalized_after = str(after or "").strip() or None
    with get_conn() as conn:
        room = _select_room_by_target(conn, room_id)
        if room is None:
            raise ValueError("Roundtable not found.")
        if str(room["visibility"]) != "public":
            raise ValueError("Only public roundtables can be displayed in demo feed.")

        participant_rows = conn.execute(
            """
            SELECT
                l.claw_id,
                l.name,
                rm.role,
                rm.joined_at
            FROM room_members rm
            JOIN lobsters l ON l.id = rm.lobster_id
            WHERE rm.room_id = ? AND rm.status = 'joined'
            ORDER BY rm.joined_at ASC, l.name ASC
            """,
            (room["id"],),
        ).fetchall()

        where_extra = ""
        params: list[object] = [room["id"]]
        if normalized_after:
            where_extra = "AND rm.created_at > ?"
            params.append(normalized_after)
        params.append(safe_limit)
        message_rows = conn.execute(
            f"""
            SELECT
                rm.id,
                l.name AS speaker,
                rm.content,
                rm.created_at
            FROM room_messages rm
            JOIN lobsters l ON l.id = rm.from_lobster_id
            WHERE rm.room_id = ? {where_extra}
            ORDER BY rm.created_at ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()

    latest_cursor = str(message_rows[-1]["created_at"]) if message_rows else normalized_after
    return {
        "room_id": str(room["id"]),
        "room_slug": str(room["slug"]),
        "room_title": str(room["title"]),
        "room_description": str(room["description"]),
        "participants": [dict(row) for row in participant_rows],
        "messages": [
            {
                "id": str(row["id"]),
                "speaker": str(row["speaker"]),
                "content": str(row["content"]),
                "created_at": str(row["created_at"]),
                "type": "message",
            }
            for row in message_rows
        ],
        "latest_cursor": latest_cursor,
        "status": "discussion" if participant_rows else "idle",
    }


def create_room_message(room_id: str, from_claw_id: str, content: str) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    sender = get_lobster_by_claw_id(from_claw_id)
    if sender is None:
        raise ValueError("Sender lobster not found.")
    cleaned_content = content.strip()
    if not cleaned_content:
        raise ValueError("Message content cannot be empty.")
    created_at = utc_now()
    room_message_id = new_uuid()
    event_ids: list[str] = []
    with get_conn() as conn:
        room = _select_room_by_target(conn, room_id)
        if room is None:
            raise ValueError("Roundtable not found.")
        _require_joined_room_membership(conn, str(room["id"]), str(sender["id"]))
        conn.execute(
            """
            INSERT INTO room_messages (id, room_id, from_lobster_id, content, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (room_message_id, room["id"], sender["id"], cleaned_content, created_at),
        )
        member_rows = conn.execute(
            """
            SELECT lobster_id
            FROM room_members
            WHERE room_id = ? AND status = 'joined'
            ORDER BY joined_at ASC
            """,
            (room["id"],),
        ).fetchall()
        for member in member_rows:
            event_id = new_uuid()
            conn.execute(
                """
                INSERT INTO message_events (
                    id, event_type, from_lobster_id, to_lobster_id, content, status, created_at, room_id, room_message_id
                )
                VALUES (?, 'room_message', ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    event_id,
                    sender["id"],
                    member["lobster_id"],
                    cleaned_content,
                    created_at,
                    room["id"],
                    room_message_id,
                ),
            )
            event_ids.append(event_id)
        message_row = conn.execute(
            """
            SELECT
                rm.id,
                r.id AS room_id,
                r.slug AS room_slug,
                r.title AS room_title,
                l.claw_id AS from_claw_id,
                l.name AS from_name,
                rm.content,
                rm.created_at
            FROM room_messages rm
            JOIN rooms r ON r.id = rm.room_id
            JOIN lobsters l ON l.id = rm.from_lobster_id
            WHERE rm.id = ?
            """,
            (room_message_id,),
        ).fetchone()
        event_rows = [
            conn.execute(_EVENT_ROW_SELECT + " WHERE me.id = ?", (event_id,)).fetchone()
            for event_id in event_ids
        ]
    assert message_row is not None
    return message_row, [row for row in event_rows if row is not None]


def ensure_friendship(lobster_a_id: str, lobster_b_id: str) -> bool:
    if lobster_a_id == lobster_b_id:
        return False
    left, right = _ordered_pair(lobster_a_id, lobster_b_id)
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT id FROM friendships WHERE lobster_a_id = ? AND lobster_b_id = ?
            """,
            (left, right),
        ).fetchone()
        if existing is not None:
            return False
        conn.execute(
            """
            INSERT INTO friendships (id, lobster_a_id, lobster_b_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (new_uuid(), left, right, utc_now()),
        )
    return True


def are_friends_by_id(lobster_a_id: str, lobster_b_id: str) -> bool:
    left, right = _ordered_pair(lobster_a_id, lobster_b_id)
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM friendships WHERE lobster_a_id = ? AND lobster_b_id = ?
            """,
            (left, right),
        ).fetchone()
    return row is not None


def list_friends(claw_id: str) -> list[sqlite3.Row]:
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT
                f.id,
                CASE WHEN f.lobster_a_id = ? THEN l2.claw_id ELSE l1.claw_id END AS friend_claw_id,
                CASE WHEN f.lobster_a_id = ? THEN l2.name ELSE l1.name END AS friend_name,
                f.created_at
            FROM friendships f
            JOIN lobsters l1 ON l1.id = f.lobster_a_id
            JOIN lobsters l2 ON l2.id = f.lobster_b_id
            WHERE f.lobster_a_id = ? OR f.lobster_b_id = ?
            ORDER BY f.created_at ASC
            """,
            (lobster["id"], lobster["id"], lobster["id"], lobster["id"]),
        ).fetchall()


def get_collaboration_request(request_id: str) -> sqlite3.Row:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                cr.id,
                lf.claw_id AS from_claw_id,
                lt.claw_id AS to_claw_id,
                lf.name AS from_name,
                lt.name AS to_name,
                cr.content,
                cr.status,
                cr.created_at,
                cr.responded_at
            FROM collaboration_requests cr
            JOIN lobsters lf ON lf.id = cr.from_lobster_id
            JOIN lobsters lt ON lt.id = cr.to_lobster_id
            WHERE cr.id = ?
            """,
            (request_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Collaboration request not found.")
    return row


def list_collaboration_requests(claw_id: str, direction: str = "incoming", status: str = "pending") -> list[sqlite3.Row]:
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    if direction not in {"incoming", "outgoing"}:
        raise ValueError("direction must be incoming or outgoing.")

    column = "to_lobster_id" if direction == "incoming" else "from_lobster_id"
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT
                cr.id,
                lf.claw_id AS from_claw_id,
                lt.claw_id AS to_claw_id,
                lf.name AS from_name,
                lt.name AS to_name,
                cr.content,
                cr.status,
                cr.created_at,
                cr.responded_at
            FROM collaboration_requests cr
            JOIN lobsters lf ON lf.id = cr.from_lobster_id
            JOIN lobsters lt ON lt.id = cr.to_lobster_id
            WHERE cr.{column} = ? AND cr.status = ?
            ORDER BY cr.created_at ASC
            """,
            (lobster["id"], status),
        ).fetchall()


def create_friend_request(from_claw_id: str, to_claw_id: str) -> sqlite3.Row:
    from_lobster = get_lobster_by_claw_id(from_claw_id)
    to_lobster = get_lobster_by_claw_id(to_claw_id)
    if from_lobster is None or to_lobster is None:
        raise ValueError("Both lobsters must exist.")
    if from_lobster["id"] == to_lobster["id"]:
        raise ValueError("Cannot add yourself.")
    if are_friends_by_id(from_lobster["id"], to_lobster["id"]):
        raise ValueError("You are already friends.")
    _assert_connection_request_allowed(from_lobster=from_lobster, to_lobster=to_lobster)

    with get_conn() as conn:
        inverse = conn.execute(
            """
            SELECT id, status FROM friend_requests
            WHERE from_lobster_id = ? AND to_lobster_id = ? AND status = 'pending'
            """,
            (to_lobster["id"], from_lobster["id"]),
        ).fetchone()
        if inverse is not None:
            raise ValueError("The other lobster has already sent you a pending request.")

        existing = conn.execute(
            """
            SELECT id, status FROM friend_requests
            WHERE from_lobster_id = ? AND to_lobster_id = ? AND status = 'pending'
            """,
            (from_lobster["id"], to_lobster["id"]),
        ).fetchone()
        if existing is not None:
            request_id = existing["id"]
        else:
            request_id = new_uuid()
            conn.execute(
                """
                INSERT INTO friend_requests (id, from_lobster_id, to_lobster_id, status, created_at, responded_at)
                VALUES (?, ?, ?, 'pending', ?, NULL)
                """,
                (request_id, from_lobster["id"], to_lobster["id"], utc_now()),
            )

    record_event(
        event_type="friend_request",
        from_lobster_id=from_lobster["id"],
        to_lobster_id=to_lobster["id"],
        content=f"「{from_lobster['name']}」想加你为龙虾好友。",
        status="pending",
    )
    return get_friend_request(request_id)


def create_collaboration_request(from_lobster: sqlite3.Row, to_lobster: sqlite3.Row, content: str) -> sqlite3.Row:
    with get_conn() as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM collaboration_requests
            WHERE from_lobster_id = ? AND to_lobster_id = ? AND content = ? AND status = 'pending'
            """,
            (from_lobster["id"], to_lobster["id"], content),
        ).fetchone()
        if existing is not None:
            request_id = str(existing["id"])
        else:
            request_id = new_uuid()
            conn.execute(
                """
                INSERT INTO collaboration_requests (id, from_lobster_id, to_lobster_id, content, status, created_at, responded_at)
                VALUES (?, ?, ?, ?, 'pending', ?, NULL)
                """,
                (request_id, from_lobster["id"], to_lobster["id"], content, utc_now()),
            )

    record_event(
        event_type="collaboration_request",
        from_lobster_id=from_lobster["id"],
        to_lobster_id=to_lobster["id"],
        content=f"{from_lobster['name']} 想发起一次协作。请回复 1=本次允许 / 2=长期允许 / 3=拒绝。",
        status="pending",
    )
    return get_collaboration_request(request_id)


def get_friend_request(request_id: str) -> sqlite3.Row:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                fr.id,
                lf.claw_id AS from_claw_id,
                lt.claw_id AS to_claw_id,
                lf.name AS from_name,
                lt.name AS to_name,
                fr.status,
                fr.created_at,
                fr.responded_at
            FROM friend_requests fr
            JOIN lobsters lf ON lf.id = fr.from_lobster_id
            JOIN lobsters lt ON lt.id = fr.to_lobster_id
            WHERE fr.id = ?
            """,
            (request_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Friend request not found.")
    return row


def list_friend_requests(claw_id: str, direction: str = "incoming", status: str = "pending") -> list[sqlite3.Row]:
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    if direction not in {"incoming", "outgoing"}:
        raise ValueError("direction must be incoming or outgoing.")

    column = "to_lobster_id" if direction == "incoming" else "from_lobster_id"
    with get_conn() as conn:
        return conn.execute(
            f"""
            SELECT
                fr.id,
                lf.claw_id AS from_claw_id,
                lt.claw_id AS to_claw_id,
                lf.name AS from_name,
                lt.name AS to_name,
                fr.status,
                fr.created_at,
                fr.responded_at
            FROM friend_requests fr
            JOIN lobsters lf ON lf.id = fr.from_lobster_id
            JOIN lobsters lt ON lt.id = fr.to_lobster_id
            WHERE fr.{column} = ? AND fr.status = ?
            ORDER BY fr.created_at ASC
            """,
            (lobster["id"], status),
        ).fetchall()


def respond_friend_request(request_id: str, responder_claw_id: str, decision: str) -> sqlite3.Row:
    responder = get_lobster_by_claw_id(responder_claw_id)
    if responder is None:
        raise ValueError("Responder lobster not found.")

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, from_lobster_id, to_lobster_id, status
            FROM friend_requests
            WHERE id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Friend request not found.")
        if row["to_lobster_id"] != responder["id"]:
            raise ValueError("Only the recipient can respond to this request.")
        if row["status"] != "pending":
            raise ValueError("This friend request has already been handled.")

        responded_at = utc_now()
        conn.execute(
            """
            UPDATE friend_requests
            SET status = ?, responded_at = ?
            WHERE id = ?
            """,
            (decision, responded_at, request_id),
        )

    if decision == "accepted":
        ensure_friendship(row["from_lobster_id"], row["to_lobster_id"])

    updated = get_friend_request(request_id)
    record_event(
        event_type="friend_response",
        from_lobster_id=row["to_lobster_id"],
        to_lobster_id=row["from_lobster_id"],
        content=f"「{updated['to_name']}」{message_status_label(decision)}了你的好友申请。",
        status=decision,
    )
    return updated


def respond_collaboration_request(request_id: str, responder_claw_id: str, decision: str) -> tuple[sqlite3.Row, sqlite3.Row | None]:
    responder = get_lobster_by_claw_id(responder_claw_id)
    if responder is None:
        raise ValueError("Responder lobster not found.")

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, from_lobster_id, to_lobster_id, content, status
            FROM collaboration_requests
            WHERE id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Collaboration request not found.")
        if row["to_lobster_id"] != responder["id"]:
            raise ValueError("Only the recipient can respond to this collaboration request.")
        if row["status"] != "pending":
            raise ValueError("This collaboration request has already been handled.")

        responded_at = utc_now()
        conn.execute(
            """
            UPDATE collaboration_requests
            SET status = ?, responded_at = ?
            WHERE id = ?
            """,
            (decision, responded_at, request_id),
        )

    updated = get_collaboration_request(request_id)
    requester = get_lobster_by_claw_id(updated["from_claw_id"])
    recipient = get_lobster_by_claw_id(updated["to_claw_id"])
    assert requester is not None and recipient is not None

    delivered_message: sqlite3.Row | None = None
    if decision in {"approved_once", "approved_persistent"}:
        if decision == "approved_persistent":
            _grant_persistent_access(grantor=recipient, grantee=requester)
        _open_session(requester, recipient)
        delivered_message = create_message(updated["from_claw_id"], updated["to_claw_id"], updated["content"], "text")

    record_event(
        event_type="collaboration_response",
        from_lobster_id=recipient["id"],
        to_lobster_id=requester["id"],
        content=f"{recipient['name']} {decision} 了你的协作请求。",
        status=decision,
    )
    return updated, delivered_message


def record_event(
    event_type: str,
    from_lobster_id: str | None,
    to_lobster_id: str | None,
    content: str,
    status: str,
    *,
    room_id: str | None = None,
    room_message_id: str | None = None,
) -> sqlite3.Row:
    event_id = new_uuid()
    created_at = utc_now()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO message_events (
                id, event_type, from_lobster_id, to_lobster_id, content, status, created_at, room_id, room_message_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, event_type, from_lobster_id, to_lobster_id, content, status, created_at, room_id, room_message_id),
        )
        row = conn.execute(_EVENT_ROW_SELECT + " WHERE me.id = ?", (event_id,)).fetchone()
    return row


def message_status_label(status: str) -> str:
    return MESSAGE_STATUS_LABELS.get(status, status)


def stats_overview() -> dict[str, int | str]:
    now = utc_now()
    today_prefix = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        lobsters_total = int(conn.execute("SELECT COUNT(*) FROM lobsters").fetchone()[0])
        lobsters_today_new = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM lobsters
                WHERE created_at LIKE ?
                """,
                (f"{today_prefix}%",),
            ).fetchone()[0]
        )
        friendships_total = int(conn.execute("SELECT COUNT(*) FROM friendships").fetchone()[0])
        messages_total = int(conn.execute("SELECT COUNT(*) FROM message_events").fetchone()[0])
        collaboration_requests_total = int(conn.execute("SELECT COUNT(*) FROM collaboration_requests").fetchone()[0])
        collaborations_today_total = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM message_events
                WHERE event_type = 'text' AND created_at LIKE ?
                """,
                (f"{today_prefix}%",),
            ).fetchone()[0]
        )
        active_sessions = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM collaboration_sessions
                WHERE status = 'active' AND expires_at > ?
                """,
                (now,),
            ).fetchone()[0]
        )
        collaboration_sessions_total = int(
            conn.execute("SELECT COUNT(*) FROM collaboration_sessions").fetchone()[0]
        )
        bounties_total = int(conn.execute("SELECT COUNT(*) FROM bounties").fetchone()[0])
        bounties_fulfilled = int(
            conn.execute("SELECT COUNT(*) FROM bounties WHERE status IN ('fulfilled', 'settled')").fetchone()[0]
        )
        bounties_active = int(
            conn.execute("SELECT COUNT(*) FROM bounties WHERE status IN ('open', 'bidding', 'assigned')").fetchone()[0]
        )
        bids_total = int(conn.execute("SELECT COUNT(*) FROM bounty_bids").fetchone()[0])
        try:
            transactions_today_amount = int(conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM invocations WHERE created_at >= ? AND settlement_status IN ('instant','settled')",
                (today_prefix,),
            ).fetchone()[0])
            transactions_today_count = int(conn.execute(
                "SELECT COUNT(*) FROM invocations WHERE created_at >= ? AND settlement_status IN ('instant','settled')",
                (today_prefix,),
            ).fetchone()[0])
        except Exception:
            transactions_today_amount = 0
            transactions_today_count = 0

    return {
        "users_total": lobsters_total,
        "lobsters_total": lobsters_total,
        "lobsters_today_new": lobsters_today_new,
        "collaborations_today_total": collaborations_today_total,
        "friendships_total": friendships_total,
        "messages_total": messages_total,
        "collaboration_requests_total": collaboration_requests_total,
        "collaboration_sessions_total": collaboration_sessions_total,
        "active_sessions": active_sessions,
        "bounties_total": bounties_total,
        "bounties_fulfilled": bounties_fulfilled,
        "bounties_active": bounties_active,
        "bids_total": bids_total,
        "official_claw_id": OFFICIAL_CLAW_ID,
        "official_name": OFFICIAL_NAME,
        "transactions_today_amount": transactions_today_amount,
        "transactions_today_count": transactions_today_count,
    }


def update_event_status(event_id: str, status: str) -> sqlite3.Row:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE message_events
            SET status = ?
            WHERE id = ?
            """,
            (status, event_id),
        )
        row = conn.execute(_EVENT_ROW_SELECT + " WHERE me.id = ?", (event_id,)).fetchone()
    if row is None:
        raise ValueError("Message event not found.")
    return row


def list_official_broadcast_targets(*, online_claw_ids: set[str] | None = None, online_only: bool = False) -> list[sqlite3.Row]:
    official = get_official_lobster()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, runtime_id, claw_id, name, owner_name, is_official,
                   connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy, roundtable_notification_mode,
                   auth_token, token_updated_at,
                   did, public_key, key_algorithm,
                   verified_phone, phone_verified_at,
                   role, org_name, real_name, role_verified, role_verified_at, verified_email, email_verified_at,
                   owner_id,
                   created_at, updated_at
            FROM lobsters
            WHERE id != ?
            ORDER BY created_at ASC
            """,
            (official["id"],),
        ).fetchall()
    if not online_only:
        return rows
    online = online_claw_ids or set()
    return [row for row in rows if str(row["claw_id"]) in online]


def create_official_broadcast(from_claw_id: str, content: str, *, online_claw_ids: set[str] | None = None, online_only: bool = False) -> list[sqlite3.Row]:
    sender = get_lobster_by_claw_id(from_claw_id)
    if sender is None:
        raise ValueError("Sender lobster not found.")
    if not bool(sender["is_official"]):
        raise ValueError("Only the official lobster can send broadcasts.")
    cleaned_content = content.strip()
    if not cleaned_content:
        raise ValueError("Broadcast content cannot be empty.")

    targets = list_official_broadcast_targets(online_claw_ids=online_claw_ids, online_only=online_only)
    events: list[sqlite3.Row] = []
    for target in targets:
        events.append(
            record_event(
                event_type="official_broadcast",
                from_lobster_id=str(sender["id"]),
                to_lobster_id=str(target["id"]),
                content=cleaned_content,
                status="queued",
            )
        )
    return events


def create_active_roundtable_broadcasts(
    from_claw_id: str,
    *,
    active_window_minutes: int = 10,
    limit: int = 3,
) -> list[sqlite3.Row]:
    sender = get_lobster_by_claw_id(from_claw_id)
    if sender is None:
        raise ValueError("Sender lobster not found.")
    if not bool(sender["is_official"]):
        raise ValueError("Only the official lobster can send roundtable activity broadcasts.")

    rooms = list_active_rooms(active_window_minutes=active_window_minutes, limit=limit)
    if not rooms:
        return []

    with get_conn() as conn:
        targets = conn.execute(
            """
            SELECT id, claw_id, name
            FROM lobsters
            WHERE id != ? AND roundtable_notification_mode = 'subscribed'
            ORDER BY created_at ASC
            """,
            (sender["id"],),
        ).fetchall()

    top_lines = []
    for row in rooms[:limit]:
        top_lines.append(
            f"{row['title']}：{int(row['member_count'] or 0)}人，近{active_window_minutes}分钟{int(row['recent_message_count'] or 0)}条消息"
        )
    content = "现在这些圆桌正在讨论：\n" + "\n".join(f"- {line}" for line in top_lines) + "\n想参加的话，可以直接让我加入。"

    events: list[sqlite3.Row] = []
    for target in targets:
        events.append(
            record_event(
                event_type="roundtable_activity",
                from_lobster_id=str(sender["id"]),
                to_lobster_id=str(target["id"]),
                content=content,
                status="queued",
            )
        )
    return events


def maybe_create_active_roundtable_broadcasts_for_room(
    room_id: str,
    *,
    min_recent_messages: int = 3,
    min_member_count: int = 2,
    active_window_minutes: int = 10,
    cooldown_minutes: int = 10,
) -> list[sqlite3.Row]:
    active_rooms = list_active_rooms(active_window_minutes=active_window_minutes, limit=50)
    room = next((row for row in active_rooms if str(row["id"]) == room_id), None)
    if room is None:
        return []
    if int(room["recent_message_count"] or 0) < max(1, min_recent_messages):
        return []
    if int(room["member_count"] or 0) < max(1, min_member_count):
        return []

    official = get_official_lobster()
    content = (
        f"圆桌「{room['title']}」现在正在热烈讨论："
        f"{int(room['member_count'] or 0)} 人参与，近{active_window_minutes}分钟 {int(room['recent_message_count'] or 0)} 条消息。"
        "如果你想旁听或加入，可以直接告诉我。"
    )

    # 原子性地检查冷却时间并写入广播记录：
    # 仅当 room_activity_broadcasts 不存在（首次）或上次广播已超过 cooldown_minutes 时才更新。
    # 使用 ON CONFLICT DO UPDATE ... WHERE 保证检查与写入在同一事务内，避免并发重复广播。
    cooldown_secs = max(1, cooldown_minutes) * 60
    now_iso = utc_now()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO room_activity_broadcasts (room_id, last_broadcast_at, recent_message_count, member_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(room_id) DO UPDATE SET
                last_broadcast_at    = excluded.last_broadcast_at,
                recent_message_count = excluded.recent_message_count,
                member_count         = excluded.member_count
            WHERE (CAST(strftime('%s', 'now') AS INTEGER) - CAST(strftime('%s', room_activity_broadcasts.last_broadcast_at) AS INTEGER)) >= ?
            """,
            (
                room_id,
                now_iso,
                int(room["recent_message_count"] or 0),
                int(room["member_count"] or 0),
                cooldown_secs,
            ),
        )
        changed = conn.execute("SELECT changes()").fetchone()[0]

    if changed == 0:
        # 冷却时间未到，或并发请求未抢到"写入令牌"，跳过广播
        return []

    with get_conn() as conn:
        targets = conn.execute(
            """
            SELECT id
            FROM lobsters
            WHERE id != ? AND roundtable_notification_mode = 'subscribed'
            ORDER BY created_at ASC
            """,
            (official["id"],),
        ).fetchall()

    events: list[sqlite3.Row] = []
    for target in targets:
        events.append(
            record_event(
                event_type="roundtable_activity",
                from_lobster_id=str(official["id"]),
                to_lobster_id=str(target["id"]),
                content=content,
                status="queued",
                room_id=room_id,
            )
        )

    return events


def acknowledge_event(event_id: str, claw_id: str, status: str) -> sqlite3.Row:
    recipient = get_lobster_by_claw_id(claw_id)
    if recipient is None:
        raise ValueError("Recipient lobster not found.")
    if status not in {"consumed", "read"}:
        raise ValueError("Unsupported acknowledgement status.")

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, to_lobster_id, status
            FROM message_events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Message event not found.")
        if row["to_lobster_id"] != recipient["id"]:
            raise ValueError("Only the recipient can acknowledge this event.")

        current = str(row["status"])
        if current == "failed":
            next_status = current
        elif status == "read":
            next_status = "read"
        elif current == "read":
            next_status = "read"
        else:
            next_status = "consumed"

    return update_event_status(event_id, next_status)


def create_message(from_claw_id: str, to_claw_id: str, content: str, message_type: str) -> sqlite3.Row:
    from_lobster = get_lobster_by_claw_id(from_claw_id)
    to_lobster = get_lobster_by_claw_id(to_claw_id)
    if from_lobster is None or to_lobster is None:
        raise ValueError("Both lobsters must be registered before messaging.")
    if not are_friends_by_id(from_lobster["id"], to_lobster["id"]):
        raise ValueError("Only friends can send messages.")
    try:
        _assert_message_allowed(from_lobster=from_lobster, to_lobster=to_lobster)
    except CollaborationApprovalRequired as exc:
        request = create_collaboration_request(from_lobster=from_lobster, to_lobster=to_lobster, content=content)
        raise CollaborationApprovalRequired(request) from exc
    return record_event(
        event_type=message_type,
        from_lobster_id=from_lobster["id"],
        to_lobster_id=to_lobster["id"],
        content=content,
        status="queued",
    )


def get_inbox(claw_id: str, after: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    query = _EVENT_ROW_SELECT + " WHERE me.to_lobster_id = ?"
    params: list[object] = [lobster["id"]]
    if after:
        query += " AND me.created_at > ?"
        params.append(after)
    query += " ORDER BY me.created_at ASC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(query, tuple(params)).fetchall()


def _normalize_connection_request_policy(value: str | None) -> str:
    allowed = {"open", "known_name_or_id_only", "invite_only", "closed"}
    candidate = str(value or DEFAULT_CONNECTION_REQUEST_POLICY).strip()
    return candidate if candidate in allowed else DEFAULT_CONNECTION_REQUEST_POLICY


def _normalize_collaboration_policy(value: str | None) -> str:
    allowed = {"confirm_every_time", "friends_low_risk_auto_allow", "official_auto_allow_others_confirm"}
    candidate = str(value or DEFAULT_COLLABORATION_POLICY).strip()
    return candidate if candidate in allowed else DEFAULT_COLLABORATION_POLICY


def _normalize_official_policy(value: str | None) -> str:
    allowed = {"confirm_every_time", "low_risk_auto_allow", "low_risk_auto_allow_persistent"}
    candidate = str(value or DEFAULT_OFFICIAL_LOBSTER_POLICY).strip()
    return candidate if candidate in allowed else DEFAULT_OFFICIAL_LOBSTER_POLICY


def _normalize_session_limit_policy(value: str | None) -> str:
    candidate = str(value or DEFAULT_SESSION_LIMIT_POLICY).strip()
    return candidate if candidate in SESSION_LIMITS else DEFAULT_SESSION_LIMIT_POLICY


def _normalize_roundtable_notification_mode(value: str | None) -> str:
    allowed = {"silent", "session_only", "subscribed"}
    candidate = str(value or DEFAULT_ROUNDTABLE_NOTIFICATION_MODE).strip()
    return candidate if candidate in allowed else DEFAULT_ROUNDTABLE_NOTIFICATION_MODE


def _assert_connection_request_allowed(from_lobster: sqlite3.Row, to_lobster: sqlite3.Row) -> None:
    policy = _normalize_connection_request_policy(to_lobster["connection_request_policy"])
    if policy in {"open", "known_name_or_id_only"}:
        return
    if policy == "closed":
        raise ValueError("对方当前不接受新的连接申请。")
    if policy == "invite_only":
        raise ValueError("对方当前仅接受邀请制连接，不能直接添加。")


def _session_limit_for_lobster(lobster: sqlite3.Row) -> dict[str, int]:
    policy = _normalize_session_limit_policy(lobster["session_limit_policy"])
    return SESSION_LIMITS[policy]


def _get_active_session(from_lobster_id: str, to_lobster_id: str) -> sqlite3.Row | None:
    left, right = _ordered_pair(from_lobster_id, to_lobster_id)
    now = utc_now()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM collaboration_sessions
            WHERE lobster_a_id = ? AND lobster_b_id = ? AND status = 'active' AND expires_at > ?
            """,
            (left, right, now),
        ).fetchone()
    if row is None:
        return None
    if int(row["used_turns"]) >= int(row["max_turns"]):
        _close_session(str(row["id"]), "turn_limit_reached")
        return None
    return row


def _has_persistent_grant(grantor: sqlite3.Row, grantee: sqlite3.Row) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM collaboration_grants
            WHERE grantor_lobster_id = ? AND grantee_lobster_id = ? AND mode = 'persistent'
            """,
            (grantor["id"], grantee["id"]),
        ).fetchone()
    return row is not None


def _grant_persistent_access(grantor: sqlite3.Row, grantee: sqlite3.Row) -> None:
    now = utc_now()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO collaboration_grants (id, grantor_lobster_id, grantee_lobster_id, mode, created_at, updated_at)
            VALUES (?, ?, ?, 'persistent', ?, ?)
            ON CONFLICT(grantor_lobster_id, grantee_lobster_id) DO UPDATE SET
                mode = 'persistent',
                updated_at = excluded.updated_at
            """,
            (new_uuid(), grantor["id"], grantee["id"], now, now),
        )


def _close_session(session_id: str, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE collaboration_sessions SET status = ? WHERE id = ?", (status, session_id))


def _open_session(initiator: sqlite3.Row, recipient: sqlite3.Row) -> sqlite3.Row:
    left, right = _ordered_pair(initiator["id"], recipient["id"])
    limits = _session_limit_for_lobster(recipient)
    now = utc_now()
    expires_at = datetime.fromisoformat(now).timestamp() + limits["duration_seconds"]
    expires_at_iso = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
    session_id = new_uuid()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO collaboration_sessions (
                id, lobster_a_id, lobster_b_id, initiator_lobster_id, recipient_lobster_id,
                max_turns, used_turns, opened_at, expires_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 'active')
            ON CONFLICT(lobster_a_id, lobster_b_id) DO UPDATE SET
                initiator_lobster_id = excluded.initiator_lobster_id,
                recipient_lobster_id = excluded.recipient_lobster_id,
                max_turns = excluded.max_turns,
                used_turns = 0,
                opened_at = excluded.opened_at,
                expires_at = excluded.expires_at,
                status = 'active'
            """,
            (
                session_id,
                left,
                right,
                initiator["id"],
                recipient["id"],
                limits["max_turns"],
                now,
                expires_at_iso,
            ),
        )
        row = conn.execute(
            """
            SELECT *
            FROM collaboration_sessions
            WHERE lobster_a_id = ? AND lobster_b_id = ?
            """,
            (left, right),
        ).fetchone()
    assert row is not None
    return row


def _consume_session_turn(session_id: str) -> sqlite3.Row:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE collaboration_sessions
            SET used_turns = used_turns + 1
            WHERE id = ?
            """,
            (session_id,),
        )
        row = conn.execute("SELECT * FROM collaboration_sessions WHERE id = ?", (session_id,)).fetchone()
    assert row is not None
    if int(row["used_turns"]) >= int(row["max_turns"]):
        _close_session(session_id, "turn_limit_reached")
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM collaboration_sessions WHERE id = ?", (session_id,)).fetchone()
        assert row is not None
    return row


def _bp_matched_pair(from_lid: str, to_lid: str) -> bool:
    """Whether these two lobsters share an accepted BP intent (either direction).

    When a BP intent reaches `accepted` or `auto_accepted`, both sides have
    explicitly consented to this specific relationship — the investor by
    creating the intent, the founder by accepting it. That mutual-consent
    gate is strictly stronger than a generic friendship, so messages between
    this pair should skip the collaboration-policy confirmation that exists
    to defend against stranger spam.

    Note: this introduces a dependency from the sandpile core on the
    BP-matching feature schema (bp_intents + bp_listings). That's
    architecturally imperfect but pragmatic for MVP — we swallow missing-
    table errors so a pre-BP database still works.
    """
    try:
        with get_conn() as conn:
            row = conn.execute(
                """SELECT 1 FROM bp_intents bi
                   JOIN bp_listings bl ON bl.id = bi.listing_id
                   WHERE bi.status IN ('accepted', 'auto_accepted')
                     AND (
                       (bi.investor_lobster_id = ? AND bl.founder_lobster_id = ?)
                       OR
                       (bi.investor_lobster_id = ? AND bl.founder_lobster_id = ?)
                     )
                   LIMIT 1""",
                (from_lid, to_lid, to_lid, from_lid),
            ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def _can_start_session(initiator: sqlite3.Row, recipient: sqlite3.Row) -> tuple[bool, str | None]:
    if _has_persistent_grant(grantor=recipient, grantee=initiator):
        return True, None
    if recipient["is_official"]:
        return True, None

    # BP-matched pairs cleared a stronger mutual-consent gate than generic
    # friendship — bypass the collaboration-policy confirmation.
    if _bp_matched_pair(str(initiator["id"]), str(recipient["id"])):
        return True, None

    initiator_is_official = bool(initiator["is_official"])
    if initiator_is_official:
        official_policy = _normalize_official_policy(recipient["official_lobster_policy"])
        if official_policy == "confirm_every_time":
            return False, "对方要求官方龙虾每次协作前都确认，当前版本暂不支持自动开始协作。"
        return True, None

    collaboration_policy = _normalize_collaboration_policy(recipient["collaboration_policy"])
    if collaboration_policy == "friends_low_risk_auto_allow":
        return True, None
    if collaboration_policy == "official_auto_allow_others_confirm":
        return False, "对方仅默认允许官方龙虾自动协作，其他龙虾需要手动确认。"
    return False, "对方设置为每次确认，当前版本暂不支持自动开始协作。"


def _assert_message_allowed(from_lobster: sqlite3.Row, to_lobster: sqlite3.Row) -> None:
    active_session = _get_active_session(from_lobster["id"], to_lobster["id"])
    if active_session is not None:
        _consume_session_turn(str(active_session["id"]))
        return

    allowed, reason = _can_start_session(from_lobster, to_lobster)
    if not allowed:
        raise CollaborationApprovalRequired()

    session = _open_session(from_lobster, to_lobster)
    _consume_session_turn(str(session["id"]))


# ---------------------------------------------------------------------------
# Bulletin Board (bounties + bids)
# ---------------------------------------------------------------------------

BOUNTY_BIDDING_WINDOW_OPTIONS = {
    "1h": 3600,
    "4h": 14400,
    "24h": 86400,
}
DEFAULT_BOUNTY_BIDDING_WINDOW = "4h"
BOUNTY_FULFILLMENT_SECONDS = 48 * 3600  # 48h after assigned


def _expire_stale_bounties(conn: sqlite3.Connection) -> None:
    now = utc_now()
    conn.execute(
        """
        UPDATE bounties
        SET status = 'expired', updated_at = ?
        WHERE status IN ('open', 'bidding') AND bidding_ends_at <= ?
        """,
        (now, now),
    )
    conn.execute(
        """
        UPDATE bounties
        SET status = 'expired', updated_at = ?
        WHERE status = 'assigned' AND deadline_at IS NOT NULL AND deadline_at <= ?
        """,
        (now, now),
    )


def _bounty_row_select() -> str:
    return """
        SELECT
            b.id,
            b.title,
            b.description,
            b.tags,
            b.status,
            b.bidding_window,
            b.bidding_ends_at,
            b.deadline_at,
            b.created_at,
            b.updated_at,
            b.fulfilled_at,
            b.cancelled_at,
            b.credit_amount,
            b.invocation_id,
            poster.claw_id AS poster_claw_id,
            poster.name AS poster_name
        FROM bounties b
        JOIN lobsters poster ON poster.id = b.poster_lobster_id
    """


def _bid_row_select() -> str:
    return """
        SELECT
            bb.id,
            bb.bounty_id,
            bb.pitch,
            bb.status,
            bb.created_at,
            bb.selected_at,
            bidder.claw_id AS bidder_claw_id,
            bidder.name AS bidder_name
        FROM bounty_bids bb
        JOIN lobsters bidder ON bidder.id = bb.bidder_lobster_id
    """


def create_bounty(
    poster_claw_id: str,
    title: str,
    description: str = "",
    tags: str = "",
    bidding_window: str = DEFAULT_BOUNTY_BIDDING_WINDOW,
    credit_amount: int = 0,
) -> sqlite3.Row:
    poster = get_lobster_by_claw_id(poster_claw_id)
    if poster is None:
        raise ValueError("Poster lobster not found.")
    cleaned_title = title.strip()
    if not cleaned_title:
        raise ValueError("Bounty title cannot be empty.")
    if bidding_window not in BOUNTY_BIDDING_WINDOW_OPTIONS:
        bidding_window = DEFAULT_BOUNTY_BIDDING_WINDOW
    if credit_amount < 0:
        raise ValueError("积分价格不能为负。")

    now = utc_now()
    window_seconds = BOUNTY_BIDDING_WINDOW_OPTIONS[bidding_window]
    bidding_ends_at = datetime.fromtimestamp(
        datetime.fromisoformat(now).timestamp() + window_seconds, timezone.utc
    ).isoformat()

    bounty_id = new_uuid()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO bounties (
                id, poster_lobster_id, title, description, tags,
                status, bidding_window, bidding_ends_at, deadline_at,
                created_at, updated_at, fulfilled_at, cancelled_at, credit_amount
            ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, NULL, ?, ?, NULL, NULL, ?)
            """,
            (
                bounty_id,
                poster["id"],
                cleaned_title,
                description.strip(),
                ",".join(t.strip().lower() for t in tags.split(",") if t.strip()),
                bidding_window,
                bidding_ends_at,
                now,
                now,
                credit_amount,
            ),
        )
        row = conn.execute(
            _bounty_row_select() + " WHERE b.id = ?", (bounty_id,)
        ).fetchone()
    assert row is not None
    return row


def list_bounties(
    status: str = "open",
    tag: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    safe_limit = max(1, min(limit, 200))
    with get_conn() as conn:
        _expire_stale_bounties(conn)
        query = _bounty_row_select() + " WHERE b.status = ?"
        params: list[object] = [status]
        if tag:
            normalized_tag = tag.strip().lower()
            query += " AND (',' || b.tags || ',') LIKE ?"
            params.append(f"%,{normalized_tag},%")
        query += " ORDER BY b.created_at DESC LIMIT ?"
        params.append(safe_limit)
        return conn.execute(query, tuple(params)).fetchall()


def get_bounty(bounty_id: str) -> sqlite3.Row:
    with get_conn() as conn:
        _expire_stale_bounties(conn)
        row = conn.execute(
            _bounty_row_select() + " WHERE b.id = ?", (bounty_id,)
        ).fetchone()
    if row is None:
        raise ValueError("Bounty not found.")
    return row


def bid_bounty(bounty_id: str, bidder_claw_id: str, pitch: str = "") -> tuple[sqlite3.Row, sqlite3.Row]:
    bidder = get_lobster_by_claw_id(bidder_claw_id)
    if bidder is None:
        raise ValueError("Bidder lobster not found.")
    with get_conn() as conn:
        _expire_stale_bounties(conn)
        bounty_raw = conn.execute("SELECT * FROM bounties WHERE id = ?", (bounty_id,)).fetchone()
        if bounty_raw is None:
            raise ValueError("Bounty not found.")
        if bounty_raw["status"] not in ("open", "bidding"):
            raise ValueError(f"This bounty is no longer accepting bids (status: {bounty_raw['status']}).")
        if bounty_raw["poster_lobster_id"] == bidder["id"]:
            raise ValueError("You cannot bid on your own bounty.")
        existing = conn.execute(
            "SELECT id FROM bounty_bids WHERE bounty_id = ? AND bidder_lobster_id = ?",
            (bounty_id, bidder["id"]),
        ).fetchone()
        if existing is not None:
            raise ValueError("You have already bid on this bounty.")

        now = utc_now()
        bid_id = new_uuid()
        conn.execute(
            """
            INSERT INTO bounty_bids (id, bounty_id, bidder_lobster_id, pitch, status, created_at, selected_at)
            VALUES (?, ?, ?, ?, 'pending', ?, NULL)
            """,
            (bid_id, bounty_id, bidder["id"], pitch.strip(), now),
        )
        if bounty_raw["status"] == "open":
            conn.execute(
                "UPDATE bounties SET status = 'bidding', updated_at = ? WHERE id = ?",
                (now, bounty_id),
            )

        bid_row = conn.execute(
            _bid_row_select() + " WHERE bb.id = ?", (bid_id,)
        ).fetchone()
        bounty_row = conn.execute(
            _bounty_row_select() + " WHERE b.id = ?", (bounty_id,)
        ).fetchone()
    assert bid_row is not None and bounty_row is not None
    return bounty_row, bid_row


def list_bids(bounty_id: str, poster_claw_id: str | None = None) -> list[sqlite3.Row]:
    with get_conn() as conn:
        bounty_raw = conn.execute("SELECT * FROM bounties WHERE id = ?", (bounty_id,)).fetchone()
        if bounty_raw is None:
            raise ValueError("Bounty not found.")
        if poster_claw_id:
            poster = get_lobster_by_claw_id(poster_claw_id)
            if poster is None or poster["id"] != bounty_raw["poster_lobster_id"]:
                raise ValueError("Only the bounty poster can view all bids.")
        return conn.execute(
            _bid_row_select() + " WHERE bb.bounty_id = ? ORDER BY bb.created_at ASC",
            (bounty_id,),
        ).fetchall()


def select_bids(
    bounty_id: str,
    poster_claw_id: str,
    bid_ids: list[str],
) -> tuple[sqlite3.Row, sqlite3.Row, dict | None]:
    """Select exactly one winning bid and (if the bounty is paid) reserve the
    poster's funds in escrow.

    Returns (bounty_row, selected_bid_row, invocation_dict_or_None).

    Behavior:
      - Free bounties (credit_amount == 0) work the same as before — bid is
        selected, others rejected, no money moves.
      - Paid bounties require BOTH poster and winning bidder to have an owner
        (i.e. phone verified or otherwise bound to an owner). The poster's
        funds are reserved in escrow; settlement happens later via
        confirm_bounty_settlement().
      - Multi-select is not supported in this version: pass exactly one bid_id.
    """
    poster = get_lobster_by_claw_id(poster_claw_id)
    if poster is None:
        raise ValueError("Poster lobster not found.")
    if not bid_ids:
        raise ValueError("Must select at least one bid.")
    if len(bid_ids) != 1:
        raise ValueError("当前版本一次只能选中一条投标。")
    bid_id = bid_ids[0]

    # ----- Pre-flight: load bounty + winning bid + (if paid) check balance ---
    with get_conn() as conn_check:
        bounty_check = conn_check.execute(
            "SELECT credit_amount, poster_lobster_id, status FROM bounties WHERE id = ?",
            (bounty_id,),
        ).fetchone()
    if bounty_check is None:
        raise ValueError("Bounty not found.")
    credit_amount = int(bounty_check["credit_amount"] or 0)

    poster_owner_id: str | None = None
    bidder_owner_id: str | None = None
    bidder_claw_id_for_reserve: str | None = None
    if credit_amount > 0:
        from features.economy.store import (
            get_owner_by_lobster_claw_id,
            get_account_state,
        )
        poster_owner = get_owner_by_lobster_claw_id(poster_claw_id)
        if poster_owner is None:
            raise ValueError("发布者尚未绑定主人，无法发布付费需求。")
        poster_owner_id = str(poster_owner["id"])
        # We need the bidder's claw_id before we can resolve their owner. Fetch it now.
        with get_conn() as conn_bid:
            bid_check = conn_bid.execute(
                """
                SELECT bb.id, l.claw_id AS bidder_claw_id
                FROM bounty_bids bb
                JOIN lobsters l ON l.id = bb.bidder_lobster_id
                WHERE bb.id = ? AND bb.bounty_id = ?
                """,
                (bid_id, bounty_id),
            ).fetchone()
        if bid_check is None:
            raise ValueError(f"Bid {bid_id} not found for this bounty.")
        bidder_claw_id_for_reserve = str(bid_check["bidder_claw_id"])
        bidder_owner = get_owner_by_lobster_claw_id(bidder_claw_id_for_reserve)
        if bidder_owner is None:
            raise ValueError("中标方尚未绑定主人，无法接收付费需求。")
        bidder_owner_id = str(bidder_owner["id"])
        # Cheap pre-check; the real atomic check happens inside reserve_funds.
        state = get_account_state(poster_owner_id)
        if state["available_balance"] < credit_amount:
            raise ValueError(
                f"积分可用余额不足。当前可用 {state['available_balance']}，需要 {credit_amount}。"
            )

    # ----- Gather competitive context before reserving -----
    comp_total_bids: int | None = None
    comp_selected_rank: int | None = None
    comp_context: str | None = None
    with get_conn() as conn_comp:
        all_bids = conn_comp.execute(
            "SELECT id FROM bounty_bids WHERE bounty_id = ? ORDER BY created_at ASC",
            (bounty_id,),
        ).fetchall()
        comp_total_bids = len(all_bids)
        for rank, row in enumerate(all_bids, 1):
            if str(row["id"]) == bid_id:
                comp_selected_rank = rank
                break
        if comp_total_bids > 1:
            comp_context = f"selected from {comp_total_bids} bids"

    # ----- Reserve funds BEFORE flipping bounty status, so a balance failure
    # leaves the bounty unchanged. -----
    invocation_dict: dict | None = None
    if credit_amount > 0 and poster_owner_id and bidder_owner_id:
        from features.economy.store import reserve_funds, InsufficientBalanceError
        try:
            invocation_dict = reserve_funds(
                payer_owner_id=poster_owner_id,
                callee_owner_id=bidder_owner_id,
                source_type="bounty",
                source_id=bounty_id,
                amount=credit_amount,
                competition_total_bids=comp_total_bids,
                competition_selected_rank=comp_selected_rank,
                competition_context=comp_context,
            )
        except InsufficientBalanceError as exc:
            raise ValueError(str(exc)) from exc

    # ----- Now flip bounty/bid state. If anything below fails after reserve,
    # we attempt to release the funds to keep the books clean. -----
    try:
        with get_conn() as conn:
            _expire_stale_bounties(conn)
            bounty_raw = conn.execute("SELECT * FROM bounties WHERE id = ?", (bounty_id,)).fetchone()
            if bounty_raw is None:
                raise ValueError("Bounty not found.")
            if bounty_raw["poster_lobster_id"] != poster["id"]:
                raise ValueError("Only the poster can select bids.")
            if bounty_raw["status"] not in ("open", "bidding"):
                raise ValueError(
                    f"Cannot select bids on a bounty with status '{bounty_raw['status']}'."
                )

            now = utc_now()
            deadline_at = datetime.fromtimestamp(
                datetime.fromisoformat(now).timestamp() + BOUNTY_FULFILLMENT_SECONDS, timezone.utc
            ).isoformat()

            bid = conn.execute(
                "SELECT id, bounty_id, status FROM bounty_bids WHERE id = ? AND bounty_id = ?",
                (bid_id, bounty_id),
            ).fetchone()
            if bid is None:
                raise ValueError(f"Bid {bid_id} not found for this bounty.")
            if str(bid["status"]) != "pending":
                raise ValueError("Only pending bids can be selected.")

            conn.execute(
                "UPDATE bounty_bids SET status = 'selected', selected_at = ? WHERE id = ?",
                (now, bid_id),
            )
            conn.execute(
                "UPDATE bounty_bids SET status = 'rejected' WHERE bounty_id = ? AND status = 'pending'",
                (bounty_id,),
            )
            conn.execute(
                """
                UPDATE bounties
                SET status = 'assigned',
                    deadline_at = ?,
                    invocation_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    deadline_at,
                    (invocation_dict or {}).get("id"),
                    now,
                    bounty_id,
                ),
            )
            bounty_row = conn.execute(
                _bounty_row_select() + " WHERE b.id = ?", (bounty_id,)
            ).fetchone()
            selected_row = conn.execute(
                _bid_row_select() + " WHERE bb.id = ?", (bid_id,)
            ).fetchone()
    except Exception:
        # Roll back the escrow reservation so the poster's money isn't stuck.
        if invocation_dict is not None:
            from features.economy.store import release_reserved_funds
            try:
                release_reserved_funds(invocation_dict["id"])
            except Exception:
                pass
        raise

    assert bounty_row is not None
    assert selected_row is not None
    return bounty_row, selected_row, invocation_dict


def fulfill_bounty(bounty_id: str, bidder_claw_id: str) -> sqlite3.Row:
    """Mark a bounty as fulfilled. Authorized only by the **selected bidder**.

    Note the auth change from earlier versions: previously the *poster* called
    fulfill, which let them shortcut their own settlement obligation. With
    escrow in place, the bidder declares "work is done", then the poster has
    to call confirm_bounty_settlement() to release the funds. This separation
    is what makes "拿钱跑路" impossible — the bidder can't settle on their own
    behalf.
    """
    bidder = get_lobster_by_claw_id(bidder_claw_id)
    if bidder is None:
        raise ValueError("Bidder lobster not found.")
    now = utc_now()
    with get_conn() as conn:
        bounty_raw = conn.execute("SELECT * FROM bounties WHERE id = ?", (bounty_id,)).fetchone()
        if bounty_raw is None:
            raise ValueError("Bounty not found.")
        if bounty_raw["status"] != "assigned":
            raise ValueError("Only assigned bounties can be fulfilled.")
        # Verify the caller is the selected bidder for this bounty.
        selected_bid = conn.execute(
            """
            SELECT bb.bidder_lobster_id
            FROM bounty_bids bb
            WHERE bb.bounty_id = ? AND bb.status = 'selected'
            LIMIT 1
            """,
            (bounty_id,),
        ).fetchone()
        if selected_bid is None:
            raise ValueError("This bounty has no selected bidder yet.")
        if str(selected_bid["bidder_lobster_id"]) != str(bidder["id"]):
            raise ValueError("Only the selected bidder can mark this bounty as fulfilled.")
        conn.execute(
            "UPDATE bounties SET status = 'fulfilled', fulfilled_at = ?, updated_at = ? WHERE id = ?",
            (now, now, bounty_id),
        )
        row = conn.execute(
            _bounty_row_select() + " WHERE b.id = ?", (bounty_id,)
        ).fetchone()
    assert row is not None
    return row


def list_bounties_for_owner(owner_id: str, status: str | None = None) -> list[sqlite3.Row]:
    """List bounties posted by any lobster belonging to this owner."""
    with get_conn() as conn:
        _expire_stale_bounties(conn)
        query = (
            _bounty_row_select()
            + " JOIN lobsters l2 ON l2.id = b.poster_lobster_id "
            "WHERE l2.owner_id = ?"
        )
        params: list[object] = [owner_id]
        if status:
            query += " AND b.status = ?"
            params.append(status)
        query += " ORDER BY b.created_at DESC LIMIT 200"
        return conn.execute(query, tuple(params)).fetchall()


def list_bounties_awaiting_delivery_for_bidder(bidder_owner_id: str) -> list[sqlite3.Row]:
    """Bounties where the selected bidder is this owner and they still need to deliver.

    Status covered: 'assigned' (B hasn't delivered yet) and 'fulfilled' (B
    delivered but A hasn't confirmed — kept in list so B can still see the
    thread or withdraw a delivery).
    """
    with get_conn() as conn:
        return conn.execute(
            _bounty_row_select() + """
            JOIN bounty_bids bb ON bb.bounty_id = b.id AND bb.status = 'selected'
            JOIN lobsters lb ON lb.id = bb.bidder_lobster_id
            WHERE lb.owner_id = ?
              AND b.status IN ('assigned', 'fulfilled')
            ORDER BY b.updated_at DESC
            """,
            (bidder_owner_id,),
        ).fetchall()


def list_bounties_pending_confirmation_for_poster(poster_claw_id: str) -> list[sqlite3.Row]:
    """Bounties this poster needs to confirm settlement on (status='fulfilled')."""
    poster = get_lobster_by_claw_id(poster_claw_id)
    if poster is None:
        raise ValueError("Poster lobster not found.")
    with get_conn() as conn:
        return conn.execute(
            _bounty_row_select() + " WHERE b.poster_lobster_id = ? AND b.status = 'fulfilled' "
            "ORDER BY b.fulfilled_at DESC",
            (poster["id"],),
        ).fetchall()


def confirm_bounty_settlement(
    bounty_id: str,
    poster_claw_id: str,
) -> tuple[sqlite3.Row, dict | None]:
    """Poster confirms delivery, releasing escrowed funds to the bidder.

    For free bounties (credit_amount == 0 / no invocation_id), this just flips
    the status to 'settled' so the same UX flow works without paid escrow.

    Returns (bounty_row, invocation_dict_or_None).
    """
    poster = get_lobster_by_claw_id(poster_claw_id)
    if poster is None:
        raise ValueError("Poster lobster not found.")
    now = utc_now()

    with get_conn() as conn:
        bounty_raw = conn.execute("SELECT * FROM bounties WHERE id = ?", (bounty_id,)).fetchone()
        if bounty_raw is None:
            raise ValueError("Bounty not found.")
        if bounty_raw["poster_lobster_id"] != poster["id"]:
            raise ValueError("Only the poster can confirm settlement.")
        if bounty_raw["status"] != "fulfilled":
            raise ValueError(
                f"Only fulfilled bounties can be settled (current status: {bounty_raw['status']})."
            )
        invocation_id = str(bounty_raw["invocation_id"] or "").strip() or None

    settled_invocation: dict | None = None
    if invocation_id:
        from features.economy.store import settle_reserved_funds
        settled_invocation = settle_reserved_funds(invocation_id)

    with get_conn() as conn:
        conn.execute(
            "UPDATE bounties SET status = 'settled', updated_at = ? WHERE id = ?",
            (now, bounty_id),
        )
        row = conn.execute(
            _bounty_row_select() + " WHERE b.id = ?", (bounty_id,)
        ).fetchone()

    # Settle any active delivery for this bounty — this also deletes cached bytes.
    try:
        from features.economy.store import list_active_deliveries_for_order, mark_delivery_settled
        for d in list_active_deliveries_for_order(bounty_id, "bounty"):
            mark_delivery_settled(d["id"])
    except Exception:
        # Don't let delivery bookkeeping block the settlement itself.
        pass

    assert row is not None
    return row, settled_invocation


def cancel_bounty(bounty_id: str, poster_claw_id: str) -> sqlite3.Row:
    poster = get_lobster_by_claw_id(poster_claw_id)
    if poster is None:
        raise ValueError("Poster lobster not found.")
    now = utc_now()
    invocation_id_to_release: str | None = None
    with get_conn() as conn:
        bounty_raw = conn.execute("SELECT * FROM bounties WHERE id = ?", (bounty_id,)).fetchone()
        if bounty_raw is None:
            raise ValueError("Bounty not found.")
        if bounty_raw["poster_lobster_id"] != poster["id"]:
            raise ValueError("Only the poster can cancel a bounty.")
        if bounty_raw["status"] in ("fulfilled", "expired", "cancelled", "settled"):
            raise ValueError(f"Cannot cancel a bounty with status '{bounty_raw['status']}'.")
        invocation_id_to_release = str(bounty_raw["invocation_id"] or "").strip() or None
        conn.execute(
            "UPDATE bounty_bids SET status = 'cancelled' WHERE bounty_id = ? AND status IN ('pending', 'selected')",
            (bounty_id,),
        )
        conn.execute(
            "UPDATE bounties SET status = 'cancelled', cancelled_at = ?, updated_at = ? WHERE id = ?",
            (now, now, bounty_id),
        )
        row = conn.execute(
            _bounty_row_select() + " WHERE b.id = ?", (bounty_id,)
        ).fetchone()

    # Release reserved escrow funds AFTER the bounty connection closes, so we
    # don't nest connections (release_reserved_funds opens its own).
    if invocation_id_to_release:
        from features.economy.store import release_reserved_funds, EscrowError
        try:
            release_reserved_funds(invocation_id_to_release)
        except EscrowError:
            # Already released or settled — that's OK, the bounty cancel still stands.
            pass
    assert row is not None
    return row
