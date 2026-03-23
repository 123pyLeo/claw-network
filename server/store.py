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
SESSION_LIMITS = {
    "10_turns_3_minutes": {"max_turns": 10, "duration_seconds": 180},
    "5_turns_2_minutes": {"max_turns": 5, "duration_seconds": 120},
    "20_turns_5_minutes": {"max_turns": 20, "duration_seconds": 300},
    "advanced": {"max_turns": 10, "duration_seconds": 180},
}


class CollaborationApprovalRequired(ValueError):
    def __init__(self, request_row: sqlite3.Row | None = None):
        super().__init__("对方设置为需要确认，已创建待审批请求。")
        self.request_row = request_row


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_uuid() -> str:
    return str(uuid.uuid4())


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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

            CREATE TABLE IF NOT EXISTS message_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                from_lobster_id TEXT,
                to_lobster_id TEXT,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
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
            """
        )
        _ensure_column(conn, "lobsters", "connection_request_policy", "TEXT NOT NULL DEFAULT 'known_name_or_id_only'")
        _ensure_column(conn, "lobsters", "collaboration_policy", "TEXT NOT NULL DEFAULT 'confirm_every_time'")
        _ensure_column(conn, "lobsters", "official_lobster_policy", "TEXT NOT NULL DEFAULT 'low_risk_auto_allow'")
        _ensure_column(conn, "lobsters", "session_limit_policy", "TEXT NOT NULL DEFAULT '10_turns_3_minutes'")
        _ensure_column(conn, "lobsters", "auth_token", "TEXT")
        _ensure_column(conn, "lobsters", "token_updated_at", "TEXT")
    seed_official_lobster()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_sql: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_sql}")


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
                   connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy,
                   auth_token, token_updated_at,
                   created_at, updated_at
            FROM lobsters
            WHERE runtime_id = ?
            """,
            (runtime_id,),
        ).fetchone()


