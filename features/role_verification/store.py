"""Database operations for role verification."""

from __future__ import annotations

import re
import sqlite3

from server.store import (
    get_conn,
    get_lobster_by_claw_id,
    get_official_lobster,
    new_uuid,
    utc_now,
    create_verification_code,
    OFFICIAL_CLAW_ID,
)
from server.sms import generate_code, CODE_EXPIRY_SECONDS, SEND_COOLDOWN_SECONDS


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def ensure_role_columns() -> None:
    """Add role-related columns to lobsters table if missing."""
    with get_conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(lobsters)").fetchall()}
        for col, sql in [
            ("role", "TEXT"),
            ("org_name", "TEXT"),
            ("real_name", "TEXT"),
            ("role_verified", "INTEGER DEFAULT 0"),
            ("role_verified_at", "TEXT"),
            ("verified_email", "TEXT"),
            ("email_verified_at", "TEXT"),
        ]:
            if col not in columns:
                conn.execute(f"ALTER TABLE lobsters ADD COLUMN {col} {sql}")

        # Role applications table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS role_applications (
                id TEXT PRIMARY KEY,
                lobster_id TEXT NOT NULL,
                claw_id TEXT NOT NULL,
                role TEXT NOT NULL,
                org_name TEXT NOT NULL,
                real_name TEXT NOT NULL,
                supporting_url TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewer_note TEXT,
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            )
        """)

        # Email verification codes (reuse verification_codes table structure)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS email_verification_codes (
                id TEXT PRIMARY KEY,
                lobster_id TEXT NOT NULL,
                email TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0
            )
        """)


# ---------------------------------------------------------------------------
# Known institutional email domains (auto-approve)
# ---------------------------------------------------------------------------

KNOWN_INSTITUTIONAL_DOMAINS = {
    # Example VC domains — extend as needed
    "sequoiacap.com",
    "matrixpartners.com",
    "hillhousecap.com",
    "ggvc.com",
    "zhenfund.com",
    "5ycap.com",
    "baidu.com",
    "alibaba-inc.com",
    "tencent.com",
}


def is_institutional_email(email: str) -> bool:
    """Check if an email belongs to a known institutional domain."""
    domain = email.strip().lower().rsplit("@", 1)[-1]
    return domain in KNOWN_INSTITUTIONAL_DOMAINS


def is_public_email(email: str) -> bool:
    """Check if an email is a public/free email provider."""
    public_domains = {
        "gmail.com", "qq.com", "163.com", "126.com", "outlook.com",
        "hotmail.com", "yahoo.com", "foxmail.com", "icloud.com",
        "sina.com", "sohu.com",
    }
    domain = email.strip().lower().rsplit("@", 1)[-1]
    return domain in public_domains


def validate_email(email: str) -> str:
    """Validate email format and return cleaned email."""
    email = email.strip().lower()
    if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email):
        raise ValueError("请输入有效的邮箱地址。")
    return email


# ---------------------------------------------------------------------------
# Role application
# ---------------------------------------------------------------------------

