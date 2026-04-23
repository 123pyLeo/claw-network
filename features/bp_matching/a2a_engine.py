"""A2A autonomous matchmaking — server-side coordinator.

Pure state machine. Does NOT call LLMs (that happens in each user's
plugin sidecar with their own credentials). Server's job is only:

  1. Decide which side speaks next, and signal them to do so.
  2. Track turn count, last-activity timestamp.
  3. Periodically ask both sides "want to end?" — record votes.
  4. When both vote want_end OR hard cap (20 turns) reached, conclude.
  5. On match, trigger contact exchange so the humans take over.

Conversation flow:

   start_session(intent_id)
     ↓
   running, next_speaker='investor'
     ↓
   [WS push 'a2a:your_turn' to investor — done by routes layer]
     ↓
   investor's plugin calls LLM → send_lobster_message → message arrives
     ↓
   on_message_in_session(session_id, from='investor', ...)
     ↓
   turn_count += 1, next_speaker='founder'
     ↓
   [push 'a2a:your_turn' to founder]
     ...
     ↓
   after 8 turns, every 4 turns → request_vote
     ↓
   [push 'a2a:judge' to both sides]
     ↓
   each side: LLM judges → POST /vote
     ↓
   record_vote → if both voted: maybe conclude, else resume

Hard limits keep the system bounded:
  - 20 turns total → force conclude
  - 5 min since last_turn_at → mark stalled
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from server.store import get_conn, new_uuid, utc_now, ensure_friendship, get_lobster_by_claw_id


# ---------------------------------------------------------------------------
# Tunables (can move to config later)
# ---------------------------------------------------------------------------

MAX_TURNS = 14                # hard cap (was 20 — agents spiral into courtesy after ~6-8 substantive turns)
VOTE_FIRST_AT_TURN = 6        # vote earlier so we can end before courtesy loops set in
VOTE_EVERY_N_TURNS = 2        # vote at 6, 8, 10, 12, 14
SPEAKER_STUCK_SECONDS = 15    # if speaker hasn't replied in 15s, retry once
RETRY_LIMIT = 1               # retries per turn before giving up (1 = retry once, then stalled)
STALL_TIMEOUT_SECONDS = 60    # absolute give-up: 60s total (~ 15s wait + 15s retry + buffer) → stalled

# Courtesy / low-content reply detector. If the last few messages match
# this pattern (each ≤ 50 chars and contains common courtesy phrases),
# we force-trigger a vote regardless of turn count — agents are clearly
# not adding substance anymore.
_COURTESY_KEYWORDS = (
    "保持联系", "期待", "继续聊", "随时喊", "随时联系", "材料准备好",
    "等你材料", "等你的", "理一理", "保持沟通", "祝顺利", "再见",
)


def _looks_courtesy(content: str) -> bool:
    if not content:
        return True
    text = content.strip()
    if len(text) <= 50 and any(kw in text for kw in _COURTESY_KEYWORDS):
        return True
    # Ultra short reply (<= 25 chars), even without keyword, is suspect
    if len(text) <= 25:
        return True
    return False


# ---------------------------------------------------------------------------
# Status enum (string)
# ---------------------------------------------------------------------------

S_PENDING = "pending"
S_RUNNING = "running"
S_AWAITING_VOTE = "awaiting_vote"
S_CONCLUDED_MATCH = "concluded_match"
S_CONCLUDED_PASS = "concluded_pass"
S_STALLED = "stalled"
S_FAILED = "failed"

ACTIVE_STATUSES = (S_PENDING, S_RUNNING, S_AWAITING_VOTE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_session(intent_id: str) -> dict | None:
    """Begin an A2A session for an accepted bp_intent.

    Caller (review_intent accept branch) should invoke this AFTER intent
    status flips to 'accepted'. Returns the new session row + a
    'next_action' hint the caller can use to push the first 'your_turn'
    WS signal.

    Returns None if A2A is not eligible (mode!=auto on either side, or
    a session already exists for this intent).
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM bp_a2a_sessions WHERE intent_id = ?", (intent_id,)
        ).fetchone()
        if existing is not None:
            return None
        intent = conn.execute(
            """
            SELECT bi.id, bi.listing_id, bi.status,
                   inv.claw_id AS inv_claw, inv.bp_a2a_mode AS inv_mode,
                   fnd.claw_id AS fnd_claw, fnd.bp_a2a_mode AS fnd_mode
            FROM bp_intents bi
            JOIN lobsters inv ON inv.id = bi.investor_lobster_id
            JOIN bp_listings bl ON bl.id = bi.listing_id
            JOIN lobsters fnd ON fnd.id = bl.founder_lobster_id
            WHERE bi.id = ?
            """,
            (intent_id,),
        ).fetchone()
    if intent is None:
        return None
    if intent["status"] not in ("accepted", "auto_accepted"):
        return None
    inv_mode = (intent["inv_mode"] or "manual").lower()
    fnd_mode = (intent["fnd_mode"] or "manual").lower()
    if inv_mode != "auto" or fnd_mode != "auto":
        # MVP: only fully-auto sessions. Manual / mixed mode = v2.
        return None

    session_id = new_uuid()
    now = utc_now()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO bp_a2a_sessions
              (id, intent_id, listing_id, investor_claw_id, founder_claw_id,
               status, turn_count, next_speaker, created_at, updated_at, last_turn_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, 'investor', ?, ?, ?)
            """,
            (
                session_id, intent_id, intent["listing_id"],
                intent["inv_claw"], intent["fnd_claw"],
                S_RUNNING, now, now, now,
            ),
        )
        row = conn.execute("SELECT * FROM bp_a2a_sessions WHERE id = ?", (session_id,)).fetchone()
    return {
        "session": dict(row),
        "next_action": {"kind": "speak", "speaker_claw_id": intent["inv_claw"]},
    }


def get_active_session_for_pair(claw_a: str, claw_b: str) -> dict | None:
    """Find an active session between two claws, regardless of direction."""
    a = claw_a.strip().upper(); b = claw_b.strip().upper()
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM bp_a2a_sessions
            WHERE status IN ({','.join(['?']*len(ACTIVE_STATUSES))})
              AND ((investor_claw_id = ? AND founder_claw_id = ?)
                OR (investor_claw_id = ? AND founder_claw_id = ?))
            ORDER BY created_at DESC LIMIT 1
            """,
            (*ACTIVE_STATUSES, a, b, b, a),
        ).fetchone()
    return dict(row) if row else None


