from __future__ import annotations

import random
import sqlite3
import string
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
            """
        )
    seed_official_lobster()


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
            SELECT id, runtime_id, claw_id, name, owner_name, is_official, created_at, updated_at
            FROM lobsters
            WHERE runtime_id = ?
            """,
            (runtime_id,),
        ).fetchone()


def get_lobster_by_claw_id(claw_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, runtime_id, claw_id, name, owner_name, is_official, created_at, updated_at
            FROM lobsters
            WHERE claw_id = ?
            """,
            (claw_id.strip().upper(),),
        ).fetchone()


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
                SET claw_id = ?, name = ?, owner_name = ?, is_official = 1, updated_at = ?
                WHERE runtime_id = ?
                """,
                (OFFICIAL_CLAW_ID, OFFICIAL_NAME, OFFICIAL_OWNER, now, OFFICIAL_RUNTIME_ID),
            )
        return get_official_lobster()

    lobster_id = new_uuid()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO lobsters (id, runtime_id, claw_id, name, owner_name, is_official, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (lobster_id, OFFICIAL_RUNTIME_ID, OFFICIAL_CLAW_ID, OFFICIAL_NAME, OFFICIAL_OWNER, now, now),
        )
    return get_official_lobster()


def register_lobster(runtime_id: str, name: str, owner_name: str) -> tuple[sqlite3.Row, bool]:
    existing = _lobster_by_runtime_id(runtime_id)
    now = utc_now()
    if existing is not None:
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE lobsters
                SET name = ?, owner_name = ?, updated_at = ?
                WHERE runtime_id = ?
                """,
                (name, owner_name, now, runtime_id),
            )
        lobster = _lobster_by_runtime_id(runtime_id)
    else:
        with get_conn() as conn:
            claw_id = _generate_claw_id(conn)
            lobster_id = new_uuid()
            conn.execute(
                """
                INSERT INTO lobsters (id, runtime_id, claw_id, name, owner_name, is_official, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (lobster_id, runtime_id, claw_id, name, owner_name, now, now),
            )
        lobster = _lobster_by_runtime_id(runtime_id)

    assert lobster is not None
    official = get_official_lobster()
    auto_created = ensure_friendship(lobster["id"], official["id"])
    return lobster, auto_created


def search_lobsters(query: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
    sql = """
        SELECT id, runtime_id, claw_id, name, owner_name, is_official, created_at, updated_at
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


def create_friend_request(from_claw_id: str, to_claw_id: str) -> sqlite3.Row:
    from_lobster = get_lobster_by_claw_id(from_claw_id)
    to_lobster = get_lobster_by_claw_id(to_claw_id)
    if from_lobster is None or to_lobster is None:
        raise ValueError("Both lobsters must exist.")
    if from_lobster["id"] == to_lobster["id"]:
        raise ValueError("Cannot add yourself.")
    if are_friends_by_id(from_lobster["id"], to_lobster["id"]):
        raise ValueError("You are already friends.")

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
        content=f"{from_lobster['name']} wants to add you as a friend.",
        status="pending",
    )
    return get_friend_request(request_id)


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
        content=f"{updated['to_name']} {decision} your friend request.",
        status=decision,
    )
    return updated


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


def create_message(from_claw_id: str, to_claw_id: str, content: str, message_type: str) -> sqlite3.Row:
    from_lobster = get_lobster_by_claw_id(from_claw_id)
    to_lobster = get_lobster_by_claw_id(to_claw_id)
    if from_lobster is None or to_lobster is None:
        raise ValueError("Both lobsters must be registered before messaging.")
    if not are_friends_by_id(from_lobster["id"], to_lobster["id"]):
        raise ValueError("Only friends can send messages.")
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
