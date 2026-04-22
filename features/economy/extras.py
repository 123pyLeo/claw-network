"""Redemption codes + delivery mechanism (attachment handling, confirmation windows).

This is the 'misc add-on features' bucket. They share a theme of being
supplementary to the core Agent-economy flow.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from server.store import get_conn, new_uuid, utc_now


# ---------------------------------------------------------------------------
# Redemption codes (兑换码): admin-issued, one-shot credit top-up
# ---------------------------------------------------------------------------

def ensure_redemption_tables() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS redemption_codes (
                code TEXT PRIMARY KEY,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'unused',
                note TEXT,
                created_at TEXT NOT NULL,
                used_by_owner_id TEXT,
                used_by_claw_id TEXT,
                used_at TEXT,
                FOREIGN KEY (used_by_owner_id) REFERENCES owners(id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_redemption_status ON redemption_codes(status, created_at)"
        )


def _generate_redemption_code() -> str:
    """Human-friendly 12-char code: SP-XXXX-XXXX (omit ambiguous chars)."""
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    chunk1 = "".join(secrets.choice(alphabet) for _ in range(4))
    chunk2 = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"SP-{chunk1}-{chunk2}"


def create_redemption_codes(amount: int, count: int, note: str | None = None) -> list[str]:
    if amount <= 0:
        raise ValueError("面额必须为正。")
    if count <= 0 or count > 500:
        raise ValueError("数量必须在 1–500 之间。")
    now = utc_now()
    codes: list[str] = []
    with get_conn() as conn:
        while len(codes) < count:
            code = _generate_redemption_code()
            try:
                conn.execute(
                    "INSERT INTO redemption_codes (code, amount, status, note, created_at) "
                    "VALUES (?, ?, 'unused', ?, ?)",
                    (code, amount, note, now),
                )
                codes.append(code)
            except sqlite3.IntegrityError:
                # Very unlikely collision — retry with a new code.
                continue
    return codes


def redeem_code(code: str, owner_id: str, claw_id: str) -> dict:
    """Atomically mark code as used and credit the owner's balance.

    Raises ValueError if the code is invalid/used. Returns
    {code, amount, new_balance}.
    """
    normalized = (code or "").strip().upper()
    if not normalized:
        raise ValueError("兑换码不能为空。")
    now = utc_now()
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE redemption_codes "
            "SET status = 'used', used_by_owner_id = ?, used_by_claw_id = ?, used_at = ? "
            "WHERE code = ? AND status = 'unused'",
            (owner_id, claw_id, now, normalized),
        )
        if cursor.rowcount == 0:
            row = conn.execute(
                "SELECT status FROM redemption_codes WHERE code = ?", (normalized,)
            ).fetchone()
            if row is None:
                raise ValueError("兑换码无效。")
            raise ValueError("兑换码已被使用。")
        row = conn.execute(
            "SELECT amount FROM redemption_codes WHERE code = ?", (normalized,)
        ).fetchone()
        amount = int(row["amount"])
        credited = conn.execute(
            "UPDATE accounts SET credit_balance = credit_balance + ?, updated_at = ? "
            "WHERE owner_id = ?",
            (amount, now, owner_id),
        )
        if credited.rowcount == 0:
            conn.execute(
                "INSERT INTO accounts (owner_id, credit_balance, updated_at) "
                "VALUES (?, ?, ?)",
                (owner_id, amount, now),
            )
        balance_row = conn.execute(
            "SELECT credit_balance FROM accounts WHERE owner_id = ?", (owner_id,)
        ).fetchone()
    return {
        "code": normalized,
        "amount": amount,
        "new_balance": int(balance_row["credit_balance"]),
    }


def void_redemption_code(code: str) -> dict:
    """Mark an unused code as voided so it can never be redeemed."""
    normalized = (code or "").strip().upper()
    if not normalized:
        raise ValueError("兑换码不能为空。")
    now = utc_now()
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE redemption_codes SET status = 'voided', used_at = ? "
            "WHERE code = ? AND status = 'unused'",
            (now, normalized),
        )
        if cursor.rowcount == 0:
            row = conn.execute(
                "SELECT status FROM redemption_codes WHERE code = ?", (normalized,)
            ).fetchone()
            if row is None:
                raise ValueError("兑换码不存在。")
            raise ValueError(f"当前状态为 {row['status']},无法作废。")
    return {"code": normalized, "status": "voided"}


def list_redemption_codes(limit: int = 100, status: str | None = None) -> list[dict]:
    base = (
        "SELECT r.*, o.nickname AS owner_nickname, o.auth_phone AS owner_phone "
        "FROM redemption_codes r LEFT JOIN owners o ON o.id = r.used_by_owner_id"
    )
    with get_conn() as conn:
        if status in ("unused", "used"):
            rows = conn.execute(
                f"{base} WHERE r.status = ? ORDER BY r.created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"{base} ORDER BY r.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def redemption_code_summary() -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT "
            "SUM(CASE WHEN status = 'unused' THEN 1 ELSE 0 END) AS unused_count, "
            "SUM(CASE WHEN status = 'used' THEN 1 ELSE 0 END) AS used_count, "
            "SUM(CASE WHEN status = 'voided' THEN 1 ELSE 0 END) AS voided_count, "
            "SUM(CASE WHEN status = 'used' THEN amount ELSE 0 END) AS redeemed_amount "
            "FROM redemption_codes"
        ).fetchone()
    return {
        "unused_count": int(row["unused_count"] or 0),
        "used_count": int(row["used_count"] or 0),
        "voided_count": int(row["voided_count"] or 0),
        "redeemed_amount": int(row["redeemed_amount"] or 0),
    }


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


# ---------------------------------------------------------------------------
# Delivery mechanism (v1) — 交付机制
#
# See /home/openclaw-a2a-mvp/docs/DELIVERY_MECHANISM.md for the full design.
# v1 scope: 说明 + 最多 5 附件, 5 种附件类型, best-effort hash, short TTL buffer.
# NOT in v1: 返工, 争议, agent 自主交付 (parked in design doc).
# ---------------------------------------------------------------------------

import hashlib
import json
import os
import pathlib

DELIVERY_CONFIRMATION_WINDOWS = ("24h", "3d", "7d", "14d")
DELIVERY_DEFAULT_WINDOW = "7d"

DELIVERY_MAX_ATTACHMENTS = 5
DELIVERY_MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB
DELIVERY_MAX_ENVELOPE_BYTES = 20 * 1024 * 1024  # 20 MB
DELIVERY_OWNER_QUOTA_BYTES = 200 * 1024 * 1024  # 200 MB soft cap
DELIVERY_GLOBAL_QUOTA_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB hard cap

ATTACHMENT_KINDS = ("text", "image", "file", "code", "link")

# Where uploaded bytes land. Gitignored. Subdir per attachment id to avoid
# filename collisions and make cleanup atomic.
def _delivery_bytes_dir() -> pathlib.Path:
    # Use the same base as the SQLite DB so ops/backups stay together.
    from server.store import DB_PATH
    base = pathlib.Path(DB_PATH).parent / "deliveries"
    base.mkdir(parents=True, exist_ok=True)
    return base


def ensure_delivery_tables() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deliveries (
                id                 TEXT PRIMARY KEY,
                order_id           TEXT NOT NULL,
                order_kind         TEXT NOT NULL,
                submitter_owner_id TEXT NOT NULL,
                receiver_owner_id  TEXT NOT NULL,
                note               TEXT NOT NULL,
                status             TEXT NOT NULL DEFAULT 'active',
                created_at         TEXT NOT NULL,
                expires_at         TEXT NOT NULL,
                settled_at         TEXT,
                withdrawn_at       TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deliveries_order "
            "ON deliveries(order_id, order_kind)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_deliveries_status "
            "ON deliveries(status, expires_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS delivery_attachments (
                id            TEXT PRIMARY KEY,
                delivery_id   TEXT NOT NULL,
                kind          TEXT NOT NULL,
                content       TEXT,
                payload_url   TEXT,
                filename      TEXT,
                byte_size     INTEGER,
                hash          TEXT,
                verifiability TEXT NOT NULL DEFAULT 'unverifiable',
                deleted_at    TEXT,
                created_at    TEXT NOT NULL,
                FOREIGN KEY (delivery_id) REFERENCES deliveries(id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attach_delivery "
            "ON delivery_attachments(delivery_id)"
        )


def ensure_confirmation_window_columns() -> None:
    """Idempotent migration: add confirmation_window to bounties + deals."""
    with get_conn() as conn:
        b_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bounties)").fetchall()}
        if "confirmation_window" not in b_cols:
            conn.execute(
                "ALTER TABLE bounties ADD COLUMN confirmation_window TEXT NOT NULL DEFAULT '7d'"
            )
        d_cols = {row["name"] for row in conn.execute("PRAGMA table_info(deals)").fetchall()}
        if "confirmation_window" not in d_cols:
            conn.execute(
                "ALTER TABLE deals ADD COLUMN confirmation_window TEXT NOT NULL DEFAULT '7d'"
            )


def _window_to_seconds(window: str) -> int:
    mapping = {"24h": 24 * 3600, "3d": 3 * 86400, "7d": 7 * 86400, "14d": 14 * 86400}
    return mapping.get(window, 7 * 86400)


def _compute_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_git_commit(url: str) -> str | None:
    """Pick out commit SHA from common code-host URLs.

    Supports patterns like:
      github.com/{o}/{r}/commit/{sha}
      github.com/{o}/{r}/pull/{n}/commits/{sha}
      github.com/{o}/{r}/blob/{sha}/...
    Returns the SHA (40 hex, or 7-char short) or None.
    """
    import re
    m = re.search(r"/(commit|commits|blob|tree)/([a-f0-9]{7,40})", url)
    return m.group(2) if m else None


def _try_fetch_hash_for_url(url: str, timeout: float = 3.0) -> str | None:
    """Best-effort GET + SHA-256 for a public URL. Returns None on any failure."""
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "sandpile-delivery/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            # Cap to 10MB so a bad URL can't spike our memory
            data = resp.read(DELIVERY_MAX_FILE_BYTES + 1)
            if len(data) > DELIVERY_MAX_FILE_BYTES:
                return None
            return hashlib.sha256(data).hexdigest()
    except Exception:
        return None


def _classify_attachment(kind: str, payload: dict) -> tuple[str, str, str]:
    """Normalize attachment input and compute (hash, verifiability, payload_url_or_null).

    payload shape depends on kind:
      text: {"content": "..."}
      image / file: {"payload_url": "...", "byte_size": N}  (caller uploaded first)
      code / link: {"payload_url": "..."}
    """
    if kind == "text":
        content = str(payload.get("content") or "")
        if not content:
            raise ValueError("文字附件的内容不能为空。")
        return _compute_text_hash(content), "verified", ""

    if kind in ("image", "file"):
        # Bytes already uploaded to our buffer; caller provides the URL we issued.
        # Hash was computed at upload time and stored — we'd look it up via the URL,
        # but for simplicity the upload endpoint passes the pre-computed hash.
        h = str(payload.get("hash") or "")
        if not h:
            raise ValueError("上传附件必须附带内容哈希。")
        return h, "verified", str(payload.get("payload_url") or "")

    if kind == "code":
        url = str(payload.get("payload_url") or "").strip()
        if not url:
            raise ValueError("代码附件需要提供 URL。")
        sha = _extract_git_commit(url)
        if sha:
            return sha, "verified", url
        return "", "unverifiable", url

    if kind == "link":
        url = str(payload.get("payload_url") or "").strip()
        if not url:
            raise ValueError("链接附件需要提供 URL。")
        h = _try_fetch_hash_for_url(url)
        if h:
            return h, "verified", url
        return "", "unverifiable", url

    raise ValueError(f"未知的附件类型: {kind}")


# ---------------------------------------------------------------------------
# Byte buffer for direct-upload attachments
# ---------------------------------------------------------------------------

def save_attachment_bytes(data: bytes, filename: str) -> dict:
    """Write bytes to the delivery buffer, return {hash, path, byte_size, stored_name}.

    Enforces per-file size cap. Global and per-owner quotas are checked by the
    create_delivery caller (needs owner context).
    """
    if len(data) > DELIVERY_MAX_FILE_BYTES:
        raise ValueError(f"单个文件不能超过 {DELIVERY_MAX_FILE_BYTES // 1024 // 1024} MB。")
    h = hashlib.sha256(data).hexdigest()
    ext = pathlib.Path(filename or "").suffix.lower()
    # Refuse obvious executables — v1 has no virus scan, this is a thin guard.
    if ext in (".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".sh", ".ps1"):
        raise ValueError("不支持可执行文件。")
    stored_name = h + ext
    path = _delivery_bytes_dir() / stored_name
    if not path.exists():
        path.write_bytes(data)
    return {
        "hash": h,
        "stored_name": stored_name,
        "byte_size": len(data),
        "path": str(path),
    }


def delete_attachment_bytes(stored_name: str) -> None:
    """Remove bytes from disk by stored_name. Idempotent."""
    if not stored_name:
        return
    path = _delivery_bytes_dir() / stored_name
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _owner_active_bytes(owner_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COALESCE(SUM(byte_size), 0) AS n
            FROM delivery_attachments a
            JOIN deliveries d ON d.id = a.delivery_id
            WHERE d.submitter_owner_id = ?
              AND a.deleted_at IS NULL
              AND a.byte_size IS NOT NULL
        """, (owner_id,)).fetchone()
    return int(row["n"] or 0)


