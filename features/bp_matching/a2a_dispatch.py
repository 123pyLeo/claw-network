"""A2A dispatch — translate state-machine actions into WS pushes.

The state machine in `a2a_engine` returns dicts like
  {"kind": "speak", "speaker_claw_id": ...}
  {"kind": "vote"}
  {"kind": "concluded", "match": True/False, ...}

This module pushes the corresponding WebSocket events to the right
plugin sidecars. Kept separate from the engine so the engine stays
pure (testable, no async, no WS dependencies).
"""

from __future__ import annotations

from typing import Any

from server.realtime import manager
from . import a2a_engine
from .store import get_listing, get_investor_profile


def _build_session_context(session_id: str) -> dict:
    """Pull together everything a sidecar needs to drive its turn:
    session row, BP details, recent message history, and human-readable
    names for both lobsters/owners (so the IM never shows raw CLAW-XXXXXX)."""
    sess = a2a_engine.get_session(session_id)
    if sess is None:
        return {}
    listing = None
    try:
        listing = get_listing(sess["listing_id"])
    except Exception:
        pass
    # Scope history to THIS session only — previous A2A sessions between
    # the same pair would otherwise leak in (their courtesy "保持联系"
    # endings poison the new session's context, making the investor
    # immediately think "we're done" and output [END] on turn 1).
    history = _load_recent_messages(
        sess["investor_claw_id"], sess["founder_claw_id"],
        since_iso=str(sess.get("created_at") or ""),
        limit=40,
    )
    inv_meta = _lobster_display(sess["investor_claw_id"])
    fnd_meta = _lobster_display(sess["founder_claw_id"])
    # Pull the investor's preference card so the founder's prompt builder
    # can anchor the conversation on the investor's actual thesis (sectors,
    # ticket size, decision cycle), not just generic "be a founder".
    investor_profile: dict | None = None
    try:
        investor_profile = get_investor_profile(sess["investor_claw_id"])
    except Exception:
        investor_profile = None
    return {
        "session_id": session_id,
        "intent_id": sess["intent_id"],
        "listing_id": sess["listing_id"],
        "investor_claw_id": sess["investor_claw_id"],
        "investor_name": inv_meta["name"],
        "investor_owner_name": inv_meta["owner_name"],
        "investor_org_name": inv_meta["org_name"],
        "investor_role": inv_meta["role"],
        "founder_claw_id": sess["founder_claw_id"],
        "founder_name": fnd_meta["name"],
        "founder_owner_name": fnd_meta["owner_name"],
        "founder_org_name": fnd_meta["org_name"],
        "founder_role": fnd_meta["role"],
        "turn_count": sess["turn_count"],
        "next_speaker": sess["next_speaker"],
        "max_turns": a2a_engine.MAX_TURNS,
        "listing": listing,
        "investor_profile": investor_profile,
        "history": history,
    }


def _lobster_display(claw_id: str) -> dict:
    """Look up lobster name + owner name + role + org_name for IM display."""
    from server.store import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT name, owner_name, role, org_name FROM lobsters WHERE claw_id = ?",
            (str(claw_id).strip().upper(),),
        ).fetchone()
    if row is None:
        return {"name": str(claw_id), "owner_name": "", "role": "", "org_name": ""}
    return {
        "name": str(row["name"] or claw_id),
        "owner_name": str(row["owner_name"] or ""),
        "role": str(row["role"] or ""),
        "org_name": str(row["org_name"] or ""),
    }


def _peer_contact(peer_claw_id: str) -> dict:
    """Get the unlocked contact info for the peer's owner. Returns empty
    dict if no contact configured. Mirrors the lookup that the existing
    meeting-unlock notification does, so format stays consistent."""
    from server.store import get_conn
    try:
        from features.platform import store as plat_store
    except Exception:
        plat_store = None
    with get_conn() as conn:
        owner_row = conn.execute(
            "SELECT o.id, o.nickname, o.primary_contact, o.primary_contact_type, o.secondary_contacts "
            "FROM owners o JOIN lobsters l ON l.owner_id = o.id "
            "WHERE l.claw_id = ?",
            (str(peer_claw_id).strip().upper(),),
        ).fetchone()
    if owner_row is None:
        return {}
    secondary = {}
    raw = owner_row["secondary_contacts"] or ""
    if raw:
        try:
            import json as _json
            secondary = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            secondary = {}
    return {
        "primary_contact": owner_row["primary_contact"] or "",
        "primary_contact_type": owner_row["primary_contact_type"] or "",
        "secondary_contacts": secondary,
        "owner_nickname": owner_row["nickname"] or "",
    }


def _load_recent_messages(claw_a: str, claw_b: str, since_iso: str = "", limit: int = 30) -> list[dict]:
    """Return text messages between the two claws (oldest→newest), optionally
    filtered to messages created at-or-after `since_iso`."""
    from server.store import get_conn
    base_sql = """
        SELECT me.created_at,
               lf.claw_id as from_claw, lf.name as from_name,
               lt.claw_id as to_claw,
               me.content
        FROM message_events me
        JOIN lobsters lf ON lf.id = me.from_lobster_id
        JOIN lobsters lt ON lt.id = me.to_lobster_id
        WHERE me.event_type = 'text'
          AND ((lf.claw_id = ? AND lt.claw_id = ?)
            OR (lf.claw_id = ? AND lt.claw_id = ?))
    """
    params: list = [claw_a, claw_b, claw_b, claw_a]
    if since_iso:
        base_sql += " AND me.created_at >= ?"
        params.append(since_iso)
    base_sql += " ORDER BY me.created_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(base_sql, tuple(params)).fetchall()
    out = [dict(r) for r in rows]
    out.reverse()
    return out


