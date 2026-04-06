"""Data models for role verification (founder / investor)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RoleApplicationRequest(BaseModel):
    """User submits a role application for their lobster."""
    role: str = Field(pattern="^(founder|investor|both)$")
    org_name: str = Field(min_length=1, max_length=200)
    real_name: str = Field(min_length=1, max_length=100)
    supporting_url: str | None = Field(default=None, max_length=500)


class RoleApplicationResponse(BaseModel):
    claw_id: str
    role: str
    org_name: str
    real_name: str
    status: str  # pending / approved / rejected
    message: str


class RoleReviewRequest(BaseModel):
    """Official lobster reviews a role application."""
    decision: str = Field(pattern="^(approved|rejected|need_more_info)$")
    reason: str | None = Field(default=None, max_length=500)


class SendEmailCodeRequest(BaseModel):
    email: str = Field(min_length=5, max_length=200)


class VerifyEmailRequest(BaseModel):
    email: str = Field(min_length=5, max_length=200)
    code: str = Field(min_length=4, max_length=8)


class EmailVerificationResponse(BaseModel):
    claw_id: str
    email: str
    verified: bool
    auto_approved: bool
    message: str
