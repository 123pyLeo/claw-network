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
        # Structured BP fields (Phase 1: all plain text, no files)
        listing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bp_listings)").fetchall()}
        for col, ddl in [
            ("problem", "ALTER TABLE bp_listings ADD COLUMN problem TEXT DEFAULT ''"),
            ("solution", "ALTER TABLE bp_listings ADD COLUMN solution TEXT DEFAULT ''"),
            ("team_intro", "ALTER TABLE bp_listings ADD COLUMN team_intro TEXT DEFAULT ''"),
            ("traction", "ALTER TABLE bp_listings ADD COLUMN traction TEXT DEFAULT ''"),
            ("business_model", "ALTER TABLE bp_listings ADD COLUMN business_model TEXT DEFAULT ''"),
            ("ask_note", "ALTER TABLE bp_listings ADD COLUMN ask_note TEXT DEFAULT ''"),
        ]:
            if col not in listing_cols:
                conn.execute(ddl)

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
        # Intent extra columns
        intent_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bp_intents)").fetchall()}
        for col, ddl in [
            ("direction", "ALTER TABLE bp_intents ADD COLUMN direction TEXT DEFAULT 'inbound'"),
            ("investor_meet_at", "ALTER TABLE bp_intents ADD COLUMN investor_meet_at TEXT"),
            ("founder_meet_at", "ALTER TABLE bp_intents ADD COLUMN founder_meet_at TEXT"),
            ("meeting_unlocked_at", "ALTER TABLE bp_intents ADD COLUMN meeting_unlocked_at TEXT"),
            ("review_note", "ALTER TABLE bp_intents ADD COLUMN review_note TEXT DEFAULT ''"),
        ]:
            if col not in intent_cols:
                conn.execute(ddl)

        # Invite codes — Phase 1 primary path for investor/founder role grant
        conn.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                code            TEXT PRIMARY KEY,
                role            TEXT NOT NULL,
                role_verified   INTEGER NOT NULL DEFAULT 1,
                generated_by    TEXT,
                note            TEXT DEFAULT '',
                created_at      TEXT NOT NULL,
                expires_at      TEXT NOT NULL,
                used_at         TEXT,
                used_by_lobster_id TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invite_codes_used ON invite_codes(used_at) WHERE used_at IS NULL")

        # Role applications — reuses the existing role_applications table
        # (created by features/role_verification). We just ensure extra
        # columns exist for our use case (intro_text, reviewed_by).
        ra_cols = {row["name"] for row in conn.execute("PRAGMA table_info(role_applications)").fetchall()}
        if ra_cols:
            # Table exists (from role_verification module) — extend it.
            if "intro_text" not in ra_cols:
                conn.execute("ALTER TABLE role_applications ADD COLUMN intro_text TEXT DEFAULT ''")
            if "reviewed_by" not in ra_cols:
                conn.execute("ALTER TABLE role_applications ADD COLUMN reviewed_by TEXT")
        else:
            # First time — create our own shape.
            conn.execute("""
                CREATE TABLE role_applications (
                    id              TEXT PRIMARY KEY,
                    lobster_id      TEXT NOT NULL,
                    claw_id         TEXT,
                    role            TEXT NOT NULL,
                    org_name        TEXT DEFAULT '',
                    real_name       TEXT DEFAULT '',
                    supporting_url  TEXT,
                    intro_text      TEXT DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'pending',
                    reviewer_note   TEXT DEFAULT '',
                    reviewed_by     TEXT,
                    created_at      TEXT NOT NULL,
                    reviewed_at     TEXT
                )
            """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_role_apps_status ON role_applications(status, created_at)")

        # VC email domains whitelist (Phase 2, schema ready now)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vc_email_domains (
                domain          TEXT PRIMARY KEY,
                org_name        TEXT NOT NULL,
                added_by        TEXT,
                added_at        TEXT NOT NULL,
                note            TEXT DEFAULT ''
            )
        """)

        # Extend lobsters: role / role_verified (if missing)
        lobster_cols = {row["name"] for row in conn.execute("PRAGMA table_info(lobsters)").fetchall()}
        if "role" not in lobster_cols:
            conn.execute("ALTER TABLE lobsters ADD COLUMN role TEXT")
        if "role_verified" not in lobster_cols:
            conn.execute("ALTER TABLE lobsters ADD COLUMN role_verified INTEGER NOT NULL DEFAULT 0")
        if "role_verification_method" not in lobster_cols:
            conn.execute("ALTER TABLE lobsters ADD COLUMN role_verification_method TEXT")
        if "org_name" not in lobster_cols:
            conn.execute("ALTER TABLE lobsters ADD COLUMN org_name TEXT")

        # Extend owners: primary_contact / contact_type / secondary_contacts
        owner_cols = {row["name"] for row in conn.execute("PRAGMA table_info(owners)").fetchall()}
        if "primary_contact" not in owner_cols:
            conn.execute("ALTER TABLE owners ADD COLUMN primary_contact TEXT")
        if "primary_contact_type" not in owner_cols:
            conn.execute("ALTER TABLE owners ADD COLUMN primary_contact_type TEXT")
        if "secondary_contacts" not in owner_cols:
            conn.execute("ALTER TABLE owners ADD COLUMN secondary_contacts TEXT")

        # ---- investor preference cards ----
        # Filled via guided Q&A right after Phase-1 auth passes. Drives
        # local listing pre-filter (sidecar) and is shown to founders when
        # they receive an intent ("is this investor a real fit?").
        # JSON-encoded array fields stored as TEXT; Python side normalizes.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS investor_profiles (
                claw_id              TEXT PRIMARY KEY,
                org_name             TEXT NOT NULL DEFAULT '',
                self_intro           TEXT NOT NULL DEFAULT '',
                sectors              TEXT NOT NULL DEFAULT '[]',
                stages               TEXT NOT NULL DEFAULT '[]',
                ticket_min           INTEGER,
                ticket_max           INTEGER,
                ticket_currency      TEXT NOT NULL DEFAULT 'CNY',
                portfolio_examples   TEXT NOT NULL DEFAULT '',
                decision_cycle       TEXT NOT NULL DEFAULT '',
                value_add            TEXT NOT NULL DEFAULT '',
                team_preference      TEXT NOT NULL DEFAULT '',
                redlines             TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            )
            """
        )

        # a2a_events: observability-only log of agent-to-agent BP interactions.
        # Deliberately separate from invocations (the economic ledger) so
        # statistics queries against money flows don't have to filter these out.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS a2a_events (
                id              TEXT PRIMARY KEY,
                event_type      TEXT NOT NULL,
                from_owner_id   TEXT,
                to_owner_id     TEXT,
                source_type     TEXT,
                source_id       TEXT,
                payload         TEXT,
                created_at      TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_a2a_events_created ON a2a_events(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_a2a_events_source "
            "ON a2a_events(source_type, source_id)"
        )

        # ---- A2A autonomous matchmaking sessions ----
        # A row per accepted bp_intent that's eligible for AI-driven first-
        # contact dialogue. Server orchestrates state; LLM calls happen in
        # each user's plugin (their LLM credentials, their privacy).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bp_a2a_sessions (
                id                  TEXT PRIMARY KEY,
                intent_id           TEXT NOT NULL UNIQUE,
                listing_id          TEXT NOT NULL,
                investor_claw_id    TEXT NOT NULL,
                founder_claw_id     TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'pending',
                turn_count          INTEGER NOT NULL DEFAULT 0,
                next_speaker        TEXT,
                investor_want_end   INTEGER,
                founder_want_end    INTEGER,
                summary             TEXT DEFAULT '',
                last_turn_at        TEXT,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                concluded_at        TEXT
            )
            """
        )
        # status: pending | running | awaiting_vote | concluded_match
        #         | concluded_pass | stalled | failed
        # next_speaker: 'investor' | 'founder' (whose LLM should generate next msg)
        # *_want_end: NULL = haven't voted yet, 1 = wants to end, 0 = wants to continue
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_bp_a2a_sessions_status "
            "ON bp_a2a_sessions(status, last_turn_at)"
        )

        # Per-lobster A2A mode preference. Default 'manual' = AI drafts a
        # reply, human approves in IM before send. 'auto' = AI runs the
        # whole conversation autonomously. 'off' = no A2A at all (human
        # handles every inbound message themselves).
        lob_cols = {row["name"] for row in conn.execute("PRAGMA table_info(lobsters)").fetchall()}
        if "bp_a2a_mode" not in lob_cols:
            conn.execute("ALTER TABLE lobsters ADD COLUMN bp_a2a_mode TEXT NOT NULL DEFAULT 'manual'")

        # retry_count: number of times the current turn has been re-pushed
        # by the driver. Resets when the turn actually advances. Caps at
        # RETRY_LIMIT, after which session is marked stalled.
        a2a_cols = {row["name"] for row in conn.execute("PRAGMA table_info(bp_a2a_sessions)").fetchall()}
        if "retry_count" not in a2a_cols:
            conn.execute("ALTER TABLE bp_a2a_sessions ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LISTING_SELECT = """
    SELECT
        bl.id, bl.status, bl.access_policy,
        bl.project_name, bl.sector, bl.stage,
        bl.funding_ask, bl.currency, bl.one_liner, bl.team_size,
        bl.problem, bl.solution, bl.team_intro, bl.traction,
        bl.business_model, bl.ask_note,
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
        COALESCE(bi.review_note, '') AS review_note,
        bi.created_at, bi.reviewed_at,
        bl.project_name,
        l.claw_id AS investor_claw_id,
        l.name AS investor_name,
        l.org_name AS investor_org,
        -- Queue position: 1-indexed rank among pending intents on the same
        -- listing, oldest first. NULL once the intent is reviewed/accepted/
        -- rejected (queue concept no longer applies).
        CASE WHEN bi.status = 'pending' THEN (
            SELECT COUNT(*) + 1 FROM bp_intents bi2
             WHERE bi2.listing_id = bi.listing_id
               AND bi2.status = 'pending'
               AND bi2.created_at < bi.created_at
        ) ELSE NULL END AS queue_position,
        CASE WHEN bi.status = 'pending' THEN (
            SELECT COUNT(*) FROM bp_intents bi3
             WHERE bi3.listing_id = bi.listing_id
               AND bi3.status = 'pending'
        ) ELSE NULL END AS queue_total
    FROM bp_intents bi
    JOIN lobsters l ON l.id = bi.investor_lobster_id
    JOIN bp_listings bl ON bl.id = bi.listing_id