def on_message_in_session(session_id: str, from_claw: str, content: str = "") -> dict | None:
    """Called from the message-delivery hook when a message lands inside an
    active session. Updates turn count, flips next_speaker, decides next
    action.

    Returns dict like:
      {"kind": "speak", "speaker_claw_id": "..."}    — push your_turn next
      {"kind": "vote"}                                — push judge to both
      {"kind": "conclude", "match": True/False}      — finalize
      None — session not eligible (concluded, stalled, etc.)
    """
    from_claw = from_claw.strip().upper()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bp_a2a_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None or row["status"] not in ACTIVE_STATUSES:
        return None

    inv = str(row["investor_claw_id"]).upper()
    fnd = str(row["founder_claw_id"]).upper()
    if from_claw not in (inv, fnd):
        return None

    new_turn_count = int(row["turn_count"]) + 1
    other_side = "founder" if from_claw == inv else "investor"
    now = utc_now()

    with get_conn() as conn:
        conn.execute(
            """
            UPDATE bp_a2a_sessions
               SET turn_count = ?, next_speaker = ?, last_turn_at = ?, updated_at = ?,
                   retry_count = 0
             WHERE id = ?
            """,
            (new_turn_count, other_side, now, now, session_id),
        )

    # Decide next action.
    if new_turn_count >= MAX_TURNS:
        return _begin_vote(session_id)
    if new_turn_count >= VOTE_FIRST_AT_TURN and (new_turn_count - VOTE_FIRST_AT_TURN) % VOTE_EVERY_N_TURNS == 0:
        return _begin_vote(session_id)
    # Early-exit: if THIS message AND the previous one are both pure
    # courtesy ("保持联系", "好等你材料", etc.), the substance is over.
    # Force a vote so we don't burn turns on filler.
    if new_turn_count >= 4 and _looks_courtesy(content):
        prev_msg = _last_text_between(inv, fnd, before_id=None)
        if prev_msg and _looks_courtesy(prev_msg.get("content") or ""):
            return _begin_vote(session_id)
    next_speaker_claw = fnd if other_side == "founder" else inv
    return {"kind": "speak", "speaker_claw_id": next_speaker_claw}


