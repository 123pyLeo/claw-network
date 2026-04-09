"""Platform layer: trusted-frontend access (sandpile-website BFF, future apps).

Provides:
  - platform_tokens table: identifies trusted callers (e.g. sandpile-website backend)
  - phone code send/verify decoupled from any lobster context
    (the website doesn't have a lobster yet at login time)
  - helpers to bridge phone → owner_id without going through the
    lobster-centric sidecar self-registration flow

Design rule: this module is the ONLY place that exposes "operate as any owner"
capabilities. Every function here that mutates state requires a verified
platform token at the route layer (see routes.py).
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from server.store import get_conn, new_uuid, utc_now


# How long a platform-issued phone code is valid
PLATFORM_CODE_EXPIRY_SECONDS = 5 * 60
# Cooldown between successive sends to the same phone via the platform path
PLATFORM_SEND_COOLDOWN_SECONDS = 60
# Max wrong attempts per code
PLATFORM_MAX_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def ensure_platform_tables() -> None:
    with get_conn() as conn:
        # platform_tokens: trusted-frontend bearer tokens.
        # The token VALUE is sensitive — treat it like a database password.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_tokens (
                token TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            )
            """
        )

        # Phone verification codes for the platform path. Decoupled from
        # verification_codes (which is keyed by lobster_id) because at login
        # time the caller has no lobster yet — only a phone number.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_phone_codes (
                id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_platform_phone_codes_phone "
            "ON platform_phone_codes(phone, created_at)"
        )


# ---------------------------------------------------------------------------
# Platform tokens
# ---------------------------------------------------------------------------

def register_platform_token(token: str, name: str) -> None:
    """Register a platform token. Idempotent — re-running with the same
    token+name is a no-op."""
    if not token or not name:
        raise ValueError("token and name are required.")
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT token FROM platform_tokens WHERE token = ?", (token,)
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO platform_tokens (token, name, created_at) VALUES (?, ?, ?)",
            (token, name, utc_now()),
        )


def verify_platform_token(token: str | None) -> dict | None:
    """Look up a platform token. Returns the row dict or None."""
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM platform_tokens WHERE token = ?", (token.strip(),)
        ).fetchone()
        if row is None:
            return None
        # Touch last_used_at (fire-and-forget)
        try:
            conn.execute(
                "UPDATE platform_tokens SET last_used_at = ? WHERE token = ?",
                (utc_now(), token.strip()),
            )
        except Exception:
            pass
        return dict(row)


# ---------------------------------------------------------------------------
# Phone verification (no lobster required)
# ---------------------------------------------------------------------------

def _get_last_platform_send_time(phone: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT created_at FROM platform_phone_codes
            WHERE phone = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (phone,),
        ).fetchone()
    return str(row["created_at"]) if row else None


def create_platform_phone_code(phone: str, code: str) -> dict:
    """Insert a new platform phone code. Enforces cooldown."""
    last_sent = _get_last_platform_send_time(phone)
    if last_sent:
        elapsed = (
            datetime.now(timezone.utc) - datetime.fromisoformat(last_sent)
        ).total_seconds()
        if elapsed < PLATFORM_SEND_COOLDOWN_SECONDS:
            remaining = int(PLATFORM_SEND_COOLDOWN_SECONDS - elapsed)
            raise ValueError(f"发送过于频繁，请 {remaining} 秒后重试。")

    now = utc_now()
    expires_at = (
        datetime.fromisoformat(now) + timedelta(seconds=PLATFORM_CODE_EXPIRY_SECONDS)
    ).isoformat()
    code_id = new_uuid()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO platform_phone_codes (id, phone, code, created_at, expires_at, used)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (code_id, phone, code, now, expires_at),
        )
        row = conn.execute(
            "SELECT * FROM platform_phone_codes WHERE id = ?", (code_id,)
        ).fetchone()
    return dict(row)


def verify_platform_phone_code(phone: str, code: str) -> bool:
    """Verify a phone code submitted via the platform path.

    Raises ValueError on any failure. Returns True on success.
    On success, all unused codes for this phone are marked as consumed.

    DEV MODE (CLAW_DEV_LOGIN=1): the actual code value is NOT checked.
    Any non-empty code is accepted. This makes manual UI testing painless
    — testers don't have to dig the real code out of the DB / server log
    every time they want to log in or do a sudo-protected action. Owner
    creation and session issuance still work normally; only the equality
    check between the typed code and the SMS-generated code is bypassed.
    """
    import os
    dev_mode = os.environ.get("CLAW_DEV_LOGIN", "").strip() == "1"

    if dev_mode:
        # Just consume any pending codes for this phone (so the next
        # send-code won't immediately hit the cooldown index). Don't
        # require any code row to exist — testers may bypass
        # send-code entirely and call verify-phone directly.
        with get_conn() as conn:
            conn.execute(
                "UPDATE platform_phone_codes SET used = 1 WHERE phone = ? AND used = 0",
                (phone,),
            )
        return True

    now = utc_now()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM platform_phone_codes
            WHERE phone = ? AND used = 0 AND expires_at > ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (phone, now),
        ).fetchone()
        if row is None:
            raise ValueError("验证码无效或已过期，请重新发送。")

        attempts = int(row["attempts"])
        if attempts >= PLATFORM_MAX_ATTEMPTS:
            conn.execute(
                "UPDATE platform_phone_codes SET used = 1 WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
            raise ValueError("验证码错误次数过多，请重新发送。")

        if str(row["code"]) != code.strip():
            conn.execute(
                "UPDATE platform_phone_codes SET attempts = ? WHERE id = ?",
                (attempts + 1, row["id"]),
            )
            conn.commit()
            raise ValueError("验证码错误。")

        # Success: invalidate all unused codes for this phone
        conn.execute(
            "UPDATE platform_phone_codes SET used = 1 WHERE phone = ? AND used = 0",
            (phone,),
        )
        return True


# ---------------------------------------------------------------------------
# Lobster status helpers (for the dashboard)
# ---------------------------------------------------------------------------

def list_lobsters_for_owner_with_status(owner_id: str, *, include_deleted: bool = False) -> list[dict]:
    """Return all lobsters for an owner with the dashboard-relevant fields.

    Includes last_seen_at and registration_source for online indicator and
    badge rendering on the website.
    """
    sql = """
        SELECT id, claw_id, name, owner_name, runtime_id,
               registration_source, description, model_hint,
               last_seen_at, deleted_at,
               created_at, updated_at
        FROM lobsters
        WHERE owner_id = ?
    """
    params: list = [owner_id]
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    sql += " ORDER BY created_at ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]