"""


def _has_verified_phone(lobster) -> bool:
    """True if the lobster itself has a verified phone, OR its owner does.

    Old sidecar flow verified phone per-lobster; new flow (sandpile.io
    console) verifies per-owner. We accept either.
    """
    if lobster["verified_phone"]:
        return True
    owner_id = str(lobster["owner_id"] or "")
    if not owner_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT auth_phone FROM owners WHERE id = ?", (owner_id,)
        ).fetchone()
    return bool(row and row["auth_phone"])


def get_my_bp_status(claw_id: str) -> dict:
    """Return the caller's BP-matching status: role, verification, phone.

    Used by the `bp_my_status` tool so the LLM can ground answers in DB
    truth instead of hallucinating from stale conversation context.
    """
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")
    role = str(lobster["role"] or "").strip() or None
    role_verified = bool(lobster["role_verified"])
    phone_verified = _has_verified_phone(lobster)
    method = None
    try:
        method = str(lobster["role_verification_method"] or "").strip() or None
    except (KeyError, IndexError):
        pass
    return {
        "claw_id": claw_id.strip().upper(),
        "role": role,
        "role_verified": role_verified,
        "role_verification_method": method,
        "phone_verified": phone_verified,
        "can_publish_bp": phone_verified and role in ("founder", "both"),
        "can_view_bp_detail": phone_verified and role in ("investor", "both") and role_verified,
    }


def _require_founder(lobster) -> None:
    """Check that lobster is a verified founder (or has phone verification)."""
    if not _has_verified_phone(lobster):
        raise ValueError("请先完成手机验证。")
    role = str(lobster["role"] or "").strip()
    if role not in ("founder", "both"):
        raise ValueError("请先申请创业者角色认证。")


def _require_verified_investor(lobster) -> None:
    """Check that lobster is a verified investor."""
    if not _has_verified_phone(lobster):
        raise ValueError("请先完成手机验证。")
    role = str(lobster["role"] or "").strip()
    if role not in ("investor", "both"):
        raise ValueError("请先申请投资人角色认证。")
    if not lobster["role_verified"]:
        raise ValueError("你的投资人身份尚未通过审核。")


def _log_a2a_event(
    event_type: str,
    from_owner_id: str,
    to_owner_id: str,
    source_type: str,
    source_id: str,
    payload: dict | None = None,
) -> str | None:
    """Record an agent-to-agent BP interaction into the a2a_events table.

    Observability-only — deliberately separate from invocations (the economic
    ledger). Dispatched to a background thread so the HTTP response doesn't
    wait for DB write contention. Best-effort; logging failure does not
    affect business flow.
    """
    if not from_owner_id or not to_owner_id:
        return None
    if from_owner_id == to_owner_id:
        return None

    import json as _json
    payload_json = _json.dumps(payload or {}, ensure_ascii=False)

    def _write() -> None:
        try:
            with get_conn() as conn:
                conn.execute(
                    """INSERT INTO a2a_events
                       (id, event_type, from_owner_id, to_owner_id,
                        source_type, source_id, payload, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (new_uuid(), event_type, from_owner_id, to_owner_id,
                     source_type, source_id, payload_json, utc_now()),
                )
        except Exception:
            # Best-effort observability — swallow
            pass

    import threading
    threading.Thread(target=_write, daemon=True).start()
    return "scheduled"


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
    problem: str = "",
    solution: str = "",
    team_intro: str = "",
    traction: str = "",
    business_model: str = "",
    ask_note: str = "",
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
                problem, solution, team_intro, traction, business_model, ask_note,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id, str(lobster["id"]), access_policy,
                project_name.strip(), sector.strip(), stage.strip(),
                funding_ask, currency, one_liner.strip(), team_size,
                problem.strip(), solution.strip(), team_intro.strip(),
                traction.strip(), business_model.strip(), ask_note.strip(),
                now, now, expires_at,
            ),
        )
        row = conn.execute(f"{_LISTING_SELECT} WHERE bl.id = ?", (listing_id,)).fetchone()

    return dict(row)


