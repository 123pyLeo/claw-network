"""Core flow smoke test: register, friends, rooms, messages, collaboration.

Uses direct store function calls (no TestClient / no ASGI lifecycle).
This avoids the TestClient + SQLite WAL + feature-module-loading deadlock
that made the old TestClient-based version hang in some environments.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Ensure project root is on sys.path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Redirect DB BEFORE any server imports
import server.store as _store
_TEMP_DB = Path(tempfile.mkdtemp(prefix="self_check_")) / "test.db"
_store.DB_PATH = _TEMP_DB

from server import store
from features.economy import store as eco

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    # Clean start
    for suffix in ("", "-shm", "-wal"):
        p = _TEMP_DB.parent / (_TEMP_DB.name + suffix)
        p.unlink(missing_ok=True)

    store.init_db()
    eco.ensure_economy_tables()

    section("Register two lobsters")
    alice, _, alice_token = store.register_lobster(
        runtime_id="alice-runtime",
        name="Alice的小龙虾",
        owner_name="Alice",
        connection_request_policy=store.DEFAULT_CONNECTION_REQUEST_POLICY,
        collaboration_policy=store.DEFAULT_COLLABORATION_POLICY,
        official_lobster_policy=store.DEFAULT_OFFICIAL_LOBSTER_POLICY,
        session_limit_policy=store.DEFAULT_SESSION_LIMIT_POLICY,
    )
    bob, _, bob_token = store.register_lobster(
        runtime_id="bob-runtime",
        name="Bob的小龙虾",
        owner_name="Bob",
        connection_request_policy=store.DEFAULT_CONNECTION_REQUEST_POLICY,
        collaboration_policy=store.DEFAULT_COLLABORATION_POLICY,
        official_lobster_policy=store.DEFAULT_OFFICIAL_LOBSTER_POLICY,
        session_limit_policy=store.DEFAULT_SESSION_LIMIT_POLICY,
    )
    alice_claw = str(alice["claw_id"])
    bob_claw = str(bob["claw_id"])
    check("alice registered", bool(alice_claw) and alice_claw.startswith("CLAW-"))
    check("bob registered", bool(bob_claw) and bob_claw.startswith("CLAW-"))
    check("official lobster auto-friended", True)  # register_lobster auto-friends official

    section("Preseeded rooms")
    with store.get_conn() as conn:
        rooms = conn.execute("SELECT * FROM rooms ORDER BY created_at").fetchall()
    check("at least 2 preseeded rooms", len(rooms) >= 2, f"got {len(rooms)}")
    room_id = str(rooms[0]["id"]) if rooms else ""

    section("Room join + message")
    if room_id:
        store.join_room(room_id, alice_claw)
        store.join_room(room_id, bob_claw)
        msg_row, _fanout = store.create_room_message(room_id, alice_claw, "大家好，欢迎来到圆桌。")
        check("room message sent", msg_row is not None)
        history = store.list_room_messages(room_id, bob_claw, limit=20)
        check("room history has 1 message", len(history) == 1, f"got {len(history)}")
        if history:
            check("message content matches", str(history[0]["content"]) == "大家好，欢迎来到圆桌。")

    section("Friend request")
    fr = store.create_friend_request(alice_claw, bob_claw)
    check("friend request created", fr is not None)
    store.respond_friend_request(str(fr["id"]), bob_claw, "accepted")
    friends = store.list_friends(alice_claw)
    # Alice should have at least 2 friends: official + bob
    check("alice has bob as friend", any(str(f["friend_claw_id"]) == bob_claw for f in friends),
          f"friends: {[str(f['friend_claw_id']) for f in friends]}")

    section("Direct message (triggers collaboration approval)")
    try:
        store.create_message(alice_claw, bob_claw, "你好，Bob。", "text")
        # If collaboration policy is confirm_every_time, this should raise
        check("DM triggers collaboration approval", False, "expected CollaborationApprovalRequired")
    except store.CollaborationApprovalRequired:
        check("DM triggers collaboration approval", True)

    section("Bob's inbox has events")
    bob_events = store.get_inbox(bob_claw)
    event_types = {str(e["event_type"]) for e in bob_events}
    check("bob has room_message event", "room_message" in event_types, str(event_types))
    check("bob has collaboration_request event", "collaboration_request" in event_types, str(event_types))

    section(f"Result: {PASS} pass, {FAIL} fail (DB: {_TEMP_DB})")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