async def push_contact_missing_nudge(session_id: str) -> None:
    """If either side of a fresh session lacks contact info, send them a
    chat-first reminder up front. Cheaper to nudge now than to discover the
    gap only after 14 turns of dialogue (Phase 7 contact gating then has to
    silently withhold the unlock and the user feels cheated).

    Idempotent in spirit: callers should only invoke once per session start.
    Side that already has contact gets nothing.
    """
    ctx = _build_session_context(session_id)
    if not ctx:
        return
    from server.store import get_conn
    pairs = (
        (ctx["investor_claw_id"], "investor"),
        (ctx["founder_claw_id"], "founder"),
    )
    for claw, role in pairs:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT o.primary_contact "
                "FROM owners o JOIN lobsters l ON l.owner_id = o.id "
                "WHERE l.claw_id = ?",
                (str(claw).strip().upper(),),
            ).fetchone()
        primary = (row["primary_contact"] if row else "") or ""
        if primary.strip():
            continue  # this side is fine
        await manager.send_to_agent(claw, {
            "event": "a2a:contact_missing",
            "payload": {
                "session_id": session_id,
                "intent_id": ctx["intent_id"],
                "my_role": role,
                "phase": "session_start",
                "reason": "撮合对话刚开始，但你还没填联系方式。聊到结束如果没补上，对方拿不到联系方式。"
                          "现在告诉我『我的微信是 xxx』或『我的手机是 1xxx』就行。",
            },
        })


async def dispatch(action: dict, session_id: str) -> None:
    """Take a state-machine action dict and push the corresponding WS
    signals to the relevant sidecars. Idempotent — safe to re-call."""
    if not isinstance(action, dict):
        return
    kind = action.get("kind")
    ctx = _build_session_context(session_id)
    if not ctx:
        return

    def _peer_block(my_role: str) -> dict:
        if my_role == "investor":
            # I'm investor → peer is founder
            return {
                "peer_claw_id": ctx["founder_claw_id"],
                "peer_name": ctx["founder_name"],
                "peer_owner_name": ctx["founder_owner_name"],
                "peer_org_name": ctx.get("founder_org_name", ""),
                "peer_role": ctx.get("founder_role") or "founder",
            }
        # I'm founder → peer is investor
        return {
            "peer_claw_id": ctx["investor_claw_id"],
            "peer_name": ctx["investor_name"],
            "peer_owner_name": ctx["investor_owner_name"],
            "peer_org_name": ctx.get("investor_org_name", ""),
            "peer_role": ctx.get("investor_role") or "investor",
        }

    if kind == "speak":
        speaker = str(action.get("speaker_claw_id") or "").strip().upper()
        if not speaker:
            return
        my_role = "investor" if speaker == ctx["investor_claw_id"] else "founder"
        await manager.send_to_agent(speaker, {
            "event": "a2a:your_turn",
            "payload": {
                **ctx,
                "my_claw_id": speaker,
                "my_role": my_role,
                **_peer_block(my_role),
            },
        })

    elif kind == "vote":
        for claw, role in (
            (ctx["investor_claw_id"], "investor"),
            (ctx["founder_claw_id"], "founder"),
        ):
            await manager.send_to_agent(claw, {
                "event": "a2a:judge",
                "payload": {
                    **ctx,
                    "my_claw_id": claw,
                    "my_role": role,
                    **_peer_block(role),
                },
            })

    elif kind == "concluded":
        match = bool(action.get("match"))
        sess = action.get("session") or ctx
        summary = sess.get("summary") or ""
        # Engine signals which sides (if any) didn't have a contact filled at
        # match time. We send the same a2a:concluded payload to everyone, but
        # also fire a separate a2a:contact_missing nudge to each side that
        # needs to fill — Phase 2 touchpoint C.
        missing = action.get("missing_contacts") or []
        for claw, role in (
            (ctx["investor_claw_id"], "investor"),
            (ctx["founder_claw_id"], "founder"),
        ):
            peer_info = _peer_block(role)
            # Only surface peer_contact when match=True AND both sides have
            # contact (i.e. unlock actually happened). Otherwise leave empty
            # so the sidecar IM template knows to skip the contact line.
            both_filled = match and not missing
            contact = _peer_contact(peer_info["peer_claw_id"]) if both_filled else {}
            await manager.send_to_agent(claw, {
                "event": "a2a:concluded",
                "payload": {
                    "session_id": session_id,
                    "intent_id": ctx["intent_id"],
                    "match": match,
                    "summary": summary,
                    "history": ctx.get("history") or [],
                    "listing": ctx.get("listing") or {},
                    "my_role": role,
                    **peer_info,
                    "peer_contact": contact,
                    "contact_unlocked": bool(both_filled),
                    "missing_contacts": missing,
                    "turn_count": ctx["turn_count"],
                },
            })

        # Phase 2 touchpoint C: nudge sides that didn't have contact filled.
        if match and missing:
            for side in missing:
                claw = ctx["investor_claw_id"] if side == "investor" else ctx["founder_claw_id"]
                await manager.send_to_agent(claw, {
                    "event": "a2a:contact_missing",
                    "payload": {
                        "session_id": session_id,
                        "intent_id": ctx["intent_id"],
                        "my_role": side,
                        "reason": "撮合成功，但你还没填联系方式，对方暂时拿不到。告诉我你的微信或手机即可。",
                    },
                })

    elif kind == "stalled":
        for claw in (ctx["investor_claw_id"], ctx["founder_claw_id"]):
            await manager.send_to_agent(claw, {
                "event": "a2a:stalled",
                "payload": {
                    "session_id": session_id,
                    "intent_id": ctx["intent_id"],
                    "turn_count": ctx["turn_count"],
                    "reason": "5 分钟无活动，会话挂起。建议自己接手。",
                },
            })