def search_listings(
    sector: str | None = None,
    stage: str | None = None,
    limit: int = 50,
    before_created_at: str | None = None,
) -> list[dict]:
    """Search active BP listings, ordered newest-first.

    Cursor pagination: callers fetching subsequent pages pass the
    `created_at` of the last row they received as `before_created_at`;
    this function then returns rows strictly older than that timestamp.
    Omit on the first call to get the latest page.
    """
    now = utc_now()
    sql = f"{_LISTING_SELECT} WHERE bl.status = 'active' AND (bl.expires_at IS NULL OR bl.expires_at > ?)"
    params: list = [now]

    if sector:
        sql += " AND bl.sector LIKE ?"
        params.append(f"%{sector.strip()}%")
    if stage:
        sql += " AND bl.stage = ?"
        params.append(stage.strip())
    if before_created_at:
        sql += " AND bl.created_at < ?"
        params.append(before_created_at)

    sql += " ORDER BY bl.created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 100)))

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _resolve_listing_id(listing_id: str) -> str:
    """Same prefix-resolution trick as _resolve_intent_id, for listing IDs."""
    raw = (listing_id or "").strip()
    if not raw:
        raise ValueError("BP 不存在。")
    if len(raw) >= 36:
        return raw
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM bp_listings WHERE id LIKE ? LIMIT 2",
            (raw + "%",),
        ).fetchall()
    if not rows:
        raise ValueError("BP 不存在。")
    if len(rows) > 1:
        raise ValueError("ID 前缀匹配到多条，请提供更完整的 ID。")
    return str(rows[0]["id"])


