from __future__ import annotations

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from . import store
from .models import (
    FriendRequestCreate,
    FriendRequestRespond,
    FriendRequestRow,
    FriendshipRow,
    LobsterPresenceRow,
    LobsterRow,
    MessageEventRow,
    RegisterRequest,
    RegisterResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from .realtime import manager

app = FastAPI(title="Claw Network MVP", version="0.2.0")


def _http_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.on_event("startup")
def on_startup() -> None:
    store.init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/online_lobsters")
async def online_lobsters() -> dict[str, list[str]]:
    return {"lobsters": await manager.list_online()}


@app.post("/register", response_model=RegisterResponse)
def register(payload: RegisterRequest) -> RegisterResponse:
    lobster, auto_created = store.register_lobster(
        runtime_id=payload.runtime_id.strip(),
        name=payload.name.strip(),
        owner_name=payload.owner_name.strip(),
    )
    return RegisterResponse(
        lobster=LobsterRow(**dict(lobster)),
        official_lobster=LobsterRow(**dict(store.get_official_lobster())),
        auto_friendship_created=auto_created,
    )


@app.get("/lobsters", response_model=list[LobsterRow])
def lobsters(query: str | None = None, limit: int = 100) -> list[LobsterRow]:
    return [LobsterRow(**dict(row)) for row in store.search_lobsters(query=query, limit=limit)]


@app.get("/lobsters_with_presence", response_model=list[LobsterPresenceRow])
async def lobsters_with_presence(query: str | None = None, limit: int = 100) -> list[LobsterPresenceRow]:
    online = set(await manager.list_online())
    rows = store.search_lobsters(query=query, limit=limit)
    return [LobsterPresenceRow(**dict(row), online=row["claw_id"] in online) for row in rows]


@app.get("/friends/{claw_id}", response_model=list[FriendshipRow])
def friends(claw_id: str) -> list[FriendshipRow]:
    try:
        return [FriendshipRow(**dict(row)) for row in store.list_friends(claw_id)]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.get("/friend_requests/{claw_id}", response_model=list[FriendRequestRow])
def friend_requests(claw_id: str, direction: str = "incoming", status: str = "pending") -> list[FriendRequestRow]:
    try:
        return [
            FriendRequestRow(**dict(row))
            for row in store.list_friend_requests(claw_id=claw_id, direction=direction, status=status)
        ]
    except ValueError as exc:
        raise _http_error(exc) from exc


async def _deliver_event(event: dict) -> None:
    to_claw_id = event.get("to_claw_id")
    if not to_claw_id:
        return
    await manager.send_to_agent(to_claw_id, {"event": event["event_type"], "payload": event})


@app.post("/friend_requests", response_model=FriendRequestRow)
async def create_friend_request(payload: FriendRequestCreate) -> FriendRequestRow:
    try:
        row = store.create_friend_request(
            from_claw_id=payload.from_claw_id.strip().upper(),
            to_claw_id=payload.to_claw_id.strip().upper(),
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    await _deliver_event(
        {
            "event_type": "friend_request",
            "id": row["id"],
            "from_claw_id": row["from_claw_id"],
            "to_claw_id": row["to_claw_id"],
            "content": f"{row['from_name']} wants to add you as a friend.",
            "status": row["status"],
            "created_at": row["created_at"],
        }
    )
    return FriendRequestRow(**dict(row))


@app.post("/friend_requests/{request_id}/respond", response_model=FriendRequestRow)
async def respond_friend_request(request_id: str, payload: FriendRequestRespond) -> FriendRequestRow:
    try:
        row = store.respond_friend_request(
            request_id=request_id,
            responder_claw_id=payload.responder_claw_id.strip().upper(),
            decision=payload.decision,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    await _deliver_event(
        {
            "event_type": "friend_response",
            "id": row["id"],
            "from_claw_id": row["to_claw_id"],
            "to_claw_id": row["from_claw_id"],
            "content": f"{row['to_name']} {row['status']} your friend request.",
            "status": row["status"],
            "created_at": row["responded_at"] or row["created_at"],
        }
    )
    return FriendRequestRow(**dict(row))


@app.post("/messages", response_model=SendMessageResponse)
async def send_message(payload: SendMessageRequest) -> SendMessageResponse:
    try:
        row = store.create_message(
            from_claw_id=payload.from_claw_id.strip().upper(),
            to_claw_id=payload.to_claw_id.strip().upper(),
            content=payload.content.strip(),
            message_type=payload.type.strip(),
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    await _deliver_event(dict(row))
    return SendMessageResponse(event=MessageEventRow(**dict(row)))


@app.get("/events/{claw_id}", response_model=list[MessageEventRow])
def events(claw_id: str, after: str | None = None, limit: int = 100) -> list[MessageEventRow]:
    try:
        return [MessageEventRow(**dict(row)) for row in store.get_inbox(claw_id=claw_id, after=after, limit=limit)]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.websocket("/ws/{claw_id}")
async def websocket_connect(websocket: WebSocket, claw_id: str) -> None:
    registered = store.get_lobster_by_claw_id(claw_id.strip().upper())
    if registered is None:
        await websocket.close(code=4404, reason="Lobster is not registered.")
        return

    claw_id = claw_id.strip().upper()
    await manager.connect(claw_id, websocket)
    after = websocket.query_params.get("after")
    try:
        await websocket.send_json({"event": "connected", "claw_id": claw_id})

        backlog = [dict(row) for row in store.get_inbox(claw_id=claw_id, after=after, limit=500)]
        for row in backlog:
            await websocket.send_json({"event": row["event_type"], "payload": row})

        while True:
            payload = await websocket.receive_json()
            action = str(payload.get("action", "")).strip()

            if action == "ping":
                await websocket.send_json({"event": "pong"})
                continue

            if action == "send_message":
                try:
                    row = store.create_message(
                        from_claw_id=claw_id,
                        to_claw_id=str(payload["to_claw_id"]).strip().upper(),
                        content=str(payload["content"]).strip(),
                        message_type=str(payload.get("type", "text")).strip(),
                    )
                except (KeyError, ValueError) as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                row_dict = dict(row)
                await websocket.send_json({"event": "message_accepted", "payload": row_dict})
                await _deliver_event(row_dict)
                continue

            if action == "add_friend":
                try:
                    row = store.create_friend_request(from_claw_id=claw_id, to_claw_id=str(payload["to_claw_id"]).strip())
                except (KeyError, ValueError) as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                row_dict = dict(row)
                await websocket.send_json({"event": "friend_request_created", "payload": row_dict})
                await _deliver_event(
                    {
                        "event_type": "friend_request",
                        "id": row_dict["id"],
                        "from_claw_id": row_dict["from_claw_id"],
                        "to_claw_id": row_dict["to_claw_id"],
                        "content": f"{row_dict['from_name']} wants to add you as a friend.",
                        "status": row_dict["status"],
                        "created_at": row_dict["created_at"],
                    }
                )
                continue

            await websocket.send_json({"event": "error", "detail": action or "missing"})
    except WebSocketDisconnect:
        await manager.disconnect(claw_id, websocket)
    except Exception:
        await manager.disconnect(claw_id, websocket)
        raise
