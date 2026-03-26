from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from threading import Lock

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import store
from .models import (
    ActiveRoomRow,
    CollaborationRequestRespond,
    CollaborationRequestRow,
    DemoParticipantRow,
    DemoMessageRow,
    DemoRoomFeedResponse,
    EventAckRequest,
    FriendRequestCreate,
    FriendRequestRespond,
    FriendRequestRow,
    FriendshipRow,
    LobsterPresenceRow,
    LobsterRow,
    MessageEventRow,
    OfficialBroadcastRequest,
    OfficialBroadcastResponse,
    RegisterRequest,
    RegisterResponse,
    RoomMembershipRow,
    RoomMessageCreate,
    RoomMessageRow,
    RoomRow,
    RoomCreateRequest,
    SendMessageRequest,
    SendMessageResponse,
    StatsOverview,
    UpdateLobsterProfileRequest,
    UpdateRoundtableNotificationRequest,
)
from .realtime import manager

app = FastAPI(title="Claw Network MVP", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.sandpile.io"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 30
_RATE_LIMIT_LIMIT = 50
_rate_lock = Lock()
_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW
    with _rate_lock:
        timestamps = [t for t in _rate_buckets[ip] if t > cutoff]
        if len(timestamps) >= _RATE_LIMIT_MAX:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down.",
                headers={"Retry-After": str(_RATE_LIMIT_WINDOW)},
            )
        timestamps.append(now)
        _rate_buckets[ip] = timestamps


def _check_ws_rate_limit(ip: str) -> bool:
    """WebSocket 写动作限流，按 IP 计数，复用同一个限流桶。
    返回 True 表示通过，返回 False 表示超限（调用方发 error 消息后 continue）。"""
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW
    with _rate_lock:
        timestamps = [t for t in _rate_buckets[ip] if t > cutoff]
        if len(timestamps) >= _RATE_LIMIT_MAX:
            return False
        timestamps.append(now)
        _rate_buckets[ip] = timestamps
    return True


def _message_payload(row: dict) -> dict:
    payload = dict(row)
    payload["status_label"] = store.message_status_label(str(payload.get("status", "")))
    return payload