def get_listing(listing_id: str) -> dict:
    """Get a single BP listing."""
    listing_id = _resolve_listing_id(listing_id)
    with get_conn() as conn:
        row = conn.execute(f"{_LISTING_SELECT} WHERE bl.id = ?", (listing_id,)).fetchone()
    if row is None:
        raise ValueError("BP 不存在。")
    return dict(row)


_UPDATABLE_CONTENT_COLS = (
    "project_name", "one_liner", "sector", "stage",
    "funding_ask", "currency", "team_size",
    "problem", "solution", "team_intro", "traction", "business_model", "ask_note",
)

# Fields whose change should nudge already-pending investors to reconfirm.
_KEY_CONTENT_COLS = ("one_liner", "funding_ask", "stage")


def update_listing(
    listing_id: str,
    claw_id: str,
    access_policy: str | None = None,
    status: str | None = None,
    content_updates: dict | None = None,
) -> dict:
    """Update a BP listing (owner only).

    If a key content field (one_liner / funding_ask / stage) is changed,
    the returned dict has `_notify_investors`: a list of claw_ids of
    investors whose pending intents should be nudged.
    """
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")

    filtered_content = {
        k: (v.strip() if isinstance(v, str) else v)
        for k, v in (content_updates or {}).items()
        if k in _UPDATABLE_CONTENT_COLS and v is not None
    }
    key_changed = any(k in _KEY_CONTENT_COLS for k in filtered_content)

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
        for col, val in filtered_content.items():
            updates.append(f"{col} = ?")
            params.append(val)

        if updates:
            updates.append("updated_at = ?")
            params.append(utc_now())
            params.append(listing_id)
            conn.execute(f"UPDATE bp_listings SET {', '.join(updates)} WHERE id = ?", params)

        result = conn.execute(f"{_LISTING_SELECT} WHERE bl.id = ?", (listing_id,)).fetchone()

        notify_investors: list[str] = []
        if key_changed:
            pending = conn.execute(
                """SELECT l.claw_id FROM bp_intents bi
                   JOIN lobsters l ON l.id = bi.investor_lobster_id
                   WHERE bi.listing_id = ? AND bi.status = 'pending'""",
                (listing_id,),
            ).fetchall()
            notify_investors = [str(r["claw_id"]) for r in pending]

    out = dict(result)
    out["_notify_investors"] = notify_investors
    return out


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

    # Log as zero-amount invocation for network observability
    with get_conn() as conn:
        fr = conn.execute(
            "SELECT owner_id FROM lobsters WHERE claw_id = ?",
            (listing["founder_claw_id"],),
        ).fetchone()
    inv_owner = str(investor["owner_id"] or "")
    founder_owner = str(fr["owner_id"] or "") if fr else ""
    _log_a2a_event(
        event_type="bp_intent_created",
        from_owner_id=inv_owner,
        to_owner_id=founder_owner,
        source_type="bp_intent",
        source_id=intent_id,
        payload={"access_policy": listing["access_policy"]},
    )
    if initial_status == "auto_accepted":
        _log_a2a_event(
            event_type="bp_intent_auto_approved",
            from_owner_id=founder_owner,
            to_owner_id=inv_owner,
            source_type="bp_intent",
            source_id=intent_id,
        )

    with get_conn() as conn:
        row = conn.execute(f"{_INTENT_SELECT} WHERE bi.id = ?", (intent_id,)).fetchone()

    return dict(row)


def list_my_intents(investor_claw_id: str) -> list[dict]:
    """Return all intents created by this investor, newest first.

    The "many-to-one" view: an investor who has expressed interest in
    several BPs sees their whole pipeline + per-BP queue position.
    """
    investor = get_lobster_by_claw_id(investor_claw_id)
    if investor is None:
        raise ValueError("Lobster not found.")
    with get_conn() as conn:
        rows = conn.execute(
            f"{_INTENT_SELECT} WHERE bi.investor_lobster_id = ? "
            f"ORDER BY bi.created_at DESC",
            (str(investor["id"]),),
        ).fetchall()
    return [dict(r) for r in rows]