def submit_role_application(
    claw_id: str,
    role: str,
    org_name: str,
    real_name: str,
    supporting_url: str | None = None,
) -> dict:
    """Submit a role application. Returns application dict."""
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")

    # Check if already verified
    if lobster["role_verified"]:
        raise ValueError("你已通过角色认证，无需重复申请。")

    # Check phone verification
    if not lobster["verified_phone"]:
        raise ValueError("请先完成手机验证，再申请角色认证。")

    lobster_id = str(lobster["id"])
    now = utc_now()
    app_id = new_uuid()

    with get_conn() as conn:
        # Check for existing pending application
        existing = conn.execute(
            "SELECT id FROM role_applications WHERE lobster_id = ? AND status = 'pending'",
            (lobster_id,),
        ).fetchone()
        if existing is not None:
            raise ValueError("你有一个待审核的申请，请等待审核结果。")

        # Update lobster profile
        conn.execute(
            "UPDATE lobsters SET role = ?, org_name = ?, real_name = ?, updated_at = ? WHERE id = ?",
            (role, org_name.strip(), real_name.strip(), now, lobster_id),
        )

        # Create application
        conn.execute(
            """
            INSERT INTO role_applications (id, lobster_id, claw_id, role, org_name, real_name, supporting_url, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (app_id, lobster_id, claw_id, role, org_name.strip(), real_name.strip(), supporting_url, now),
        )

    return {
        "application_id": app_id,
        "claw_id": claw_id,
        "role": role,
        "org_name": org_name.strip(),
        "real_name": real_name.strip(),
        "status": "pending",
    }


def get_pending_applications() -> list[dict]:
    """Get all pending role applications (for official lobster review)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ra.*, l.verified_phone, l.did, l.public_key, l.created_at as lobster_created_at
            FROM role_applications ra
            JOIN lobsters l ON l.id = ra.lobster_id
            WHERE ra.status = 'pending'
            ORDER BY ra.created_at ASC
            """,
        ).fetchall()
    return [dict(row) for row in rows]


def review_application(application_id: str, decision: str, reason: str | None = None) -> dict:
    """Review a role application. Returns updated application."""
    now = utc_now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM role_applications WHERE id = ?", (application_id,)
        ).fetchone()
        if row is None:
            raise ValueError("申请不存在。")
        if row["status"] != "pending":
            raise ValueError("该申请已处理。")

        conn.execute(
            "UPDATE role_applications SET status = ?, reviewer_note = ?, reviewed_at = ? WHERE id = ?",
            (decision, reason, now, application_id),
        )

        if decision == "approved":
            conn.execute(
                "UPDATE lobsters SET role_verified = 1, role_verified_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["lobster_id"]),
            )

    return {
        "application_id": application_id,
        "claw_id": row["claw_id"],
        "role": row["role"],
        "status": decision,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Build review message for official lobster
# ---------------------------------------------------------------------------

def build_review_message(application: dict, lobster_stats: dict | None = None) -> str:
    """Build a formatted review message to send to official lobster."""
    role_label = {"founder": "创业者", "investor": "投资人", "both": "创业者+投资人"}
    lines = [
        "━━━ 角色认证申请 ━━━",
        f"龙虾：{application['claw_id']}",
        f"申请角色：{role_label.get(application['role'], application['role'])}",
        "",
        "📋 申请人填写：",
        f"  机构：{application['org_name']}",
        f"  姓名：{application['real_name']}",
    ]
    if application.get("supporting_url"):
        lines.append(f"  佐证链接：{application['supporting_url']}")

    lines.append("")
    lines.append("🔍 系统信息（不可伪造）：")
    lines.append(f"  手机验证：✓")
    if application.get("did"):
        lines.append(f"  密钥绑定：✓")
    else:
        lines.append(f"  密钥绑定：✗")

    lines.append("")
    lines.append(f"申请ID：{application['application_id']}")
    lines.append("回复格式：沙堆 审核 <申请ID> 通过/拒绝")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

def create_email_verification_code(lobster_id: str, email: str, code: str, expiry_seconds: int) -> None:
    """Store an email verification code."""
    from datetime import datetime, timezone, timedelta
    now = utc_now()
    expires_at = (datetime.fromisoformat(now) + timedelta(seconds=expiry_seconds)).isoformat()
    code_id = new_uuid()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO email_verification_codes (id, lobster_id, email, code, created_at, expires_at, used)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (code_id, lobster_id, email, code, now, expires_at),
        )


def get_email_last_sent_time(lobster_id: str, email: str) -> str | None:
    """Get the created_at of the most recent code sent to this email."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT created_at FROM email_verification_codes WHERE lobster_id = ? AND email = ? ORDER BY created_at DESC LIMIT 1",
            (lobster_id, email),
        ).fetchone()
    return str(row["created_at"]) if row else None


def verify_email(claw_id: str, email: str, code: str) -> dict:
    """Verify an email address. Returns result dict with auto_approved flag."""
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    lobster_id = str(lobster["id"])

    now = utc_now()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM email_verification_codes
            WHERE lobster_id = ? AND email = ? AND used = 0 AND expires_at > ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (lobster_id, email, now),
        ).fetchone()
        if row is None:
            raise ValueError("验证码无效或已过期，请重新发送。")

        attempts = int(row["attempts"])
        if attempts >= 5:
            conn.execute("UPDATE email_verification_codes SET used = 1 WHERE id = ?", (row["id"],))
            conn.commit()
            raise ValueError("验证码错误次数过多，请重新发送。")

        if str(row["code"]) != code.strip():
            conn.execute("UPDATE email_verification_codes SET attempts = ? WHERE id = ?", (attempts + 1, row["id"]))
            conn.commit()
            raise ValueError("验证码错误。")

        # Mark all codes for this lobster+email as used
        conn.execute("UPDATE email_verification_codes SET used = 1 WHERE lobster_id = ? AND email = ?", (lobster_id, email))

        # Store verified email
        conn.execute(
            "UPDATE lobsters SET verified_email = ?, email_verified_at = ?, updated_at = ? WHERE id = ?",
            (email, now, now, lobster_id),
        )

    # Check if institutional email → auto approve role
    auto_approved = False
    if is_institutional_email(email) and lobster["role"] and not lobster["role_verified"]:
        with get_conn() as conn:
            conn.execute(
                "UPDATE lobsters SET role_verified = 1, role_verified_at = ?, updated_at = ? WHERE id = ?",
                (now, now, lobster_id),
            )
            # Also update any pending application
            conn.execute(
                "UPDATE role_applications SET status = 'approved', reviewer_note = '机构邮箱自动通过', reviewed_at = ? WHERE lobster_id = ? AND status = 'pending'",
                (now, lobster_id),
            )
        auto_approved = True

    return {
        "email": email,
        "verified": True,
        "auto_approved": auto_approved,
    }