def _global_active_bytes() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(byte_size), 0) AS n FROM delivery_attachments "
            "WHERE deleted_at IS NULL AND byte_size IS NOT NULL"
        ).fetchone()
    return int(row["n"] or 0)


# ---------------------------------------------------------------------------
# Create / withdraw / query deliveries
# ---------------------------------------------------------------------------

def create_delivery(
    order_id: str,
    order_kind: str,
    submitter_owner_id: str,
    receiver_owner_id: str,
    note: str,
    attachments: list[dict],
    confirmation_window: str,
) -> dict:
    """Create a new delivery with its attachments. Atomic.

    `attachments` is a list of {kind, ...kind-specific-payload}. See
    _classify_attachment() for per-kind shape.

    Returns the delivery as a dict with its attachments inline.
    """
    if order_kind not in ("bounty", "deal"):
        raise ValueError("order_kind 必须是 bounty 或 deal。")
    note = (note or "").strip()
    if not note:
        raise ValueError("交付说明不能为空。")
    if not attachments or not isinstance(attachments, list):
        attachments = []
    if len(attachments) > DELIVERY_MAX_ATTACHMENTS:
        raise ValueError(f"一次交付最多 {DELIVERY_MAX_ATTACHMENTS} 个附件。")
    window = confirmation_window if confirmation_window in DELIVERY_CONFIRMATION_WINDOWS else DELIVERY_DEFAULT_WINDOW

    # Pre-flight quota check (only counts already-uploaded bytes; caller should
    # have uploaded before calling this)
    envelope_bytes = sum(int(a.get("byte_size") or 0) for a in attachments)
    if envelope_bytes > DELIVERY_MAX_ENVELOPE_BYTES:
        raise ValueError(f"单次交付总量不能超过 {DELIVERY_MAX_ENVELOPE_BYTES // 1024 // 1024} MB。")
    owner_total = _owner_active_bytes(submitter_owner_id) + envelope_bytes
    if owner_total > 2 * DELIVERY_OWNER_QUOTA_BYTES:
        raise ValueError("你的活跃存储已超出限制,请等旧订单结算后再试。")
    if _global_active_bytes() + envelope_bytes > DELIVERY_GLOBAL_QUOTA_BYTES:
        raise ValueError("沙堆全网暂存已满,请稍后再试。")

    now = utc_now()
    from datetime import datetime, timedelta
    now_dt = datetime.fromisoformat(now)
    expires_at = (now_dt + timedelta(seconds=_window_to_seconds(window))).isoformat()
    delivery_id = new_uuid()

    # Process each attachment to derive hash + verifiability before writing
    processed = []
    for a in attachments:
        kind = str(a.get("kind") or "").strip().lower()
        if kind not in ATTACHMENT_KINDS:
            raise ValueError(f"未知的附件类型: {kind}")
        h, verifiability, payload_url = _classify_attachment(kind, a)
        processed.append({
            "id": new_uuid(),
            "kind": kind,
            "content": str(a.get("content") or "") if kind == "text" else None,
            "payload_url": payload_url or None,
            "filename": str(a.get("filename") or "") or None,
            "byte_size": int(a.get("byte_size") or 0) or None,
            "hash": h or None,
            "verifiability": verifiability,
            "created_at": now,
        })

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO deliveries (
                id, order_id, order_kind, submitter_owner_id, receiver_owner_id,
                note, status, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """, (delivery_id, order_id, order_kind, submitter_owner_id,
              receiver_owner_id, note, now, expires_at))
        for p in processed:
            conn.execute("""
                INSERT INTO delivery_attachments (
                    id, delivery_id, kind, content, payload_url,
                    filename, byte_size, hash, verifiability, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (p["id"], delivery_id, p["kind"], p["content"], p["payload_url"],
                  p["filename"], p["byte_size"], p["hash"], p["verifiability"],
                  p["created_at"]))

    return get_delivery(delivery_id)


def get_delivery(delivery_id: str) -> dict | None:
    with get_conn() as conn:
        d = conn.execute(
            "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
        if d is None:
            return None
        attachments = conn.execute(
            "SELECT * FROM delivery_attachments WHERE delivery_id = ? ORDER BY created_at",
            (delivery_id,),
        ).fetchall()
    result = dict(d)
    result["attachments"] = [dict(a) for a in attachments]
    return result


def list_active_deliveries_for_order(order_id: str, order_kind: str) -> list[dict]:
    """Return all non-withdrawn deliveries for an order (usually 0 or 1 in v1)."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM deliveries
            WHERE order_id = ? AND order_kind = ? AND status = 'active'
            ORDER BY created_at DESC
        """, (order_id, order_kind)).fetchall()
    return [get_delivery(str(r["id"])) for r in rows]


def withdraw_delivery(delivery_id: str, submitter_owner_id: str) -> dict:
    """B withdraws their delivery. Allowed only while A has taken no action.

    Marks delivery 'withdrawn' + deletes attachment bytes immediately.
    Caller must also flip the order status back to 'assigned'.
    """
    now = utc_now()
    with get_conn() as conn:
        d = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        if d is None:
            raise ValueError("交付不存在。")
        if str(d["submitter_owner_id"]) != submitter_owner_id:
            raise ValueError("这个交付不是你发起的,不能撤回。")
        if str(d["status"]) != "active":
            raise ValueError(f"当前状态为 {d['status']},不能撤回。")
        conn.execute(
            "UPDATE deliveries SET status = 'withdrawn', withdrawn_at = ? WHERE id = ?",
            (now, delivery_id),
        )
        atts = conn.execute(
            "SELECT id, hash, filename FROM delivery_attachments WHERE delivery_id = ? AND deleted_at IS NULL",
            (delivery_id,),
        ).fetchall()
        for a in atts:
            # stored_name = hash + ext
            if a["hash"]:
                ext = pathlib.Path(a["filename"] or "").suffix.lower()
                delete_attachment_bytes(str(a["hash"]) + ext)
            conn.execute(
                "UPDATE delivery_attachments SET deleted_at = ? WHERE id = ?",
                (now, a["id"]),
            )
    return get_delivery(delivery_id)


def mark_delivery_settled(delivery_id: str) -> None:
    """Called when the order is settled — delete all bytes for this delivery."""
    now = utc_now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE deliveries SET status = 'settled', settled_at = ? WHERE id = ?",
            (now, delivery_id),
        )
        atts = conn.execute(
            "SELECT id, hash, filename FROM delivery_attachments WHERE delivery_id = ? AND deleted_at IS NULL",
            (delivery_id,),
        ).fetchall()
        for a in atts:
            if a["hash"]:
                ext = pathlib.Path(a["filename"] or "").suffix.lower()
                delete_attachment_bytes(str(a["hash"]) + ext)
            conn.execute(
                "UPDATE delivery_attachments SET deleted_at = ? WHERE id = ?",
                (now, a["id"]),
            )