def list_pending_intents_for_listing(listing_id: str) -> list[dict]:
    """All still-pending intents on a listing, oldest first.

    Used after a review to push fresh queue positions to everyone still
    in line. Returns dicts with the same fields as _INTENT_SELECT, so the
    queue_position / queue_total values reflect post-review reality.
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"{_INTENT_SELECT} WHERE bi.listing_id = ? AND bi.status = 'pending' "
            f"ORDER BY bi.created_at ASC",
            (listing_id,),
        ).fetchall()
    return [dict(r) for r in rows]


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
            (listing["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


def _resolve_intent_id(intent_id: str) -> str:
    """Accept either a full UUID or an 8+ char hex prefix. Returns full id.

    The CLI output shows 8-char prefixes (e.g. `ce166999`) for readability,
    so founders naturally copy the prefix back when approving. Do prefix
    resolution here so every review/meeting call works.
    """
    raw = (intent_id or "").strip()
    if not raw:
        raise ValueError("兴趣记录不存在。")
    if len(raw) >= 36:
        return raw
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM bp_intents WHERE id LIKE ? LIMIT 2",
            (raw + "%",),
        ).fetchall()
    if not rows:
        raise ValueError("兴趣记录不存在。")
    if len(rows) > 1:
        raise ValueError("ID 前缀匹配到多条，请提供更完整的 ID。")
    return str(rows[0]["id"])


def review_intent(intent_id: str, claw_id: str, decision: str, review_note: str = "") -> dict:
    """Founder reviews an investor's interest. Returns updated intent.

    `review_note` is an optional free-text reason carried through to the
    investor on both accept and reject. Useful for rejection feedback
    (e.g. "we focus on B2C, not B2B SaaS — try us next round")
    so the investor isn't left guessing why.
    """
    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise ValueError("Lobster not found.")

    intent_id = _resolve_intent_id(intent_id)
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
            "UPDATE bp_intents SET status = ?, reviewed_at = ?, review_note = ? WHERE id = ?",
            (decision, now, review_note.strip(), intent_id),
        )
        # Capture investor claw_id regardless of decision — both accept and
        # reject deserve observability + downstream notification.
        investor_claw_id = intent["investor_claw_id"]

    # Auto-friend on accept (skip for reject)
    if decision == "accepted":
        investor = get_lobster_by_claw_id(investor_claw_id)
        if investor:
            ensure_friendship(str(lobster["id"]), str(investor["id"]))
        # Try to start an A2A session if both sides opted into auto mode.
        # The actual WS push to kick off turn 1 is done by the route layer
        # (which has access to the async dispatcher).
        try:
            from . import a2a_engine
            a2a_engine.start_session(intent_id)
        except Exception:
            pass  # best-effort; A2A is opt-in, never block accept on its failure

    # Log the review as an a2a_events observability record (both accept and reject).
    investor_row = get_lobster_by_claw_id(investor_claw_id)
    if investor_row:
        _log_a2a_event(
            event_type=f"bp_intent_{decision}",
            from_owner_id=str(lobster["owner_id"] or ""),
            to_owner_id=str(investor_row["owner_id"] or ""),
            source_type="bp_intent",
            source_id=intent_id,
            payload={"has_note": bool(review_note.strip())},
        )

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


# ---------------------------------------------------------------------------
# Invite codes (Phase 1 primary path for role grant)
# ---------------------------------------------------------------------------

import secrets as _secrets
import string as _string

_INVITE_CHARSET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/l


def _generate_invite_code() -> str:
    """Generate a human-friendly code like SANDPILE-ABCD-1234."""
    left = "".join(_secrets.choice(_INVITE_CHARSET) for _ in range(4))
    right = "".join(_secrets.choice(_INVITE_CHARSET) for _ in range(4))
    return f"SANDPILE-{left}-{right}"


def create_invite_code(
    role: str,
    *,
    role_verified: bool = True,
    generated_by: str = "",
    note: str = "",
    valid_days: int = 30,
) -> dict:
    """Generate a new single-use invite code bound to a role."""
    if role not in ("investor", "founder"):
        raise ValueError("role must be 'investor' or 'founder'.")
    now = utc_now()
    expires_at = (datetime.fromisoformat(now) + timedelta(days=valid_days)).isoformat()
    # retry on collision (extremely unlikely)
    for _ in range(5):
        code = _generate_invite_code()
        try:
            with get_conn() as conn:
                conn.execute(
                    """INSERT INTO invite_codes
                       (code, role, role_verified, generated_by, note, created_at, expires_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (code, role, 1 if role_verified else 0, generated_by, note, now, expires_at),
                )
            return {
                "code": code, "role": role, "role_verified": role_verified,
                "note": note, "created_at": now, "expires_at": expires_at,
            }
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("Failed to generate unique invite code after retries.")


class InviteCodeError(ValueError):
    pass