def _last_text_between(claw_a: str, claw_b: str, *, before_id: str | None = None) -> dict | None:
    """Get the most recent text message between two claws (excluding the
    just-arrived one; used by courtesy detection to check if the
    PREVIOUS turn was also courtesy)."""
    sql = """
        SELECT me.id, lf.claw_id AS from_claw, me.content
        FROM message_events me
        JOIN lobsters lf ON lf.id = me.from_lobster_id
        JOIN lobsters lt ON lt.id = me.to_lobster_id
        WHERE me.event_type = 'text'
          AND ((lf.claw_id = ? AND lt.claw_id = ?) OR (lf.claw_id = ? AND lt.claw_id = ?))
        ORDER BY me.created_at DESC LIMIT 2
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (claw_a, claw_b, claw_b, claw_a)).fetchall()
    # Skip the current (latest); return the second-latest.
    if len(rows) >= 2:
        return dict(rows[1])
    return None


def _begin_vote(session_id: str) -> dict:
    """Flip session to awaiting_vote and clear prior votes."""
    now = utc_now()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE bp_a2a_sessions
               SET status = ?, investor_want_end = NULL, founder_want_end = NULL,
                   updated_at = ?
             WHERE id = ?
            """,
            (S_AWAITING_VOTE, now, session_id),
        )
    return {"kind": "vote"}


def record_vote(session_id: str, claw_id: str, want_end: bool) -> dict:
    """Record one side's want_end vote. If both have voted, decide."""
    claw_id = claw_id.strip().upper()
    now = utc_now()
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bp_a2a_sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        raise ValueError("Session not found.")
    if row["status"] != S_AWAITING_VOTE:
        raise ValueError(f"Session is not awaiting vote (status={row['status']}).")
    inv = str(row["investor_claw_id"]).upper()
    fnd = str(row["founder_claw_id"]).upper()
    if claw_id not in (inv, fnd):
        raise ValueError("Voter is not part of this session.")
    col = "investor_want_end" if claw_id == inv else "founder_want_end"
    val = 1 if want_end else 0
    with get_conn() as conn:
        conn.execute(
            f"UPDATE bp_a2a_sessions SET {col} = ?, updated_at = ? WHERE id = ?",
            (val, now, session_id),
        )
        row = conn.execute("SELECT * FROM bp_a2a_sessions WHERE id = ?", (session_id,)).fetchone()

    inv_v = row["investor_want_end"]
    fnd_v = row["founder_want_end"]
    if inv_v is None or fnd_v is None:
        return {"kind": "wait_for_other_vote", "row": dict(row)}
    if inv_v == 1 and fnd_v == 1:
        return conclude(session_id, match=True, summary="双方 AI 都认为初步沟通已完成，建议见面深聊。")
    # At least one side wants to continue.
    if int(row["turn_count"]) >= MAX_TURNS:
        # Hard cap: even though one side wants more, we're done. Default to
        # match (they had 20 substantive turns; better to over-connect).
        return conclude(session_id, match=True, summary="达到对话轮数上限。AI 判断已覆盖足够内容，建议见面继续。")
    # Resume: flip back to running, the other side speaks next (whichever
    # wasn't the most recent speaker — we use the existing next_speaker hint).
    with get_conn() as conn:
        conn.execute(
            "UPDATE bp_a2a_sessions SET status = ?, updated_at = ? WHERE id = ?",
            (S_RUNNING, now, session_id),
        )
    next_speaker_claw = fnd if str(row["next_speaker"] or "") == "founder" else inv
    return {"kind": "speak", "speaker_claw_id": next_speaker_claw, "resumed": True}