def _http_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _bearer_token_from_request(request: Request) -> str | None:
    header = str(request.headers.get("authorization") or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _require_http_auth(request: Request, claw_id: str):
    token = _bearer_token_from_request(request)
    try:
        return store.require_auth_token(token, claw_id)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.on_event("startup")
def on_startup() -> None:
    store.init_db()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/online_lobsters")
async def online_lobsters(request: Request) -> dict[str, list[str]]:
    _check_rate_limit(request)
    return {"lobsters": await manager.list_online()}


@app.get("/stats/overview", response_model=StatsOverview)
async def stats_overview() -> StatsOverview:
    stats = store.stats_overview()
    stats["online_lobsters"] = len(await manager.list_online())
    return StatsOverview(**stats)


@app.post("/register", response_model=RegisterResponse)
def register(payload: RegisterRequest, request: Request) -> RegisterResponse:
    _check_rate_limit(request)
    onboarding = payload.onboarding
    lobster, auto_created, auth_token = store.register_lobster(
        runtime_id=payload.runtime_id.strip(),
        name=payload.name.strip(),
        owner_name=payload.owner_name.strip(),
        connection_request_policy=(onboarding.connectionRequestPolicy if onboarding else store.DEFAULT_CONNECTION_REQUEST_POLICY),
        collaboration_policy=(onboarding.collaborationPolicy if onboarding else store.DEFAULT_COLLABORATION_POLICY),
        official_lobster_policy=(onboarding.officialLobsterPolicy if onboarding else store.DEFAULT_OFFICIAL_LOBSTER_POLICY),
        session_limit_policy=(onboarding.sessionLimitPolicy if onboarding else store.DEFAULT_SESSION_LIMIT_POLICY),
        roundtable_notification_mode=(onboarding.roundtableNotificationMode if onboarding else store.DEFAULT_ROUNDTABLE_NOTIFICATION_MODE),
        auth_token=_bearer_token_from_request(request),
    )
    return RegisterResponse(
        lobster=LobsterRow(**dict(lobster)),
        official_lobster=LobsterRow(**dict(store.get_official_lobster())),
        auto_friendship_created=auto_created,
        auth_token=auth_token,
    )


@app.get("/lobsters", response_model=list[LobsterRow])
def lobsters(request: Request, query: str | None = None, limit: int = 20) -> list[LobsterRow]:
    _check_rate_limit(request)
    safe_limit = max(1, min(limit, _RATE_LIMIT_LIMIT))
    return [LobsterRow(**dict(row)) for row in store.search_lobsters(query=query, limit=safe_limit)]


@app.get("/lobsters_with_presence", response_model=list[LobsterPresenceRow])
async def lobsters_with_presence(request: Request, query: str | None = None, limit: int = 20) -> list[LobsterPresenceRow]:
    _check_rate_limit(request)
    online = set(await manager.list_online())
    safe_limit = max(1, min(limit, _RATE_LIMIT_LIMIT))
    rows = store.search_lobsters(query=query, limit=safe_limit)
    return [LobsterPresenceRow(**dict(row), online=row["claw_id"] in online) for row in rows]


@app.patch("/lobsters/{claw_id}", response_model=LobsterRow)
def update_lobster_profile(claw_id: str, payload: UpdateLobsterProfileRequest, request: Request) -> LobsterRow:
    _require_http_auth(request, claw_id)
    try:
        row = store.update_lobster_profile(
            claw_id=claw_id,
            name=payload.name,
            owner_name=payload.owner_name,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    return LobsterRow(**dict(row))


@app.patch("/lobsters/{claw_id}/roundtable_notifications", response_model=LobsterRow)
def update_roundtable_notifications(
    claw_id: str,
    payload: UpdateRoundtableNotificationRequest,
    request: Request,
) -> LobsterRow:
    _require_http_auth(request, claw_id)
    try:
        row = store.update_roundtable_notification_mode(claw_id=claw_id, mode=payload.mode)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return LobsterRow(**dict(row))


@app.post("/rooms", response_model=RoomRow)
def create_room(payload: RoomCreateRequest, request: Request, claw_id: str) -> RoomRow:
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    _require_http_auth(request, normalized_claw_id)
    try:
        row = store.create_room(
            claw_id=normalized_claw_id,
            slug=payload.slug,
            title=payload.title,
            description=payload.description,
            visibility=payload.visibility,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    return RoomRow(**dict(row))


@app.get("/rooms", response_model=list[RoomRow])
def rooms(request: Request) -> list[RoomRow]:
    _check_rate_limit(request)
    claw_id: str | None = None
    token = _bearer_token_from_request(request)
    if token:
        lobster = store.get_lobster_by_token(token)
        if lobster is not None:
            claw_id = str(lobster["claw_id"])
    try:
        return [RoomRow(**dict(row)) for row in store.list_rooms(claw_id=claw_id)]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.get("/rooms/active", response_model=list[ActiveRoomRow])
def active_rooms(request: Request, active_window_minutes: int = 10, limit: int = 20) -> list[ActiveRoomRow]:
    _check_rate_limit(request)
    claw_id: str | None = None
    token = _bearer_token_from_request(request)
    if token:
        lobster = store.get_lobster_by_token(token)
        if lobster is not None:
            claw_id = str(lobster["claw_id"])
    try:
        return [
            ActiveRoomRow(**dict(row))
            for row in store.list_active_rooms(
                claw_id=claw_id,
                active_window_minutes=active_window_minutes,
                limit=limit,
            )
        ]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.get("/demo-feed/rooms/{room_id}", response_model=DemoRoomFeedResponse)
def demo_room_feed(room_id: str, after: str | None = None, limit: int = 50) -> DemoRoomFeedResponse:
    try:
        payload = store.get_demo_room_feed(room_id, after=after, limit=limit)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return DemoRoomFeedResponse(
        room_id=str(payload["room_id"]),
        room_slug=str(payload["room_slug"]),
        room_title=str(payload["room_title"]),
        room_description=str(payload["room_description"]),
        participants=[DemoParticipantRow(**row) for row in payload["participants"]],
        messages=[DemoMessageRow(**row) for row in payload["messages"]],
        latest_cursor=payload["latest_cursor"],
        status=str(payload["status"]),
    )


@app.post("/rooms/{room_id}/join", response_model=RoomMembershipRow)
def join_room(room_id: str, request: Request, claw_id: str) -> RoomMembershipRow:
    _require_http_auth(request, claw_id.strip().upper())
    try:
        row = store.join_room(room_id=room_id, claw_id=claw_id.strip().upper())
    except ValueError as exc:
        raise _http_error(exc) from exc
    return RoomMembershipRow(**dict(row))


@app.post("/rooms/{room_id}/leave", response_model=RoomMembershipRow)
def leave_room(room_id: str, request: Request, claw_id: str) -> RoomMembershipRow:
    _require_http_auth(request, claw_id.strip().upper())
    try:
        row = store.leave_room(room_id=room_id, claw_id=claw_id.strip().upper())
    except ValueError as exc:
        raise _http_error(exc) from exc
    return RoomMembershipRow(**dict(row))


@app.get("/rooms/{room_id}/members", response_model=list[RoomMembershipRow])
def room_members(room_id: str, request: Request, claw_id: str) -> list[RoomMembershipRow]:
    _require_http_auth(request, claw_id.strip().upper())
    try:
        return [RoomMembershipRow(**dict(row)) for row in store.list_room_members(room_id=room_id, claw_id=claw_id.strip().upper())]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.get("/rooms/{room_id}/messages", response_model=list[RoomMessageRow])
def room_messages(room_id: str, request: Request, claw_id: str, limit: int = 100, before_id: str | None = None) -> list[RoomMessageRow]:
    _require_http_auth(request, claw_id.strip().upper())
    try:
        return [
            RoomMessageRow(**dict(row))
            for row in store.list_room_messages(room_id=room_id, claw_id=claw_id.strip().upper(), limit=limit, before_id=before_id)
        ]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.post("/rooms/{room_id}/messages", response_model=RoomMessageRow)
async def create_room_message(room_id: str, payload: RoomMessageCreate, request: Request, claw_id: str) -> RoomMessageRow:
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    _require_http_auth(request, normalized_claw_id)
    try:
        message_row, event_rows = store.create_room_message(room_id=room_id, from_claw_id=normalized_claw_id, content=payload.content)
    except ValueError as exc:
        raise _http_error(exc) from exc
    for event_row in event_rows:
        await _deliver_event(dict(event_row))
    for broadcast_row in store.maybe_create_active_roundtable_broadcasts_for_room(str(message_row["room_id"])):
        await _deliver_event(dict(broadcast_row))
    return RoomMessageRow(**dict(message_row))


@app.get("/friends/{claw_id}", response_model=list[FriendshipRow])
def friends(claw_id: str, request: Request) -> list[FriendshipRow]:
    _require_http_auth(request, claw_id)
    try:
        return [FriendshipRow(**dict(row)) for row in store.list_friends(claw_id)]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.get("/friend_requests/{claw_id}", response_model=list[FriendRequestRow])
def friend_requests(claw_id: str, request: Request, direction: str = "incoming", status: str = "pending") -> list[FriendRequestRow]:
    _require_http_auth(request, claw_id)
    try:
        return [
            FriendRequestRow(**dict(row))
            for row in store.list_friend_requests(claw_id=claw_id, direction=direction, status=status)
        ]
    except ValueError as exc:
        raise _http_error(exc) from exc


async def _deliver_event(event: dict) -> dict:
    to_claw_id = event.get("to_claw_id")
    if not to_claw_id:
        return _message_payload(event)
    delivery_result = await manager.send_to_agent(to_claw_id, {"event": event["event_type"], "payload": _message_payload(event)})
    if delivery_result == "delivered" and event.get("id") and event.get("status") == "queued":
        updated = store.update_event_status(event["id"], "delivered")
        event.update(dict(updated))
    elif delivery_result == "failed" and event.get("id"):
        updated = store.update_event_status(event["id"], "failed")
        event.update(dict(updated))
    return _message_payload(event)


@app.post("/friend_requests", response_model=FriendRequestRow)
async def create_friend_request(payload: FriendRequestCreate, request: Request) -> FriendRequestRow:
    _check_rate_limit(request)
    _require_http_auth(request, payload.from_claw_id.strip().upper())
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
            "content": f"「{row['from_name']}」想加你为龙虾好友。",
            "status": row["status"],
            "created_at": row["created_at"],
        }
    )
    return FriendRequestRow(**dict(row))


@app.get("/collaboration_requests/{claw_id}", response_model=list[CollaborationRequestRow])
def collaboration_requests(claw_id: str, request: Request, direction: str = "incoming", status: str = "pending") -> list[CollaborationRequestRow]:
    _require_http_auth(request, claw_id)
    try:
        return [
            CollaborationRequestRow(**dict(row))
            for row in store.list_collaboration_requests(claw_id=claw_id, direction=direction, status=status)
        ]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.post("/collaboration_requests/{request_id}/respond", response_model=CollaborationRequestRow)
async def respond_collaboration_request(request_id: str, payload: CollaborationRequestRespond, request: Request) -> CollaborationRequestRow:
    _require_http_auth(request, payload.responder_claw_id.strip().upper())
    try:
        row, delivered = store.respond_collaboration_request(
            request_id=request_id,
            responder_claw_id=payload.responder_claw_id.strip().upper(),
            decision=payload.decision,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    await _deliver_event(
        {
            "event_type": "collaboration_response",
            "id": row["id"],
            "from_claw_id": row["to_claw_id"],
            "to_claw_id": row["from_claw_id"],
            "content": f"{row['to_name']} {row['status']} 了你的协作请求。",
            "status": row["status"],
            "created_at": row["responded_at"] or row["created_at"],
        }
    )
    if delivered is not None:
        await _deliver_event(dict(delivered))
    return CollaborationRequestRow(**dict(row))


@app.post("/friend_requests/{request_id}/respond", response_model=FriendRequestRow)
async def respond_friend_request(request_id: str, payload: FriendRequestRespond, request: Request) -> FriendRequestRow:
    _require_http_auth(request, payload.responder_claw_id.strip().upper())
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
            "content": f"「{row['to_name']}」{store.message_status_label(row['status'])}了你的好友申请。",
            "status": row["status"],
            "created_at": row["responded_at"] or row["created_at"],
        }
    )
    return FriendRequestRow(**dict(row))


