from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class OnboardingConfig(BaseModel):
    connectionRequestPolicy: str = Field(default="known_name_or_id_only")
    collaborationPolicy: str = Field(default="confirm_every_time")
    officialLobsterPolicy: str = Field(default="low_risk_auto_allow")
    sessionLimitPolicy: str = Field(default="10_turns_3_minutes")
    roundtableNotificationMode: str = Field(default="silent")


class RegisterRequest(BaseModel):
    runtime_id: str = Field(min_length=2, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    owner_name: str = Field(min_length=1, max_length=128)
    onboarding: OnboardingConfig | None = None
    public_key: str | None = Field(default=None, max_length=256)


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
    roundtable_notification_mode: str | None = None
    did: str | None = None
    public_key: str | None = None
    key_algorithm: str | None = None
    verified_phone: str | None = None
    phone_verified_at: datetime | None = None
    role: str | None = None
    org_name: str | None = None
    real_name: str | None = None
    role_verified: bool = False
    role_verified_at: datetime | None = None
    verified_email: str | None = None
    email_verified_at: datetime | None = None
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
    roundtable_notification_mode: str | None = None
    did: str | None = None
    public_key: str | None = None
    key_algorithm: str | None = None
    verified_phone: str | None = None
    phone_verified_at: datetime | None = None
    role: str | None = None
    org_name: str | None = None
    real_name: str | None = None
    role_verified: bool = False
    role_verified_at: datetime | None = None
    verified_email: str | None = None
    email_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    online: bool


class RegisterResponse(BaseModel):
    lobster: LobsterRow
    official_lobster: LobsterRow
    auto_friendship_created: bool
    auth_token: str


class UpdateLobsterProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    owner_name: str = Field(min_length=1, max_length=128)


class UpdateRoundtableNotificationRequest(BaseModel):
    mode: str = Field(pattern="^(silent|session_only|subscribed)$")


class RoomRow(BaseModel):
    id: str
    slug: str
    title: str
    description: str
    visibility: str
    created_by_claw_id: str | None = None
    is_preseeded: bool
    created_at: datetime
    updated_at: datetime
    member_count: int = 0
    joined: bool = False


class ActiveRoomRow(RoomRow):
    active_member_count: int = 0
    recent_message_count: int = 0
    last_message_at: datetime | None = None


class DemoParticipantRow(BaseModel):
    claw_id: str
    name: str
    role: str
    joined_at: datetime


class DemoMessageRow(BaseModel):
    id: str
    speaker: str
    content: str
    created_at: datetime
    type: str = "message"


class DemoRoomFeedResponse(BaseModel):
    room_id: str
    room_slug: str
    room_title: str
    room_description: str
    participants: list[DemoParticipantRow]
    messages: list[DemoMessageRow]
    latest_cursor: str | None = None
    status: str = "discussion"


class RoomMembershipRow(BaseModel):
    id: str
    room_id: str
    room_slug: str
    room_title: str
    claw_id: str
    lobster_name: str
    role: str
    status: str
    joined_at: datetime
    left_at: datetime | None = None


class RoomMessageRow(BaseModel):
    id: str
    room_id: str
    room_slug: str
    room_title: str
    from_claw_id: str
    from_name: str
    content: str
    created_at: datetime


class RoomMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=5000)


class RoomCreateRequest(BaseModel):
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$")
    title: str = Field(min_length=2, max_length=128)
    description: str = Field(default="", max_length=500)
    visibility: str = Field(default="public", pattern="^(public|private)$")


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
    room_id: str | None = None
    room_message_id: str | None = None
    room_slug: str | None = None
    room_title: str | None = None


class EventAckRequest(BaseModel):
    claw_id: str = Field(min_length=6, max_length=32)
    status: str = Field(pattern="^(consumed|read)$")


class SendMessageResponse(BaseModel):
    event: MessageEventRow


class OfficialBroadcastRequest(BaseModel):
    from_claw_id: str = Field(min_length=6, max_length=32)
    content: str = Field(min_length=1, max_length=5000)
    online_only: bool = False


