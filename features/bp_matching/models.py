"""Data models for BP matching (founder-investor matchmaking)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BPListingCreateRequest(BaseModel):
    project_name: str = Field(min_length=1, max_length=200)
    sector: str = Field(default="", max_length=200)          # e.g. "AI,B2B SaaS"
    stage: str = Field(default="", max_length=50)             # pre-seed / seed / A / B
    funding_ask: int | None = None                            # amount in smallest unit
    currency: str = Field(default="CNY", max_length=10)
    one_liner: str = Field(min_length=1, max_length=500)      # one-line pitch
    team_size: int | None = None
    access_policy: str = Field(default="manual", pattern="^(manual|open)$")
    expires_in_days: int | None = Field(default=90, ge=1, le=365)


class BPListingRow(BaseModel):
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


class BPListingUpdateRequest(BaseModel):
    access_policy: str | None = Field(default=None, pattern="^(manual|open)$")
    status: str | None = Field(default=None, pattern="^(active|closed)$")


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
    created_at: datetime
    reviewed_at: datetime | None = None


class BPIntentReviewRequest(BaseModel):
    decision: str = Field(pattern="^(accepted|rejected)$")