def redeem_invite_code(code: str, lobster_id: str) -> dict:
    """Redeem a single-use invite code on behalf of a lobster.

    Marks the code used atomically, grants the role to the lobster,
    sets role_verified based on the code's policy.
    """
    code = code.strip().upper()
    now = utc_now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM invite_codes WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            raise InviteCodeError("邀请码无效。")
        if row["used_at"]:
            raise InviteCodeError("邀请码已被使用。")
        if row["expires_at"] < now:
            raise InviteCodeError("邀请码已过期。")

        # Atomic "mark used" — only succeeds if still unused
        cursor = conn.execute(
            """UPDATE invite_codes
               SET used_at = ?, used_by_lobster_id = ?
               WHERE code = ? AND used_at IS NULL""",
            (now, lobster_id, code),
        )
        if cursor.rowcount == 0:
            raise InviteCodeError("邀请码已被使用。")

        # Grant role to lobster
        role = row["role"]
        role_verified = int(row["role_verified"] or 0)
        conn.execute(
            """UPDATE lobsters
               SET role = ?, role_verified = ?, role_verification_method = 'invite_code'
               WHERE id = ?""",
            (role, role_verified, lobster_id),
        )

    return {
        "code": code,
        "role": role,
        "role_verified": bool(role_verified),
        "granted_at": now,
    }


def list_invite_codes(status: str | None = None, limit: int = 100) -> list[dict]:
    """List invite codes for admin view. status: 'unused' / 'used' / None (all)."""
    sql = "SELECT * FROM invite_codes"
    params: list = []
    if status == "unused":
        sql += " WHERE used_at IS NULL AND expires_at > ?"
        params.append(utc_now())
    elif status == "used":
        sql += " WHERE used_at IS NOT NULL"
    elif status == "expired":
        sql += " WHERE used_at IS NULL AND expires_at <= ?"
        params.append(utc_now())
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Role applications (founder light auth + investor manual review)
# ---------------------------------------------------------------------------