def purge_expired_deliveries() -> int:
    """Cron-callable: delete bytes for deliveries past their expires_at.

    Does NOT change delivery status (auto-settle is a separate concern at the
    order layer). Returns count of attachments purged.
    """
    now = utc_now()
    count = 0
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT a.id, a.hash, a.filename
            FROM delivery_attachments a
            JOIN deliveries d ON d.id = a.delivery_id
            WHERE a.deleted_at IS NULL
              AND d.expires_at < ?
        """, (now,)).fetchall()
        for a in rows:
            if a["hash"]:
                ext = pathlib.Path(a["filename"] or "").suffix.lower()
                delete_attachment_bytes(str(a["hash"]) + ext)
            conn.execute(
                "UPDATE delivery_attachments SET deleted_at = ? WHERE id = ?",
                (now, a["id"]),
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# Auto-settle cron (v1)
#
# Scan active deliveries past their expires_at. For each, auto-settle the
# underlying order (bounty or deal), which:
#   - Moves escrowed funds to the bidder
#   - Marks the delivery settled, clears cached bytes
#   - Updates order status to 'settled'
# Idempotent: if the delivery is no longer active (withdrawn / already settled),
# skipped silently.
# ---------------------------------------------------------------------------

def auto_settle_expired_deliveries() -> dict:
    """Run once per poll interval (e.g., every 30 minutes from a cron / scheduler).

    Returns {settled: n, skipped: n, errors: [{delivery_id, reason}]} for observability.
    """
    now = utc_now()
    report = {"settled": 0, "skipped": 0, "errors": []}

    with get_conn() as conn:
        expired_rows = conn.execute("""
            SELECT id, order_id, order_kind, submitter_owner_id, receiver_owner_id
            FROM deliveries
            WHERE status = 'active'
              AND expires_at < ?
        """, (now,)).fetchall()

    for d in expired_rows:
        delivery_id = str(d["id"])
        order_id = str(d["order_id"])
        order_kind = str(d["order_kind"])
        try:
            if order_kind == "bounty":
                from server.store import confirm_bounty_settlement, get_conn as get_conn_s
                with get_conn_s() as c:
                    b = c.execute("SELECT status, poster_lobster_id FROM bounties WHERE id = ?", (order_id,)).fetchone()
                    if b is None:
                        report["errors"].append({"delivery_id": delivery_id, "reason": "bounty missing"})
                        continue
                    if b["status"] != "fulfilled":
                        # Already moved on (settled/cancelled). Just clean the delivery row.
                        mark_delivery_settled(delivery_id)
                        report["skipped"] += 1
                        continue
                    poster_claw = c.execute(
                        "SELECT claw_id FROM lobsters WHERE id = ?", (b["poster_lobster_id"],)
                    ).fetchone()
                if poster_claw is None:
                    report["errors"].append({"delivery_id": delivery_id, "reason": "poster lobster missing"})
                    continue
                confirm_bounty_settlement(order_id, str(poster_claw["claw_id"]))
                report["settled"] += 1

            elif order_kind == "deal":
                with get_conn() as c:
                    deal = c.execute("SELECT status, caller_lobster_id FROM deals WHERE id = ?", (order_id,)).fetchone()
                    if deal is None:
                        report["errors"].append({"delivery_id": delivery_id, "reason": "deal missing"})
                        continue
                    if deal["status"] != "fulfilled":
                        mark_delivery_settled(delivery_id)
                        report["skipped"] += 1
                        continue
                    caller_claw = c.execute(
                        "SELECT claw_id FROM lobsters WHERE id = ?", (deal["caller_lobster_id"],)
                    ).fetchone()
                if caller_claw is None:
                    report["errors"].append({"delivery_id": delivery_id, "reason": "caller lobster missing"})
                    continue
                from .deals import confirm_deal
                confirm_deal(order_id, str(caller_claw["claw_id"]))
                report["settled"] += 1
            else:
                report["errors"].append({"delivery_id": delivery_id, "reason": f"unknown order_kind {order_kind}"})
        except Exception as exc:
            report["errors"].append({"delivery_id": delivery_id, "reason": str(exc)})

    return report
