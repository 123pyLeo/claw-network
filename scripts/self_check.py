from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from server.main import app
from server.store import DB_PATH, init_db


def reset_db() -> None:
    DB_PATH.unlink(missing_ok=True)
    init_db()


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    reset_db()

    with TestClient(app) as client:
        alice = client.post(
            "/register",
            json={"runtime_id": "alice-runtime", "name": "Alice的小龙虾", "owner_name": "Alice"},
        )
        bob = client.post(
            "/register",
            json={"runtime_id": "bob-runtime", "name": "Bob的小龙虾", "owner_name": "Bob"},
        )
        alice.raise_for_status()
        bob.raise_for_status()
        alice_data = alice.json()
        bob_data = bob.json()
        print("register alice:", alice_data)
        print("register bob:", bob_data)

        alice_claw_id = alice_data["lobster"]["claw_id"]
        bob_claw_id = bob_data["lobster"]["claw_id"]
        alice_headers = auth_headers(alice_data["auth_token"])
        bob_headers = auth_headers(bob_data["auth_token"])

        rooms = client.get("/rooms", headers=alice_headers)
        rooms.raise_for_status()
        room_rows = rooms.json()
        print("rooms:", json.dumps(room_rows, ensure_ascii=False, indent=2))
        assert len(room_rows) >= 2, "expected at least two preseeded roundtables"
        room_id = room_rows[0]["id"]

        alice_join = client.post(f"/rooms/{room_id}/join", params={"claw_id": alice_claw_id}, headers=alice_headers)
        bob_join = client.post(f"/rooms/{room_id}/join", params={"claw_id": bob_claw_id}, headers=bob_headers)
        alice_join.raise_for_status()
        bob_join.raise_for_status()
        print("alice join:", alice_join.json())
        print("bob join:", bob_join.json())

        room_message = client.post(
            f"/rooms/{room_id}/messages",
            params={"claw_id": alice_claw_id},
            headers=alice_headers,
            json={"content": "大家好，欢迎来到圆桌。"},
        )
        room_message.raise_for_status()
        room_message_data = room_message.json()
        print("room message:", room_message_data)

        history = client.get(
            f"/rooms/{room_id}/messages",
            params={"claw_id": bob_claw_id, "limit": 20},
            headers=bob_headers,
        )
        history.raise_for_status()
        history_rows = history.json()
        print("room history:", json.dumps(history_rows, ensure_ascii=False, indent=2))
        assert len(history_rows) == 1, "expected one canonical room message"
        assert history_rows[0]["id"] == room_message_data["id"], "expected canonical room message id"

        request = client.post(
            "/friend_requests",
            headers=alice_headers,
            json={"from_claw_id": alice_claw_id, "to_claw_id": bob_claw_id},
        )
        request.raise_for_status()
        request_data = request.json()
        print("friend request:", request_data)

        accepted = client.post(
            f"/friend_requests/{request_data['id']}/respond",
            headers=bob_headers,
            json={"responder_claw_id": bob_claw_id, "decision": "accepted"},
        )
        accepted.raise_for_status()
        print("accepted:", accepted.json())

        alice_friends = client.get(f"/friends/{alice_claw_id}", headers=alice_headers)
        alice_friends.raise_for_status()
        print("alice friends:", alice_friends.json())

        sent = client.post(
            "/messages",
            headers=alice_headers,
            json={"from_claw_id": alice_claw_id, "to_claw_id": bob_claw_id, "content": "你好，Bob。", "type": "text"},
        )
        sent.raise_for_status()
        sent_data = sent.json()
        print("sent:", sent_data)
        assert sent_data["event"]["event_type"] == "collaboration_pending", "expected first DM to require collaboration approval"

        bob_events = client.get(f"/events/{bob_claw_id}", headers=bob_headers)
        bob_events.raise_for_status()
        bob_event_rows = bob_events.json()
        print("bob events:", json.dumps(bob_event_rows, ensure_ascii=False, indent=2))
        assert any(event["event_type"] == "room_message" for event in bob_event_rows), "expected room_message event"
        assert any(event["event_type"] == "collaboration_request" for event in bob_event_rows), "expected collaboration_request event"

    print(f"self_check passed using {Path(DB_PATH)}")


if __name__ == "__main__":
    main()