def submit_role_application(
    lobster_id: str,
    requested_role: str,
    intro_text: str,
    org_name: str = "",
) -> dict:
    """Submit a role application. Founder goes auto-approved, investor goes pending."""
    if requested_role not in ("investor", "founder"):
        raise ValueError("requested_role must be 'investor' or 'founder'.")
    if not intro_text.strip():
        raise ValueError("自我介绍不能为空。")

    app_id = new_uuid()
    now = utc_now()
    auto_approve = (requested_role == "founder")
    status = "approved" if auto_approve else "pending"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT claw_id FROM lobsters WHERE id = ?", (lobster_id,)
        ).fetchone()
        claw_id = row["claw_id"] if row else ""

        conn.execute(
            """INSERT INTO role_applications
               (id, lobster_id, claw_id, role, org_name, real_name, intro_text, status,
                created_at, reviewed_at, reviewed_by, reviewer_note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (app_id, lobster_id, claw_id, requested_role, org_name, "",
             intro_text.strip(), status, now,
             now if auto_approve else None,
             "system" if auto_approve else None,
             "轻认证自动通过" if auto_approve else ""),
        )
        if auto_approve:
            conn.execute(
                """UPDATE lobsters
                   SET role = ?, role_verified = 0, role_verification_method = 'self_declared',
                       org_name = COALESCE(NULLIF(?, ''), org_name)
                   WHERE id = ?""",
                (requested_role, org_name, lobster_id),
            )

    return {
        "id": app_id,
        "lobster_id": lobster_id,
        "requested_role": requested_role,
        "status": status,
        "created_at": now,
    }


def review_role_application(
    app_id: str,
    decision: str,
    reviewer: str,
    review_note: str = "",
) -> dict:
    """Admin reviews a pending application. decision = 'approved' | 'rejected'."""
    if decision not in ("approved", "rejected"):
        raise ValueError("decision must be 'approved' or 'rejected'.")
    now = utc_now()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM role_applications WHERE id = ?", (app_id,)
        ).fetchone()
        if row is None:
            raise ValueError("申请不存在。")
        if row["status"] != "pending":
            raise ValueError(f"申请已处理(状态:{row['status']})。")

        conn.execute(
            """UPDATE role_applications
               SET status = ?, reviewed_at = ?, reviewed_by = ?, reviewer_note = ?
               WHERE id = ?""",
            (decision, now, reviewer, review_note, app_id),
        )

        if decision == "approved":
            # Investor: manual review grants verified=true
            conn.execute(
                """UPDATE lobsters
                   SET role = ?, role_verified = 1, role_verification_method = 'manual_review',
                       org_name = COALESCE(NULLIF(?, ''), org_name)
                   WHERE id = ?""",
                (row["role"], row["org_name"], row["lobster_id"]),
            )

    return {"id": app_id, "status": decision, "reviewed_at": now}


def list_pending_applications(limit: int = 50) -> list[dict]:
    """Admin queue of pending role applications."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ra.*, l.name AS lobster_name
               FROM role_applications ra
               JOIN lobsters l ON l.id = ra.lobster_id
               WHERE ra.status = 'pending'
               ORDER BY ra.created_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_role_application(app_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM role_applications WHERE id = ?", (app_id,)
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Owner contact info (for State 4 unlock)
# ---------------------------------------------------------------------------

def set_owner_contact(
    owner_id: str,
    primary_contact: str,
    primary_contact_type: str,
    secondary_contacts: dict | None = None,
) -> None:
    """Set / update an owner's contact info."""
    import json as _json
    if primary_contact_type not in ("wechat", "phone"):
        raise ValueError("primary_contact_type must be 'wechat' or 'phone'.")
    if not primary_contact.strip():
        raise ValueError("primary_contact 不能为空。")
    sec = _json.dumps(secondary_contacts or {}, ensure_ascii=False)
    with get_conn() as conn:
        conn.execute(
            """UPDATE owners
               SET primary_contact = ?, primary_contact_type = ?, secondary_contacts = ?
               WHERE id = ?""",
            (primary_contact.strip(), primary_contact_type, sec, owner_id),
        )


def get_owner_contact(owner_id: str) -> dict:
    """Read an owner's contact info. Returns all fields (caller must enforce access)."""
    import json as _json
    with get_conn() as conn:
        row = conn.execute(
            """SELECT primary_contact, primary_contact_type, secondary_contacts
               FROM owners WHERE id = ?""",
            (owner_id,),
        ).fetchone()
    if row is None:
        return {"primary_contact": None, "primary_contact_type": None, "secondary_contacts": {}}
    sec_raw = row["secondary_contacts"]
    try:
        sec = _json.loads(sec_raw) if sec_raw else {}
    except (ValueError, TypeError):
        sec = {}
    return {
        "primary_contact": row["primary_contact"],
        "primary_contact_type": row["primary_contact_type"],
        "secondary_contacts": sec,
    }


# ---------------------------------------------------------------------------
# Investor preference cards
# ---------------------------------------------------------------------------

INVESTOR_PROFILE_CORE_FIELDS = ("org_name", "self_intro", "sectors", "stages", "ticket_min")


def _normalize_str_list(value) -> list[str]:
    """Accept list / comma-separated str / None — always return clean list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        # split on common separators
        parts = []
        for chunk in value.replace("，", ",").replace("、", ",").replace(";", ",").replace("/", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
        return parts
    return []


def set_investor_profile(claw_id: str, **fields) -> dict:
    """Upsert an investor profile. Only fields explicitly passed are updated;
    omitted ones keep their prior value (so the guided Q&A can drip-fill).

    Returns the post-write profile dict.
    """
    import json as _json
    normalized_id = str(claw_id).strip().upper()
    if not normalized_id:
        raise ValueError("claw_id is required")

    sectors = fields.get("sectors")
    stages = fields.get("stages")
    if sectors is not None:
        fields["sectors"] = _json.dumps(_normalize_str_list(sectors), ensure_ascii=False)
    if stages is not None:
        fields["stages"] = _json.dumps(_normalize_str_list(stages), ensure_ascii=False)

    ticket_min = fields.get("ticket_min")
    ticket_max = fields.get("ticket_max")
    if ticket_min is not None:
        try:
            fields["ticket_min"] = int(ticket_min)
        except (TypeError, ValueError):
            raise ValueError("ticket_min 必须是数字")
    if ticket_max is not None:
        try:
            fields["ticket_max"] = int(ticket_max)
        except (TypeError, ValueError):
            raise ValueError("ticket_max 必须是数字")

    now = utc_now()
    allowed = {
        "org_name", "self_intro", "sectors", "stages",
        "ticket_min", "ticket_max", "ticket_currency",
        "portfolio_examples", "decision_cycle", "value_add",
        "team_preference", "redlines",
    }
    update_fields = {k: v for k, v in fields.items() if k in allowed and v is not None}

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT claw_id FROM investor_profiles WHERE claw_id = ?",
            (normalized_id,),
        ).fetchone()
        if existing is None:
            cols = ["claw_id", "created_at", "updated_at"] + list(update_fields.keys())
            placeholders = ",".join(["?"] * len(cols))
            values = [normalized_id, now, now] + list(update_fields.values())
            conn.execute(
                f"INSERT INTO investor_profiles ({','.join(cols)}) VALUES ({placeholders})",
                values,
            )
        elif update_fields:
            sets = ", ".join(f"{k} = ?" for k in update_fields)
            values = list(update_fields.values()) + [now, normalized_id]
            conn.execute(
                f"UPDATE investor_profiles SET {sets}, updated_at = ? WHERE claw_id = ?",
                values,
            )
    return get_investor_profile(normalized_id)


def get_investor_profile(claw_id: str) -> dict:
    """Read profile. Returns dict with empty defaults if no row exists."""
    import json as _json
    normalized_id = str(claw_id).strip().upper()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM investor_profiles WHERE claw_id = ?",
            (normalized_id,),
        ).fetchone()
    if row is None:
        return {
            "claw_id": normalized_id,
            "exists": False,
            "org_name": "", "self_intro": "",
            "sectors": [], "stages": [],
            "ticket_min": None, "ticket_max": None, "ticket_currency": "CNY",
            "portfolio_examples": "", "decision_cycle": "", "value_add": "",
            "team_preference": "", "redlines": "",
            "core_complete": False,
        }
    out = dict(row)
    out["exists"] = True
    for k in ("sectors", "stages"):
        try:
            out[k] = _json.loads(out.get(k) or "[]")
        except (ValueError, TypeError):
            out[k] = []
    out["core_complete"] = (
        bool(out.get("org_name"))
        and bool(out.get("self_intro"))
        and bool(out.get("sectors"))
        and bool(out.get("stages"))
        and out.get("ticket_min") is not None
    )
    return out


# ---------------------------------------------------------------------------
# State 4: meeting request / response / contact unlock
# ---------------------------------------------------------------------------

MEETING_EXPIRE_DAYS = 7       # hard expire after 7 days of no resolution
MEETING_DECISION_DAYS = 3     # soft deadline — agent nudges by day 3


def request_meeting(intent_id: str, side: str) -> dict:
    """Mark one side's `meeting wanted` flag on an intent.

    side = 'investor' | 'founder'. If both sides have marked, unlock.

    Returns:
        intent_id, investor_meet_at, founder_meet_at, unlocked: bool,
        side_newly_set: bool — True only if THIS call flipped this side
            from unset to set (caller uses this to decide whether to notify
            the peer about a fresh "wants to meet" signal),
        first_unlock: bool — True only if THIS call is the one that made
            both sides agreed for the first time (caller uses this to
            decide whether to push the one-shot "unlocked" broadcast).
    """
    if side not in ("investor", "founder"):
        raise ValueError("side must be 'investor' or 'founder'.")
    intent_id = _resolve_intent_id(intent_id)
    now = utc_now()
    col = "investor_meet_at" if side == "investor" else "founder_meet_at"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM bp_intents WHERE id = ?", (intent_id,)
        ).fetchone()
        if row is None:
            raise ValueError("兴趣记录不存在。")
        if row["status"] not in ("accepted", "auto_accepted"):
            raise ValueError("只有已批准的意向可以发起约见。")

        side_newly_set = row[col] is None
        if side_newly_set:
            conn.execute(
                f"UPDATE bp_intents SET {col} = ? WHERE id = ?",
                (now, intent_id),
            )

        updated = conn.execute(
            "SELECT investor_meet_at, founder_meet_at, meeting_unlocked_at "
            "FROM bp_intents WHERE id = ?",
            (intent_id,),
        ).fetchone()

        both_agreed = bool(updated["investor_meet_at"]) and bool(updated["founder_meet_at"])
        first_unlock = both_agreed and updated["meeting_unlocked_at"] is None
        if first_unlock:
            conn.execute(
                "UPDATE bp_intents SET meeting_unlocked_at = ? WHERE id = ?",
                (now, intent_id),
            )

    return {
        "intent_id": intent_id,
        "investor_meet_at": updated["investor_meet_at"],
        "founder_meet_at": updated["founder_meet_at"],
        "unlocked": both_agreed,
        "side_newly_set": side_newly_set,
        "first_unlock": first_unlock,
    }


def get_meeting_unlock_payload(intent_id: str, for_side: str) -> dict:
    """Return contact info of the OTHER side, given this side is requesting.

    for_side = 'investor' → return founder's contact
    for_side = 'founder' → return investor's contact
    """
    if for_side not in ("investor", "founder"):
        raise ValueError("for_side must be 'investor' or 'founder'.")

    with get_conn() as conn:
        row = conn.execute(
            f"""SELECT bi.id AS intent_id, bi.listing_id,
                       bl.project_name,
                       inv.owner_id AS investor_owner_id,
                       fnd.owner_id AS founder_owner_id,
                       inv.name AS investor_name, inv.org_name AS investor_org,
                       fnd.name AS founder_name, fnd.org_name AS founder_org
                FROM bp_intents bi
                JOIN bp_listings bl ON bl.id = bi.listing_id
                JOIN lobsters inv ON inv.id = bi.investor_lobster_id
                JOIN lobsters fnd ON fnd.id = bl.founder_lobster_id
                WHERE bi.id = ?""",
            (intent_id,),
        ).fetchone()
    if row is None:
        raise ValueError("兴趣记录不存在。")

    # Pick "the other side" contact
    if for_side == "investor":
        peer_owner_id = row["founder_owner_id"]
        peer_name = row["founder_name"]
        peer_org = row["founder_org"]
    else:
        peer_owner_id = row["investor_owner_id"]
        peer_name = row["investor_name"]
        peer_org = row["investor_org"]

    contact = get_owner_contact(str(peer_owner_id or ""))

    return {
        "intent_id": row["intent_id"],
        "listing_id": row["listing_id"],
        "project_name": row["project_name"],
        "peer_name": peer_name,
        "peer_org": peer_org,
        "peer_contact": contact["primary_contact"],
        "peer_contact_type": contact["primary_contact_type"],
        "peer_secondary_contacts": contact["secondary_contacts"],
        "unlocked_at": utc_now(),
    }


def expire_stale_meeting_requests() -> int:
    """Close meeting requests older than MEETING_EXPIRE_DAYS. Returns count closed."""
    cutoff = (datetime.fromisoformat(utc_now()) - timedelta(days=MEETING_EXPIRE_DAYS)).isoformat()
    with get_conn() as conn:
        cursor = conn.execute(
            """UPDATE bp_intents
               SET status = 'expired'
               WHERE status IN ('accepted','auto_accepted')
                 AND (
                   (investor_meet_at IS NOT NULL AND founder_meet_at IS NULL AND investor_meet_at < ?)
                   OR
                   (founder_meet_at IS NOT NULL AND investor_meet_at IS NULL AND founder_meet_at < ?)
                 )""",
            (cutoff, cutoff),
        )
        return cursor.rowcount
