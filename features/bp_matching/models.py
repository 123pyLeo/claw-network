"""Data models for BP matching (founder-investor matchmaking)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BPListingCreateRequest(BaseModel):
    project_name: str = Field(min_length=1, max_length=200)
    sector: str = Field(default="", max_length=200)          # e.g. "AI,B2B SaaS"
    stage: str = Field(default="", max_length=50)             # pre-seed / seed / A / B
    funding_ask: int | None = None
    currency: str = Field(default="CNY", max_length=10)
    one_liner: str = Field(min_length=1, max_length=500)      # one-line pitch
    team_size: int | None = None
    access_policy: str = Field(default="manual", pattern="^(manual|open)$")
    expires_in_days: int | None = Field(default=90, ge=1, le=365)

    # Structured BP content (Phase 1 — pure text, sidecar extracts locally)
    problem: str = Field(default="", max_length=2000)
    solution: str = Field(default="", max_length=3000)
    team_intro: str = Field(default="", max_length=2000)
    traction: str = Field(default="", max_length=1500)
    business_model: str = Field(default="", max_length=1500)
    ask_note: str = Field(default="", max_length=1000)


class BPListingSummaryRow(BaseModel):
    """Public-safe view of a BP. Returned by list endpoint and by detail
    endpoint when caller is NOT yet approved on this listing. Excludes the
    structured "deep" fields (problem/solution/team_intro/traction/
    business_model/ask_note) — those are the actual pitch content, gated
    behind founder approval."""
    id: str
    founder_claw_id: str
    founder_name: str
    founder_org: str | None = None
    status: str
    access_policy: str
    project_name: str
    sector: str
    stage: str
    funding_ask: int | None = None
    currency: str
    one_liner: str
    team_size: int | None = None
    intent_count: int = 0
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None


class BPListingRow(BPListingSummaryRow):
    """Full BP detail. Only returned to authorized callers: the founder
    themselves, an investor whose intent on this listing is accepted /
    auto_accepted, or any caller for an open-policy listing. The route
    layer enforces the gate."""
    problem: str = ""
    solution: str = ""
    team_intro: str = ""
    traction: str = ""
    business_model: str = ""
    ask_note: str = ""


class BPListingUpdateRequest(BaseModel):
    access_policy: str | None = Field(default=None, pattern="^(manual|open)$")
    status: str | None = Field(default=None, pattern="^(active|closed)$")
    # Editable content fields. Omit (or pass None) to leave unchanged.
    project_name: str | None = Field(default=None, min_length=1, max_length=200)
    one_liner: str | None = Field(default=None, min_length=1, max_length=500)
    sector: str | None = Field(default=None, max_length=200)
    stage: str | None = Field(default=None, max_length=50)
    funding_ask: int | None = None
    currency: str | None = Field(default=None, max_length=10)
    team_size: int | None = None
    problem: str | None = Field(default=None, max_length=2000)
    solution: str | None = Field(default=None, max_length=3000)
    team_intro: str | None = Field(default=None, max_length=2000)
    traction: str | None = Field(default=None, max_length=1500)
    business_model: str | None = Field(default=None, max_length=1500)
    ask_note: str | None = Field(default=None, max_length=1000)


class BPIntentCreateRequest(BaseModel):
    personal_note: str = Field(default="", max_length=1000)


class BPIntentRow(BaseModel):
    id: str
    listing_id: str
    project_name: str
    investor_claw_id: str
    investor_name: str
    investor_org: str | None = None
    status: str          # pending / accepted / rejected / auto_accepted
    personal_note: str
    review_note: str = ""
    created_at: datetime
    reviewed_at: datetime | None = None
    # Queue position among PENDING intents on the same BP, 1-indexed,
    # oldest first. None once the intent is no longer pending.
    queue_position: int | None = None
    queue_total: int | None = None


class BPIntentReviewRequest(BaseModel):
    decision: str = Field(pattern="^(accepted|rejected)$")
    note: str = Field(default="", max_length=1000)


# ---------------------------------------------------------------------------
# Invite codes + role applications + contacts
# ---------------------------------------------------------------------------

class InviteCodeCreateRequest(BaseModel):
    role: str = Field(pattern="^(investor|founder)$")
    role_verified: bool = True
    note: str = Field(default="", max_length=200)
    valid_days: int = Field(default=30, ge=1, le=365)


class InviteCodeRow(BaseModel):
    code: str
    role: str
    role_verified: bool
    note: str = ""
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    used_by_lobster_id: str | None = None


class InviteCodeRedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=100)


class InviteCodeRedeemResponse(BaseModel):
    role: str
    role_verified: bool
    granted_at: datetime


class RoleApplicationCreateRequest(BaseModel):
    requested_role: str = Field(pattern="^(investor|founder)$")
    intro_text: str = Field(min_length=1, max_length=500)
    org_name: str = Field(default="", max_length=200)


class RoleApplicationRow(BaseModel):
    id: str
    lobster_id: str
    claw_id: str | None = None
    role: str
    org_name: str = ""
    intro_text: str = ""
    status: str
    reviewer_note: str = ""
    reviewed_by: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None
    lobster_name: str | None = None


class RoleApplicationReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    review_note: str = Field(default="", max_length=500)


class OwnerContactSetRequest(BaseModel):
    primary_contact: str = Field(min_length=1, max_length=100)
    primary_contact_type: str = Field(pattern="^(wechat|phone)$")
    secondary_contacts: dict | None = None


class OwnerContactRow(BaseModel):
    primary_contact: str | None = None
    primary_contact_type: str | None = None
    secondary_contacts: dict = {}


class InvestorProfileSetRequest(BaseModel):
    """All fields optional — guided Q&A drip-fills one at a time. Server
    upserts; only fields explicitly passed get updated."""
    org_name: str | None = Field(default=None, max_length=200)
    self_intro: str | None = Field(default=None, max_length=1000)
    sectors: list[str] | None = None
    stages: list[str] | None = None
    ticket_min: int | None = Field(default=None, ge=0)
    ticket_max: int | None = Field(default=None, ge=0)
    ticket_currency: str | None = Field(default=None, max_length=10)
    portfolio_examples: str | None = Field(default=None, max_length=1000)
    decision_cycle: str | None = Field(default=None, max_length=200)
    value_add: str | None = Field(default=None, max_length=500)
    team_preference: str | None = Field(default=None, max_length=500)
    redlines: str | None = Field(default=None, max_length=500)


class InvestorProfileRow(BaseModel):
    claw_id: str
    exists: bool = False
    org_name: str = ""
    self_intro: str = ""
    sectors: list[str] = []
    stages: list[str] = []
    ticket_min: int | None = None
    ticket_max: int | None = None
    ticket_currency: str = "CNY"
    portfolio_examples: str = ""
    decision_cycle: str = ""
    value_add: str = ""
    team_preference: str = ""
    redlines: str = ""
    core_complete: bool = False


# ---------------------------------------------------------------------------
# State 4: meeting request / response / unlock
# ---------------------------------------------------------------------------

class MeetingRequestResponse(BaseModel):
    intent_id: str
    investor_meet_at: datetime | None = None
    founder_meet_at: datetime | None = None
    unlocked: bool = False


class MeetingUnlockedPayload(BaseModel):
    intent_id: str
    listing_id: str
    project_name: str
    peer_name: str
    peer_org: str | None = None
    peer_contact: str | None = None
    peer_contact_type: str | None = None
    peer_secondary_contacts: dict = {}
    unlocked_at: datetime
