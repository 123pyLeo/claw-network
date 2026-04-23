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
from .store import get_listing


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
        # On match: pull each side's peer contact info so the IM card can
        # show "对方微信: xxx" inline (instead of leaving it to the
        # legacy meeting-unlock event).
        for claw, role in (
            (ctx["investor_claw_id"], "investor"),
            (ctx["founder_claw_id"], "founder"),
        ):
            peer_info = _peer_block(role)
            contact = _peer_contact(peer_info["peer_claw_id"]) if match else {}
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
                    "turn_count": ctx["turn_count"],
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
