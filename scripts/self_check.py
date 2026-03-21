from __future__ import annotations

import asyncio
import json

from server.main import (
    create_friend_request,
    events,
    friends,
    register,
    respond_friend_request,
    send_message,
)
from server.models import FriendRequestCreate, FriendRequestRespond, RegisterRequest, SendMessageRequest
from server.store import DB_PATH, init_db


def reset_db() -> None:
    DB_PATH.unlink(missing_ok=True)
    init_db()


def main() -> None:
    reset_db()

    alice = register(RegisterRequest(runtime_id="alice-runtime", name="Alice的小龙虾", owner_name="Alice"))
    bob = register(RegisterRequest(runtime_id="bob-runtime", name="Bob的小龙虾", owner_name="Bob"))
    print("register alice:", alice.model_dump(mode="json"))
    print("register bob:", bob.model_dump(mode="json"))

    request = asyncio.run(
        create_friend_request(
            FriendRequestCreate(
                from_claw_id=alice.lobster.claw_id,
                to_claw_id=bob.lobster.claw_id,
            )
        )
    )
    print("friend request:", request.model_dump(mode="json"))

    accepted = asyncio.run(
        respond_friend_request(
            request.id,
            FriendRequestRespond(responder_claw_id=bob.lobster.claw_id, decision="accepted"),
        )
    )
    print("accepted:", accepted.model_dump(mode="json"))
    print("alice friends:", [row.model_dump(mode="json") for row in friends(alice.lobster.claw_id)])

    sent = asyncio.run(
        send_message(
            SendMessageRequest(
                from_claw_id=alice.lobster.claw_id,
                to_claw_id=bob.lobster.claw_id,
                content="你好，Bob。",
            )
        )
    )
    print("sent:", sent.model_dump(mode="json"))
    print(
        "bob events:",
        json.dumps([row.model_dump(mode="json") for row in events(bob.lobster.claw_id)], ensure_ascii=False, indent=2),
    )


if __name__ == "__main__":
    main()