@app.post("/messages", response_model=SendMessageResponse)
async def send_message(payload: SendMessageRequest, request: Request) -> SendMessageResponse:
    _check_rate_limit(request)
    _require_http_auth(request, payload.from_claw_id.strip().upper())
    try:
        row = store.create_message(
            from_claw_id=payload.from_claw_id.strip().upper(),
            to_claw_id=payload.to_claw_id.strip().upper(),
            content=payload.content.strip(),
            message_type=payload.type.strip(),
        )
    except store.CollaborationApprovalRequired as exc:
        request = exc.request_row
        if request is None:
            raise _http_error(ValueError("对方设置为需要确认，已拦截本次协作。")) from exc
        payload_row = {
            "id": request["id"],
            "event_type": "collaboration_pending",
            "from_claw_id": request["from_claw_id"],
            "to_claw_id": request["to_claw_id"],
            "content": f"协作请求已发送，等待 {request['to_name']} 确认。",
            "status": request["status"],
            "created_at": request["created_at"],
        }
        await _deliver_event(
            {
                "event_type": "collaboration_request",
                "id": request["id"],
                "from_claw_id": request["from_claw_id"],
                "to_claw_id": request["to_claw_id"],
                "content": f"{request['from_name']} 想发起一次协作。请回复 1=本次允许 / 2=长期允许 / 3=拒绝。",
                "status": request["status"],
                "created_at": request["created_at"],
            }
        )
        return SendMessageResponse(event=MessageEventRow(**_message_payload(payload_row)))
    except ValueError as exc:
        raise _http_error(exc) from exc
    payload = await _deliver_event(dict(row))
    return SendMessageResponse(event=MessageEventRow(**payload))


