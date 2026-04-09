"""Pydantic models for economy layer."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AccountInfoResponse(BaseModel):
    claw_id: str
    owner_id: str | None
    credit_balance: int
    has_account: bool


class InvocationRow(BaseModel):
    id: str
    caller_owner_id: str
    callee_owner_id: str
    source_type: str
    source_id: str
    amount: int
    status: str
    created_at: datetime
    completed_at: datetime | None = None


class JoinRequestReviewRequest(BaseModel):
    decision: str  # 'approved' or 'rejected'


class JoinRequestRow(BaseModel):
    id: str
    requesting_lobster_id: str
    target_owner_id: str
    phone: str
    status: str
    created_at: datetime
    expires_at: datetime
    reviewed_at: datetime | None = None
    requesting_claw_id: str | None = None
    requesting_name: str | None = None


class OwnerLobsterRow(BaseModel):
    id: str
    claw_id: str
    name: str
    runtime_id: str
    created_at: datetime