def conclude(session_id: str, match: bool, summary: str = "") -> dict:
    """Finalize session. On match, also create the friendship + flag the
    intent as ready for contact exchange (uses existing
    request_meeting machinery so contact unlocks via the established
    flow)."""
    now = utc_now()
    final_status = S_CONCLUDED_MATCH if match else S_CONCLUDED_PASS
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bp_a2a_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ValueError("Session not found.")
        conn.execute(
            """
            UPDATE bp_a2a_sessions
               SET status = ?, summary = ?, concluded_at = ?, updated_at = ?
             WHERE id = ?
            """,
            (final_status, summary, now, now, session_id),
        )

    if match:
        # Both lobsters auto-friended on intent accept already; here we
        # also mark BOTH sides as wanting the meeting (the existing
        # contact-exchange flow only unlocks when both flags are set).
        try:
            from .store import request_meeting  # late import to avoid cycle
            request_meeting(str(row["intent_id"]), "investor")
            request_meeting(str(row["intent_id"]), "founder")
        except Exception:
            # Best-effort. If contact exchange fails here, the human can
            # still trigger '沙堆 约见 <intent>' manually.
            pass

    return {
        "kind": "concluded",
        "match": match,
        "session": dict(row) | {"status": final_status, "summary": summary},
    }


def driver_tick() -> list[dict]:
    """Background tick: handle timeouts and re-issue any actions that got
    dropped. Called periodically by the FastAPI background driver.

    Returns a list of action dicts the caller (which has WS access) should
    dispatch:
      {"kind": "stalled", "session_id": ...}            — final timeout
      {"kind": "vote", "session_id": ...}               — re-push the judge signal
      {"kind": "speak", "session_id": ..., "speaker_claw_id": ...}  — re-push your_turn
    """
    now_dt = datetime.now(timezone.utc)
    actions: list[dict] = []
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM bp_a2a_sessions
            WHERE status IN ({','.join(['?']*len(ACTIVE_STATUSES))})
            """,
            ACTIVE_STATUSES,
        ).fetchall()
    for row in rows:
        last_at = row["last_turn_at"] or row["created_at"]
        try:
            last_dt = datetime.fromisoformat(last_at)
        except Exception:
            continue
        idle_seconds = (now_dt - last_dt).total_seconds()
        retry_count = int(row["retry_count"] or 0)

        # Speaker stuck: speak hasn't completed in SPEAKER_STUCK_SECONDS
        if row["status"] == S_RUNNING and idle_seconds > SPEAKER_STUCK_SECONDS:
            if retry_count >= RETRY_LIMIT:
                # Already retried; give up.
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE bp_a2a_sessions SET status = ?, updated_at = ? WHERE id = ?",
                        (S_STALLED, utc_now(), row["id"]),
                    )
                actions.append({"kind": "stalled", "session_id": row["id"], "row": dict(row)})
                continue
            # First retry: re-push your_turn, bump retry_count + last_turn_at
            speaker_role = row["next_speaker"] or "investor"
            speaker_claw = row["investor_claw_id"] if speaker_role == "investor" else row["founder_claw_id"]
            actions.append({"kind": "speak", "session_id": row["id"], "speaker_claw_id": speaker_claw, "retry": True})
            with get_conn() as conn:
                conn.execute(
                    "UPDATE bp_a2a_sessions SET retry_count = retry_count + 1, last_turn_at = ?, updated_at = ? WHERE id = ?",
                    (utc_now(), utc_now(), row["id"]),
                )
            continue

        # Vote stuck: only one side voted (or zero) and time's up
        if row["status"] == S_AWAITING_VOTE and idle_seconds > SPEAKER_STUCK_SECONDS:
            if row["investor_want_end"] is not None and row["founder_want_end"] is not None:
                continue  # both already voted; record_vote handler will conclude
            if retry_count >= RETRY_LIMIT:
                with get_conn() as conn:
                    conn.execute(
                        "UPDATE bp_a2a_sessions SET status = ?, updated_at = ? WHERE id = ?",
                        (S_STALLED, utc_now(), row["id"]),
                    )
                actions.append({"kind": "stalled", "session_id": row["id"], "row": dict(row)})
                continue
            actions.append({"kind": "vote", "session_id": row["id"], "retry": True})
            with get_conn() as conn:
                conn.execute(
                    "UPDATE bp_a2a_sessions SET retry_count = retry_count + 1, last_turn_at = ?, updated_at = ? WHERE id = ?",
                    (utc_now(), utc_now(), row["id"]),
                )

        # Belt-and-suspenders: absolute timeout regardless
        if idle_seconds > STALL_TIMEOUT_SECONDS:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE bp_a2a_sessions SET status = ?, updated_at = ? WHERE id = ?",
                    (S_STALLED, utc_now(), row["id"]),
                )
            actions.append({"kind": "stalled", "session_id": row["id"], "row": dict(row)})
    return actions


def get_session(session_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bp_a2a_sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None
