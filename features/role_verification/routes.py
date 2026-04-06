"""API routes for role verification."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from server.store import get_lobster_by_claw_id, get_official_lobster, create_message
from server.sms import generate_code, send_sms, CODE_EXPIRY_SECONDS, SEND_COOLDOWN_SECONDS

from .models import (
    RoleApplicationRequest,
    RoleApplicationResponse,
    RoleReviewRequest,
    SendEmailCodeRequest,
    VerifyEmailRequest,
    EmailVerificationResponse,
)
from . import store as role_store

router = APIRouter(tags=["role-verification"])


# ---------------------------------------------------------------------------
# Helpers (imported from main app at registration time)
# ---------------------------------------------------------------------------

_check_rate_limit = None
_require_http_auth = None


def init_helpers(check_rate_limit, require_http_auth):
    """Called by main app to inject shared helpers."""
    global _check_rate_limit, _require_http_auth
    _check_rate_limit = check_rate_limit
    _require_http_auth = require_http_auth


def _http_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Role application
# ---------------------------------------------------------------------------

@router.post("/lobsters/{claw_id}/role", response_model=RoleApplicationResponse)
def apply_role(claw_id: str, payload: RoleApplicationRequest, request: Request) -> RoleApplicationResponse:
    """Submit a role application (founder / investor)."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id)
    try:
        result = role_store.submit_role_application(
            claw_id=claw_id,
            role=payload.role,
            org_name=payload.org_name,
            real_name=payload.real_name,
            supporting_url=payload.supporting_url,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    # Notify official lobster
    try:
        official = get_official_lobster()
        msg = role_store.build_review_message(result)
        create_message(
            from_claw_id=claw_id,
            to_claw_id=str(official["claw_id"]),
            content=msg,
            message_type="role_application",
        )
    except Exception:
        pass  # Don't fail the application if notification fails

    # Determine response message based on role
    if payload.role == "founder":
        message = "创业者角色申请已提交。由于创业者为轻验证，你的申请将很快通过。"
    else:
        message = "角色认证申请已提交，请等待审核。你也可以通过机构邮箱验证加速审核。"

    return RoleApplicationResponse(
        claw_id=claw_id,
        role=payload.role,
        org_name=payload.org_name,
        real_name=payload.real_name,
        status=result["status"],
        message=message,
    )


@router.post("/role-applications/{application_id}/review", response_model=RoleApplicationResponse)
def review_role(application_id: str, payload: RoleReviewRequest, request: Request) -> RoleApplicationResponse:
    """Review a role application (official lobster only)."""
    _check_rate_limit(request)
    # Only official lobster can review
    official = get_official_lobster()
    _require_http_auth(request, str(official["claw_id"]))
    try:
        result = role_store.review_application(
            application_id=application_id,
            decision=payload.decision,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    return RoleApplicationResponse(
        claw_id=result["claw_id"],
        role=result["role"],
        org_name="",
        real_name="",
        status=result["status"],
        message=f"审核结果：{result['status']}" + (f"，原因：{result['reason']}" if result.get("reason") else ""),
    )


@router.get("/role-applications/pending")
def list_pending(request: Request) -> list[dict]:
    """List pending role applications (official lobster only)."""
    _check_rate_limit(request)
    official = get_official_lobster()
    _require_http_auth(request, str(official["claw_id"]))
    return role_store.get_pending_applications()


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------

@router.post("/lobsters/{claw_id}/email/send-code", response_model=EmailVerificationResponse)
def send_email_code(claw_id: str, payload: SendEmailCodeRequest, request: Request) -> EmailVerificationResponse:
    """Send verification code to an email address."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id)
    try:
        email = role_store.validate_email(payload.email)
    except ValueError as exc:
        raise _http_error(exc) from exc

    lobster = get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise HTTPException(status_code=404, detail="Lobster not found.")
    lobster_id = str(lobster["id"])

    # Rate limit
    last_sent = role_store.get_email_last_sent_time(lobster_id, email)
    if last_sent:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_sent)).total_seconds()
        if elapsed < SEND_COOLDOWN_SECONDS:
            remaining = int(SEND_COOLDOWN_SECONDS - elapsed)
            raise HTTPException(status_code=429, detail=f"发送过于频繁，请 {remaining} 秒后重试。")

    code = generate_code()
    role_store.create_email_verification_code(lobster_id, email, code, CODE_EXPIRY_SECONDS)

    # In dev mode, send_sms prints to console. For email, we do the same.
    import logging
    logger = logging.getLogger(__name__)
    logger.info("[DEV EMAIL] email=%s code=%s", email, code)
    print(f"\n{'='*50}")
    print(f"  邮箱验证码（开发模式）: {code}")
    print(f"  发送至邮箱: {email}")
    print(f"{'='*50}\n", flush=True)

    masked = email[:3] + "***" + email[email.index("@"):]
    return EmailVerificationResponse(
        claw_id=claw_id,
        email=masked,
        verified=False,
        auto_approved=False,
        message=f"验证码已发送至 {masked}，{CODE_EXPIRY_SECONDS // 60} 分钟内有效。",
    )


@router.post("/lobsters/{claw_id}/email/verify", response_model=EmailVerificationResponse)
def verify_email(claw_id: str, payload: VerifyEmailRequest, request: Request) -> EmailVerificationResponse:
    """Verify email with code. Institutional emails auto-approve role."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id)
    try:
        email = role_store.validate_email(payload.email)
        result = role_store.verify_email(claw_id, email, payload.code)
    except ValueError as exc:
        raise _http_error(exc) from exc

    masked = email[:3] + "***" + email[email.index("@"):]
    msg = "邮箱验证成功。"
    if result["auto_approved"]:
        msg += " 机构邮箱已识别，角色认证自动通过！"

    return EmailVerificationResponse(
        claw_id=claw_id,
        email=masked,
        verified=True,
        auto_approved=result["auto_approved"],
        message=msg,
    )