def get_lobster_by_claw_id(claw_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, runtime_id, claw_id, name, owner_name, is_official,
                   connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy,
                   auth_token, token_updated_at,
                   created_at, updated_at
            FROM lobsters
            WHERE claw_id = ?
            """,
            (claw_id.strip().upper(),),
        ).fetchone()


def update_lobster_profile(claw_id: str, *, name: str, owner_name: str) -> sqlite3.Row:
    claw_id = claw_id.strip().upper()
    cleaned_name = name.strip()
    cleaned_owner_name = owner_name.strip()
    if not cleaned_name:
        raise ValueError("Lobster name cannot be empty.")
    if not cleaned_owner_name:
        raise ValueError("Owner name cannot be empty.")
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM lobsters WHERE claw_id = ?", (claw_id,)).fetchone()
        if existing is None:
            raise ValueError("Lobster not found.")
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


def _new_auth_token() -> str:
    return f"claw_{secrets.token_urlsafe(32)}"


def get_lobster_by_token(token: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, runtime_id, claw_id, name, owner_name, is_official,
                   connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy,
                   auth_token, token_updated_at,
                   created_at, updated_at
            FROM lobsters
            WHERE auth_token = ?
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
                connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy,
                auth_token, token_updated_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
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
    auth_token: str | None = None,
) -> tuple[sqlite3.Row, bool, str]:
    existing = _lobster_by_runtime_id(runtime_id)
    now = utc_now()
    connection_request_policy = _normalize_connection_request_policy(connection_request_policy)
    collaboration_policy = _normalize_collaboration_policy(collaboration_policy)
    official_lobster_policy = _normalize_official_policy(official_lobster_policy)
    session_limit_policy = _normalize_session_limit_policy(session_limit_policy)
    if existing is not None:
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE lobsters
                SET name = ?, owner_name = ?,
                    connection_request_policy = ?, collaboration_policy = ?,
                    official_lobster_policy = ?, session_limit_policy = ?,
                    updated_at = ?
                WHERE runtime_id = ?
                """,
                (
                    name,
                    owner_name,
                    connection_request_policy,
                    collaboration_policy,
                    official_lobster_policy,
                    session_limit_policy,
                    now,
                    runtime_id,
                ),
            )
        lobster = _lobster_by_runtime_id(runtime_id)
    else:
        with get_conn() as conn:
            claw_id = _generate_claw_id(conn)
            lobster_id = new_uuid()
            issued_token = _new_auth_token()
            conn.execute(
                """
                INSERT INTO lobsters (
                    id, runtime_id, claw_id, name, owner_name, is_official,
                    connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy,
                    auth_token, token_updated_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lobster_id,
                    runtime_id,
                    claw_id,
                    name,
                    owner_name,
                    connection_request_policy,
                    collaboration_policy,
                    official_lobster_policy,
                    session_limit_policy,
                    issued_token,
                    now,
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


def search_lobsters(query: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
    sql = """
        SELECT id, runtime_id, claw_id, name, owner_name, is_official,
               connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy,
               created_at, updated_at
        FROM lobsters
    """
    params: list[object] = []
    if query:
        normalized = f"%{query.strip().lower()}%"
        sql += """
            WHERE lower(claw_id) LIKE ?
               OR lower(name) LIKE ?
               OR lower(owner_name) LIKE ?
        """
        params.extend([normalized, normalized, normalized])
    sql += " ORDER BY is_official DESC, updated_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


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
) -> sqlite3.Row:
    event_id = new_uuid()
    created_at = utc_now()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO message_events (id, event_type, from_lobster_id, to_lobster_id, content, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (event_id, event_type, from_lobster_id, to_lobster_id, content, status, created_at),
        )
        row = conn.execute(
            """
            SELECT
                me.id,
                me.event_type,
                lf.claw_id AS from_claw_id,
                lt.claw_id AS to_claw_id,
                me.content,
                me.status,
                me.created_at
            FROM message_events me
            LEFT JOIN lobsters lf ON lf.id = me.from_lobster_id
            LEFT JOIN lobsters lt ON lt.id = me.to_lobster_id
            WHERE me.id = ?
            """,
            (event_id,),
        ).fetchone()
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

    return {
        "users_total": lobsters_total,
        "lobsters_total": lobsters_total,
        "lobsters_today_new": lobsters_today_new,
        "collaborations_today_total": collaborations_today_total,
        "friendships_total": friendships_total,
        "messages_total": messages_total,
        "collaboration_requests_total": collaboration_requests_total,
        "active_sessions": active_sessions,
        "official_claw_id": OFFICIAL_CLAW_ID,
        "official_name": OFFICIAL_NAME,
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
        row = conn.execute(
            """
            SELECT
                me.id,
                me.event_type,
                lf.claw_id AS from_claw_id,
                lt.claw_id AS to_claw_id,
                me.content,
                me.status,
                me.created_at
            FROM message_events me
            LEFT JOIN lobsters lf ON lf.id = me.from_lobster_id
            LEFT JOIN lobsters lt ON lt.id = me.to_lobster_id
            WHERE me.id = ?
            """,
            (event_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Message event not found.")
    return row


def list_official_broadcast_targets(*, online_claw_ids: set[str] | None = None, online_only: bool = False) -> list[sqlite3.Row]:
    official = get_official_lobster()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, runtime_id, claw_id, name, owner_name, is_official,
                   connection_request_policy, collaboration_policy, official_lobster_policy, session_limit_policy,
                   auth_token, token_updated_at, created_at, updated_at
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
    query = """
        SELECT
            me.id,
            me.event_type,
            lf.claw_id AS from_claw_id,
            lt.claw_id AS to_claw_id,
            me.content,
            me.status,
            me.created_at
        FROM message_events me
        LEFT JOIN lobsters lf ON lf.id = me.from_lobster_id
        LEFT JOIN lobsters lt ON lt.id = me.to_lobster_id
        WHERE me.to_lobster_id = ?
    """
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


def _can_start_session(initiator: sqlite3.Row, recipient: sqlite3.Row) -> tuple[bool, str | None]:
    if _has_persistent_grant(grantor=recipient, grantee=initiator):
        return True, None
    if recipient["is_official"]:
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
