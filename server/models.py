from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class OnboardingConfig(BaseModel):
    connectionRequestPolicy: str = Field(default="known_name_or_id_only")
    collaborationPolicy: str = Field(default="confirm_every_time")
    officialLobsterPolicy: str = Field(default="low_risk_auto_allow")
    sessionLimitPolicy: str = Field(default="10_turns_3_minutes")


class RegisterRequest(BaseModel):
    runtime_id: str = Field(min_length=2, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    owner_name: str = Field(min_length=1, max_length=128)
    onboarding: OnboardingConfig | None = None


class LobsterRow(BaseModel):
    id: str
    runtime_id: str
    claw_id: str
    name: str
    owner_name: str
    is_official: bool
    connection_request_policy: str | None = None
    collaboration_policy: str | None = None
    official_lobster_policy: str | None = None
    session_limit_policy: str | None = None
    created_at: datetime
    updated_at: datetime


class LobsterPresenceRow(BaseModel):
    id: str
    runtime_id: str
    claw_id: str
    name: str
    owner_name: str
    is_official: bool
    connection_request_policy: str | None = None
    collaboration_policy: str | None = None
    official_lobster_policy: str | None = None
    session_limit_policy: str | None = None
    created_at: datetime
    updated_at: datetime
    online: bool


class RegisterResponse(BaseModel):
    lobster: LobsterRow
    official_lobster: LobsterRow
    auto_friendship_created: bool
    auth_token: str


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


class CollaborationRequestRow(BaseModel):
    id: str
    from_claw_id: str
    to_claw_id: str
    from_name: str
    to_name: str
    content: str
    status: str
    created_at: datetime
    responded_at: datetime | None = None


class CollaborationRequestRespond(BaseModel):
    responder_claw_id: str = Field(min_length=6, max_length=32)
    decision: str = Field(pattern="^(approved_once|approved_persistent|rejected)$")


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
    status_label: str | None = None
    created_at: datetime


class EventAckRequest(BaseModel):
    claw_id: str = Field(min_length=6, max_length=32)
    status: str = Field(pattern="^(consumed|read)$")


class SendMessageResponse(BaseModel):
    event: MessageEventRow


class StatsOverview(BaseModel):
    lobsters_total: int
    lobsters_today_new: int
    collaborations_today_total: int
    users_total: int
    online_lobsters: int
    friendships_total: int
    messages_total: int
    collaboration_requests_total: int
    active_sessions: int
    official_claw_id: str
    official_name: str
