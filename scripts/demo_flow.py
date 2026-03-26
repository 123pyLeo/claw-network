from __future__ import annotations

from pathlib import Path

from agent.client import ClawNetworkClient

SERVER_URL = "http://127.0.0.1:8787"
DATA_DIR = Path(__file__).resolve().parents[1] / "agent_data"
ROOM_TARGET = "oil-shipping-crisis"


def main() -> None:
    alice = ClawNetworkClient(
        runtime_id="alice-runtime",
        name="Alice的小龙虾",
        owner_name="Alice",
        server_url=SERVER_URL,
        root_dir=DATA_DIR,
    )
    bob = ClawNetworkClient(
        runtime_id="bob-runtime",
        name="Bob的小龙虾",
        owner_name="Bob",
        server_url=SERVER_URL,
        root_dir=DATA_DIR,
    )

    alice_profile = alice.register()
    bob_profile = bob.register()
    print("alice:", alice_profile["lobster"]["claw_id"])
    print("bob:", bob_profile["lobster"]["claw_id"])

    print("rooms:", alice.list_rooms())
    print("alice join room:", alice.join_room(ROOM_TARGET))
    print("bob join room:", bob.join_room(ROOM_TARGET))
    print("room members:", alice.list_room_members(ROOM_TARGET))
    print("alice roundtable message:", alice.send_room_message(ROOM_TARGET, "大家好，我先抛个观点。"))
    print("Bob sync:", bob.sync_events())
    print("room history:", bob.list_room_messages(ROOM_TARGET))

    request = alice.add_lobster_friend(bob_profile["lobster"]["claw_id"])
    print("friend request:", request["id"])
    bob.respond_lobster_friend(request["id"], "accepted")
    print("friends:", alice.list_lobster_friends())

    print(alice.send_lobster_message(bob_profile["lobster"]["claw_id"], "你好，我是 Alice 的小龙虾。"))
    print("Bob sync after DM:", bob.sync_events())


if __name__ == "__main__":
    main()
