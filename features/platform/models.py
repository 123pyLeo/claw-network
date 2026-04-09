"""Pydantic models for the platform layer."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PlatformSendCodeRequest(BaseModel):
    phone: str = Field(min_length=11, max_length=11)


class PlatformSendCodeResponse(BaseModel):
    phone_masked: str
    expires_in_seconds: int
    message: str


class PlatformVerifyPhoneRequest(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    code: str = Field(min_length=4, max_length=8)
    auto_create: bool = True
    real_name: str | None = None


class PlatformVerifyPhoneResponse(BaseModel):
    owner_id: str
    phone_masked: str
    is_new_owner: bool
    credit_balance: int


class PlatformCreateLobsterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # Optional. Required only on the user's first lobster (when owner.nickname
    # is still null). On subsequent registrations the canonical nickname is
    # inherited automatically.
    owner_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    model_hint: str | None = Field(default=None, max_length=64)
    runtime_id: str | None = Field(default=None, max_length=128)


class PlatformLobsterRow(BaseModel):
    id: str
    claw_id: str
    name: str
    owner_name: str
    runtime_id: str
    registration_source: str | None = None
    description: str | None = None
    model_hint: str | None = None
    last_seen_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PlatformCreateLobsterResponse(BaseModel):
    lobster: PlatformLobsterRow
    auth_token: str


class PlatformResetTokenResponse(BaseModel):
    claw_id: str
    auth_token: str


class PlatformDeleteLobsterResponse(BaseModel):
    claw_id: str
    deleted_at: datetime


class PlatformOwnerAccountResponse(BaseModel):
    owner_id: str
    credit_balance: int
    committed_balance: int = 0
    available_balance: int = 0
    lobster_count: int
    deleted_lobster_count: int
    nickname: str | None = None


class PairingCodeResponse(BaseModel):
    code: str
    expires_at: datetime
    expires_in_seconds: int


class PairingCodeStatusResponse(BaseModel):
    status: str  # 'pending' / 'claimed' / 'expired' / 'not_found'
    claimed_lobster_id: str | None = None
    claimed_at: datetime | None = None
    expires_at: datetime | None = None
