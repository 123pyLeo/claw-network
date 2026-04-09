from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from server.main import app
from server.store import DB_PATH, grant_funds_by_claw_id, init_db


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

        alice_account = client.get(f"/lobsters/{alice_claw_id}/account", headers=alice_headers)
        bob_account = client.get(f"/lobsters/{bob_claw_id}/account", headers=bob_headers)
        alice_account.raise_for_status()
        bob_account.raise_for_status()
        print("alice account initial:", json.dumps(alice_account.json(), ensure_ascii=False, indent=2))
        print("bob account initial:", json.dumps(bob_account.json(), ensure_ascii=False, indent=2))
        assert alice_account.json()["balance_total"] == 0, "expected alice account to start at 0"
        assert bob_account.json()["balance_total"] == 0, "expected bob account to start at 0"

        granted = grant_funds_by_claw_id(alice_claw_id, 500, note="self_check seed funds")
        print("alice account granted:", json.dumps(dict(granted), ensure_ascii=False, indent=2))

        bounty = client.post(
            "/bounties",
            params={"claw_id": alice_claw_id},
            headers=alice_headers,
            json={
                "title": "帮我整理一页支付方案摘要",
                "description": "聚焦 payment happy path，产出一页摘要。",
                "tags": "payment,summary",
                "bidding_window": "4h",
                "reward_amount": 120,
            },
        )
        bounty.raise_for_status()
        bounty_data = bounty.json()
        print("bounty:", json.dumps(bounty_data, ensure_ascii=False, indent=2))

        bid = client.post(
            f"/bounties/{bounty_data['id']}/bid",
            params={"claw_id": bob_claw_id},
            headers=bob_headers,
            json={"pitch": "我可以基于现有 payment 逻辑给你整理。"},
        )
        bid.raise_for_status()
        bid_data = bid.json()
        print("bid:", json.dumps(bid_data, ensure_ascii=False, indent=2))

        selected = client.post(
            f"/bounties/{bounty_data['id']}/select",
            params={"claw_id": alice_claw_id},
            headers=alice_headers,
            json={"bid_ids": [bid_data["id"]]},
        )
        selected.raise_for_status()
        selected_data = selected.json()
        print("selected bounty:", json.dumps(selected_data, ensure_ascii=False, indent=2))
        assert selected_data["status"] == "assigned", "expected bounty to become assigned after select"
        assert selected_data["invocation_id"], "expected payment bounty to create an invocation"
        assert selected_data["selected_bid_id"] == bid_data["id"], "expected selected bid to be stored on bounty"

        alice_reserved = client.get(f"/lobsters/{alice_claw_id}/account", headers=alice_headers)
        alice_reserved.raise_for_status()
        alice_reserved_data = alice_reserved.json()
        print("alice account reserved:", json.dumps(alice_reserved_data, ensure_ascii=False, indent=2))
        assert alice_reserved_data["balance_total"] == 500, "reserve should not change total balance"
        assert alice_reserved_data["balance_committed"] == 120, "expected reserved funds to move into committed"
        assert alice_reserved_data["balance_available"] == 380, "expected available balance to shrink after reserve"

        fulfilled = client.post(
            f"/bounties/{bounty_data['id']}/fulfill",
            params={"claw_id": bob_claw_id},
            headers=bob_headers,
        )
        fulfilled.raise_for_status()
        fulfilled_data = fulfilled.json()
        print("fulfilled bounty:", json.dumps(fulfilled_data, ensure_ascii=False, indent=2))
        assert fulfilled_data["status"] == "fulfilled", "expected bounty to become fulfilled"
        assert fulfilled_data["settlement_status"] == "pending", "expected settlement to wait for confirmation"

        settled = client.post(
            f"/bounties/{bounty_data['id']}/settlement/confirm",
            params={"claw_id": alice_claw_id},
            headers=alice_headers,
        )
        settled.raise_for_status()
        settled_data = settled.json()
        print("settled bounty:", json.dumps(settled_data, ensure_ascii=False, indent=2))
        assert settled_data["bounty"]["status"] == "settled", "expected bounty to become settled"
        assert settled_data["invocation"]["settlement_status"] == "settled", "expected invocation settlement to finish"

        alice_final = client.get(f"/lobsters/{alice_claw_id}/account", headers=alice_headers)
        bob_final = client.get(f"/lobsters/{bob_claw_id}/account", headers=bob_headers)
        alice_final.raise_for_status()
        bob_final.raise_for_status()
        alice_final_data = alice_final.json()
        bob_final_data = bob_final.json()
        print("alice account final:", json.dumps(alice_final_data, ensure_ascii=False, indent=2))
        print("bob account final:", json.dumps(bob_final_data, ensure_ascii=False, indent=2))
        assert alice_final_data["balance_total"] == 380, "expected payer total to decrease after settlement"
        assert alice_final_data["balance_committed"] == 0, "expected committed balance to clear after settlement"
        assert alice_final_data["balance_available"] == 380, "expected payer available to match total after settlement"
        assert bob_final_data["balance_total"] == 120, "expected payee total to increase after settlement"
        assert bob_final_data["balance_available"] == 120, "expected payee available to increase after settlement"

        alice_ledger = client.get(f"/lobsters/{alice_claw_id}/ledger", headers=alice_headers)
        bob_ledger = client.get(f"/lobsters/{bob_claw_id}/ledger", headers=bob_headers)
        alice_ledger.raise_for_status()
        bob_ledger.raise_for_status()
        alice_ledger_rows = alice_ledger.json()
        bob_ledger_rows = bob_ledger.json()
        print("alice ledger:", json.dumps(alice_ledger_rows, ensure_ascii=False, indent=2))
        print("bob ledger:", json.dumps(bob_ledger_rows, ensure_ascii=False, indent=2))
        alice_actions = {entry["action"] for entry in alice_ledger_rows}
        bob_actions = {entry["action"] for entry in bob_ledger_rows}
        assert {"grant", "reserve", "settle_debit"}.issubset(alice_actions), "expected alice ledger to include grant/reserve/settle_debit"
        assert "settle_credit" in bob_actions, "expected bob ledger to include settle_credit"

        payment_events = client.get(f"/events/{bob_claw_id}", headers=bob_headers)
        payment_events.raise_for_status()
        payment_event_rows = payment_events.json()
        print("bob events after payment:", json.dumps(payment_event_rows, ensure_ascii=False, indent=2))
        assert any(event["event_type"] == "payment_reserved" for event in payment_event_rows), "expected payment_reserved event"
        assert any(event["event_type"] == "payment_pending_settlement" for event in payment_event_rows), "expected payment_pending_settlement event"
        assert any(event["event_type"] == "payment_settled" for event in payment_event_rows), "expected payment_settled event"

    print(f"self_check passed using {Path(DB_PATH)}")


if __name__ == "__main__":
    main()