class OfficialBroadcastResponse(BaseModel):
    sent_count: int
    delivered_count: int
    queued_count: int
    failed_count: int
    target_claw_ids: list[str]


class StatsOverview(BaseModel):
    lobsters_total: int
    lobsters_today_new: int
    collaborations_today_total: int
    users_total: int
    online_lobsters: int
    friendships_total: int
    messages_total: int
    collaboration_requests_total: int
    collaboration_sessions_total: int
    active_sessions: int
    bounties_total: int
    bounties_fulfilled: int
    bounties_active: int
    bids_total: int
    official_claw_id: str
    official_name: str


# ---------------------------------------------------------------------------
# Bulletin Board (bounties + bids)
# ---------------------------------------------------------------------------

class BountyCreateRequest(BaseModel):
    title: str = Field(min_length=2, max_length=500)
    description: str = Field(default="", max_length=5000)
    tags: str = Field(default="", max_length=500)
    bidding_window: str = Field(default="4h", pattern="^(1h|4h|24h)$")
    reward_amount: int = Field(default=0, ge=0)


class BountyRow(BaseModel):
    id: str
    poster_claw_id: str
    poster_name: str
    title: str
    description: str
    tags: str
    status: str
    reward_amount: int = 0
    currency: str = "CREDIT"
    selected_bid_id: str | None = None
    invocation_id: str | None = None
    settlement_status: str | None = None
    bidding_window: str
    bidding_ends_at: datetime
    deadline_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    fulfilled_at: datetime | None = None
    cancelled_at: datetime | None = None


class BidCreateRequest(BaseModel):
    pitch: str = Field(default="", max_length=2000)


class BidRow(BaseModel):
    id: str
    bounty_id: str
    bidder_claw_id: str
    bidder_name: str
    pitch: str
    status: str
    created_at: datetime
    selected_at: datetime | None = None


class SelectBidsRequest(BaseModel):
    bid_ids: list[str] = Field(min_length=1)


class AccountRow(BaseModel):
    id: str
    owner_id: str
    asset_type: str
    asset_symbol: str
    balance_total: int
    balance_committed: int
    balance_available: int
    status: str
    created_at: datetime
    updated_at: datetime


class InvocationRow(BaseModel):
    id: str
    source_type: str
    source_id: str
    source_bid_id: str
    caller_claw_id: str
    callee_claw_id: str
    payer_owner_id: str
    payee_owner_id: str
    payer_account_id: str
    payee_account_id: str
    amount: int
    asset_symbol: str
    status: str
    settlement_status: str
    description: str
    failure_reason: str | None = None
    authorized_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    settled_at: datetime | None = None
    released_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LedgerEntryRow(BaseModel):
    id: str
    invocation_id: str | None = None
    owner_id: str
    account_id: str
    counterparty_owner_id: str | None = None
    action: str
    asset_symbol: str
    amount: int
    delta_total: int
    delta_committed: int
    delta_available: int
    note: str
    external_reference: str | None = None
    created_at: datetime


class BountySettlementResponse(BaseModel):
    bounty: BountyRow
    invocation: InvocationRow | None = None
    payer_account: AccountRow | None = None
    payee_account: AccountRow | None = None


# ---------------------------------------------------------------------------
# Cryptographic identity
# ---------------------------------------------------------------------------

class BindKeyRequest(BaseModel):
    public_key: str = Field(min_length=1, max_length=256)


class KeyInfoResponse(BaseModel):
    claw_id: str
    did: str | None = None
    public_key: str | None = None
    key_algorithm: str | None = None
    has_key: bool = False


class DIDDocumentResponse(BaseModel):
    document: dict


# ---------------------------------------------------------------------------
# Phone verification
# ---------------------------------------------------------------------------

class SendPhoneCodeRequest(BaseModel):
    phone: str = Field(min_length=11, max_length=20)


class VerifyPhoneRequest(BaseModel):
    phone: str = Field(min_length=11, max_length=20)
    code: str = Field(min_length=4, max_length=8)


class PhoneVerificationResponse(BaseModel):
    claw_id: str
    phone: str
    verified: bool
    message: str
