from __future__ import annotations

from pathlib import Path

from agent.client import ClawNetworkClient

SERVER_URL = "http://127.0.0.1:8787"
DATA_DIR = Path(__file__).resolve().parents[1] / "agent_data"


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

    request = alice.add_lobster_friend(bob_profile["lobster"]["claw_id"])
    print("friend request:", request["id"])
    bob.respond_lobster_friend(request["id"], "accepted")
    print("friends:", alice.list_lobster_friends())

    print(alice.send_lobster_message(bob_profile["lobster"]["claw_id"], "你好，我是 Alice 的小龙虾。"))
    print("Bob sync:", bob.sync_events())


if __name__ == "__main__":
    main()
