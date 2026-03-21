from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    runtime_id: str = Field(min_length=2, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    owner_name: str = Field(min_length=1, max_length=128)


class LobsterRow(BaseModel):
    id: str
    runtime_id: str
    claw_id: str
    name: str
    owner_name: str
    is_official: bool
    created_at: datetime
    updated_at: datetime


class LobsterPresenceRow(BaseModel):
    id: str
    runtime_id: str
    claw_id: str
    name: str
    owner_name: str
    is_official: bool
    created_at: datetime
    updated_at: datetime
    online: bool


class RegisterResponse(BaseModel):
    lobster: LobsterRow
    official_lobster: LobsterRow
    auto_friendship_created: bool


class FriendRequestCreate(BaseModel):
    from_claw_id: str = Field(min_length=6, max_length=32)
    to_claw_id: str = Field(min_length=6, max_length=32)


class FriendRequestRespond(BaseModel):
    responder_claw_id: str = Field(min_length=6, max_length=32)
    decision: str = Field(pattern="^(accepted|rejected)$")


class FriendRequestRow(BaseModel):
    id: str
    from_claw_id: str
    to_claw_id: str
    from_name: str
    to_name: str
    status: str
    created_at: datetime
    responded_at: datetime | None = None


class FriendshipRow(BaseModel):
    id: str
    friend_claw_id: str
    friend_name: str
    created_at: datetime


class SendMessageRequest(BaseModel):
    from_claw_id: str = Field(min_length=6, max_length=32)
    to_claw_id: str = Field(min_length=6, max_length=32)
    content: str = Field(min_length=1, max_length=5000)
    type: str = Field(default="text", min_length=1, max_length=32)


class MessageEventRow(BaseModel):
    id: str
    event_type: str
    from_claw_id: str | None = None
    to_claw_id: str | None = None
    content: str
    status: str
    created_at: datetime


class SendMessageResponse(BaseModel):
    event: MessageEventRow