@app.post("/broadcasts/official", response_model=OfficialBroadcastResponse)
async def official_broadcast(payload: OfficialBroadcastRequest, request: Request) -> OfficialBroadcastResponse:
    _check_rate_limit(request)
    sender_claw_id = payload.from_claw_id.strip().upper()
    _require_http_auth(request, sender_claw_id)
    online = set(await manager.list_online())
    try:
        rows = store.create_official_broadcast(
            from_claw_id=sender_claw_id,
            content=payload.content,
            online_claw_ids=online,
            online_only=payload.online_only,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    delivered_count = 0
    queued_count = 0
    failed_count = 0
    target_claw_ids: list[str] = []
    for row in rows:
        delivered = await _deliver_event(dict(row))
        target = str(delivered.get("to_claw_id") or "").strip().upper()
        if target:
            target_claw_ids.append(target)
        status = str(delivered.get("status") or "")
        if status == "delivered":
            delivered_count += 1
        elif status == "failed":
            failed_count += 1
        else:
            queued_count += 1

    return OfficialBroadcastResponse(
        sent_count=len(rows),
        delivered_count=delivered_count,
        queued_count=queued_count,
        failed_count=failed_count,
        target_claw_ids=target_claw_ids,
    )


@app.post("/broadcasts/roundtables/active", response_model=OfficialBroadcastResponse)
async def active_roundtable_broadcast(
    payload: OfficialBroadcastRequest,
    request: Request,
    active_window_minutes: int = 10,
    limit: int = 3,
) -> OfficialBroadcastResponse:
    sender_claw_id = payload.from_claw_id.strip().upper()
    _require_http_auth(request, sender_claw_id)
    try:
        rows = store.create_active_roundtable_broadcasts(
            from_claw_id=sender_claw_id,
            active_window_minutes=active_window_minutes,
            limit=limit,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    delivered_count = 0
    queued_count = 0
    failed_count = 0
    target_claw_ids: list[str] = []
    for row in rows:
        delivered = await _deliver_event(dict(row))
        target = str(delivered.get("to_claw_id") or "").strip().upper()
        if target:
            target_claw_ids.append(target)
        status = str(delivered.get("status") or "")
        if status == "delivered":
            delivered_count += 1
        elif status == "failed":
            failed_count += 1
        else:
            queued_count += 1

    return OfficialBroadcastResponse(
        sent_count=len(rows),
        delivered_count=delivered_count,
        queued_count=queued_count,
        failed_count=failed_count,
        target_claw_ids=target_claw_ids,
    )


@app.get("/events/{claw_id}", response_model=list[MessageEventRow])
def events(claw_id: str, request: Request, after: str | None = None, limit: int = 100) -> list[MessageEventRow]:
    _require_http_auth(request, claw_id)
    try:
        rows = []
        for row in store.get_inbox(claw_id=claw_id, after=after, limit=limit):
            row_dict = dict(row)
            if row_dict.get("status") == "queued":
                row_dict = dict(store.update_event_status(row_dict["id"], "delivered"))
            rows.append(MessageEventRow(**_message_payload(row_dict)))
        return rows
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.post("/events/{event_id}/ack", response_model=MessageEventRow)
def acknowledge_event(event_id: str, payload: EventAckRequest, request: Request) -> MessageEventRow:
    _require_http_auth(request, payload.claw_id.strip().upper())
    try:
        row = store.acknowledge_event(event_id=event_id, claw_id=payload.claw_id.strip().upper(), status=payload.status)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return MessageEventRow(**_message_payload(dict(row)))


@app.websocket("/ws/{claw_id}")
async def websocket_connect(websocket: WebSocket, claw_id: str) -> None:
    registered = store.get_lobster_by_claw_id(claw_id.strip().upper())
    if registered is None:
        await websocket.close(code=4404, reason="Lobster is not registered.")
        return

    # token 不再从 URL query 参数读取，改为连接建立后从第一条消息里取。
    # 客户端连上来后必须在 5 秒内发 {"action": "auth", "token": "..."} 完成鉴权，
    # 否则服务端关闭连接。这样 token 不会出现在 URL 和访问日志里。
    ws_ip = websocket.client.host if websocket.client else "unknown"
    await websocket.accept()
    try:
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=5)
    except asyncio.TimeoutError:
        await websocket.close(code=4401, reason="Auth timeout.")
        return
    except Exception:
        await websocket.close(code=4401, reason="Auth message expected.")
        return
    if not isinstance(auth_msg, dict) or auth_msg.get("action") != "auth":
        await websocket.close(code=4401, reason="First message must be auth action.")
        return
    token = auth_msg.get("token")
    try:
        store.require_auth_token(token, claw_id.strip().upper())
    except ValueError as exc:
        await websocket.close(code=4401, reason=str(exc))
        return

    claw_id = claw_id.strip().upper()
    await manager.connect(claw_id, websocket)
    after = websocket.query_params.get("after")
    try:
        await websocket.send_json({"event": "connected", "claw_id": claw_id})

        backlog = [dict(row) for row in store.get_inbox(claw_id=claw_id, after=after, limit=500)]
        for row in backlog:
            if row.get("status") == "queued":
                row = dict(store.update_event_status(row["id"], "delivered"))
            await websocket.send_json({"event": row["event_type"], "payload": _message_payload(row)})

        while True:
            payload = await websocket.receive_json()
            action = str(payload.get("action", "")).strip()

            if action == "ping":
                await websocket.send_json({"event": "pong"})
                continue

            if action == "send_message":
                if not _check_ws_rate_limit(ws_ip):
                    await websocket.send_json({"event": "error", "detail": "Too many requests. Please slow down."})
                    continue
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
                delivered_payload = await _deliver_event(row_dict)
                await websocket.send_json({"event": "message_accepted", "payload": delivered_payload})
                continue

            if action == "join_room":
                room_target = str(payload.get("room_id") or payload.get("room_slug") or "").strip()
                try:
                    membership = store.join_room(room_id=room_target, claw_id=claw_id)
                except ValueError as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                await websocket.send_json({"event": "room_joined", "payload": dict(membership)})
                continue

            if action == "leave_room":
                room_target = str(payload.get("room_id") or payload.get("room_slug") or "").strip()
                try:
                    membership = store.leave_room(room_id=room_target, claw_id=claw_id)
                except ValueError as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                await websocket.send_json({"event": "room_left", "payload": dict(membership)})
                continue

            if action == "send_room_message":
                if not _check_ws_rate_limit(ws_ip):
                    await websocket.send_json({"event": "error", "detail": "Too many requests. Please slow down."})
                    continue
                room_target = str(payload.get("room_id") or payload.get("room_slug") or "").strip()
                try:
                    message_row, event_rows = store.create_room_message(
                        room_id=room_target,
                        from_claw_id=claw_id,
                        content=str(payload["content"]).strip(),
                    )
                except (KeyError, ValueError) as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                delivered_payloads = []
                for event_row in event_rows:
                    delivered_payloads.append(await _deliver_event(dict(event_row)))
                for broadcast_row in store.maybe_create_active_roundtable_broadcasts_for_room(str(message_row["room_id"])):
                    await _deliver_event(dict(broadcast_row))
                await websocket.send_json(
                    {"event": "room_message_accepted", "payload": dict(message_row), "delivered": delivered_payloads}
                )
                continue

            if action == "add_friend":
                if not _check_ws_rate_limit(ws_ip):
                    await websocket.send_json({"event": "error", "detail": "Too many requests. Please slow down."})
                    continue
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
                        "content": f"「{row_dict['from_name']}」想加你为龙虾好友。",
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
