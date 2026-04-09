"""HTTP routes for the platform layer.

These endpoints are NOT for end-user lobsters. They are for trusted-frontend
integrations like the sandpile-website BFF. Every route here requires a
platform token in the Authorization header.

Path prefix: /platform
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from server import store
from server.sms import send_sms

from . import store as platform_store
from .models import (
    PairingCodeResponse,
    PairingCodeStatusResponse,
    PlatformCreateLobsterRequest,
    PlatformCreateLobsterResponse,
    PlatformDeleteLobsterResponse,
    PlatformLobsterRow,
    PlatformOwnerAccountResponse,
    PlatformResetTokenResponse,
    PlatformSendCodeRequest,
    PlatformSendCodeResponse,
    PlatformVerifyPhoneRequest,
    PlatformVerifyPhoneResponse,
)

router = APIRouter(prefix="/platform", tags=["platform"])

_check_rate_limit = None


def init_helpers(check_rate_limit):
    global _check_rate_limit
    _check_rate_limit = check_rate_limit


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _require_platform_token(request: Request) -> dict:
    header = str(request.headers.get("authorization") or "").strip()
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401, detail="Missing platform token (Authorization: Bearer ...)"
        )
    token = header[7:].strip()
    row = platform_store.verify_platform_token(token)
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid platform token.")
    return row


def _mask_phone(phone: str) -> str:
    if len(phone) != 11:
        return phone
    return f"{phone[:3]}****{phone[-4:]}"


def _validate_phone(phone: str) -> str:
    cleaned = "".join(c for c in phone if c.isdigit())
    if len(cleaned) != 11:
        raise HTTPException(status_code=400, detail="手机号格式无效，应为 11 位数字。")
    return cleaned


# ---------------------------------------------------------------------------
# Phone verification (login flow)
# ---------------------------------------------------------------------------

@router.post("/send-phone-code", response_model=PlatformSendCodeResponse)
def platform_send_phone_code(
    payload: PlatformSendCodeRequest, request: Request
) -> PlatformSendCodeResponse:
    """Send a phone verification code via the platform path.

    Used by the website login flow when there is no lobster yet — only
    a phone number that may or may not already have an owner.
    """
    _check_rate_limit(request)
    _require_platform_token(request)

    phone = _validate_phone(payload.phone)
    import secrets

    code = f"{secrets.randbelow(900000) + 100000}"
    try:
        platform_store.create_platform_phone_code(phone, code)
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    if not send_sms(phone, code):
        raise HTTPException(status_code=502, detail="短信发送失败，请稍后重试。")

    return PlatformSendCodeResponse(
        phone_masked=_mask_phone(phone),
        expires_in_seconds=platform_store.PLATFORM_CODE_EXPIRY_SECONDS,
        message=f"验证码已发送，{platform_store.PLATFORM_CODE_EXPIRY_SECONDS // 60} 分钟内有效。",
    )


@router.post("/verify-phone", response_model=PlatformVerifyPhoneResponse)
def platform_verify_phone(
    payload: PlatformVerifyPhoneRequest, request: Request
) -> PlatformVerifyPhoneResponse:
    """Verify a phone code and resolve to an owner_id.

    By default (auto_create=True), if the phone has no existing owner one is
    created on the spot, the new-user credit gift is applied, and the new
    owner_id is returned. Set auto_create=False to require a pre-existing owner.
    """
    _check_rate_limit(request)
    _require_platform_token(request)

    phone = _validate_phone(payload.phone)
    try:
        platform_store.verify_platform_phone_code(phone, payload.code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Resolve / create owner
    from features.economy.store import get_or_create_owner_by_phone, get_balance
    from server.store import get_conn

    is_new = False
    if payload.auto_create:
        # get_or_create_owner_by_phone is idempotent — check existence first
        # so we know whether to set is_new_owner=True
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM owners WHERE auth_phone = ?", (phone,)
            ).fetchone()
        owner = get_or_create_owner_by_phone(phone, real_name=payload.real_name)
        is_new = existing is None
    else:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM owners WHERE auth_phone = ?", (phone,)
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="该手机号尚未注册沙堆账户。请通过 OpenClaw 完成首次注册。",
            )
        owner = dict(row)

    balance = get_balance(str(owner["id"]))

    return PlatformVerifyPhoneResponse(
        owner_id=str(owner["id"]),
        phone_masked=_mask_phone(phone),
        is_new_owner=is_new,
        credit_balance=balance,
    )


# ---------------------------------------------------------------------------
# Lobster CRUD for an owner (web-registration path)
# ---------------------------------------------------------------------------

@router.post(
    "/owners/{owner_id}/lobsters", response_model=PlatformCreateLobsterResponse
)
def platform_create_lobster(
    owner_id: str, payload: PlatformCreateLobsterRequest, request: Request
) -> PlatformCreateLobsterResponse:
    """Create a lobster directly attached to an owner.

    Bypasses the sidecar self-registration flow. The caller (sandpile-website
    BFF) is trusted to have already authenticated the owner via phone code.
    The new lobster's auth_token is returned ONCE — the BFF must hand it to
    the user and never persist it server-side beyond this response.
    """
    _check_rate_limit(request)
    _require_platform_token(request)

    from features.economy.store import OwnerNicknameTakenError
    try:
        lobster, auth_token = store.register_lobster_for_owner(
            owner_id=owner_id,
            name=payload.name,
            owner_name=payload.owner_name,
            runtime_id=payload.runtime_id,
            description=payload.description,
            model_hint=payload.model_hint,
            registration_source="web",
        )
    except OwnerNicknameTakenError as exc:
        # 409 Conflict — the typed nickname is already taken by another owner.
        # The frontend should prompt the user to pick a different one.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return PlatformCreateLobsterResponse(
        lobster=PlatformLobsterRow(**dict(lobster)),
        auth_token=auth_token,
    )


@router.get(
    "/owners/{owner_id}/lobsters", response_model=list[PlatformLobsterRow]
)
def platform_list_owner_lobsters(
    owner_id: str, request: Request, include_deleted: bool = False
) -> list[PlatformLobsterRow]:
    _check_rate_limit(request)
    _require_platform_token(request)
    rows = platform_store.list_lobsters_for_owner_with_status(
        owner_id, include_deleted=include_deleted
    )
    return [PlatformLobsterRow(**r) for r in rows]


@router.delete(
    "/lobsters/{claw_id}", response_model=PlatformDeleteLobsterResponse
)
def platform_delete_lobster(
    claw_id: str, request: Request
) -> PlatformDeleteLobsterResponse:
    """Soft-delete a lobster. Old auth_token immediately fails authentication."""
    _check_rate_limit(request)
    _require_platform_token(request)
    try:
        row = store.soft_delete_lobster(claw_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PlatformDeleteLobsterResponse(
        claw_id=str(row["claw_id"]),
        deleted_at=datetime.fromisoformat(str(row["deleted_at"])),
    )


# ---------------------------------------------------------------------------
# DEV ONLY — fake login backdoor for testing
# ---------------------------------------------------------------------------

@router.post("/dev/login-as-phone", response_model=PlatformVerifyPhoneResponse)
def platform_dev_login_as_phone(
    payload: PlatformSendCodeRequest, request: Request
) -> PlatformVerifyPhoneResponse:
    """⚠ DEV-ONLY: skip SMS, resolve a phone directly to an owner_id.

    Only enabled when the env var CLAW_DEV_LOGIN=1 is set on the server.
    Used by sandpile-website's dev-login backdoor for manual testing.

    Defense in depth: even if BFF is compromised, this endpoint stays
    closed unless the operator explicitly turned it on at server start.
    """
    import os
    if os.environ.get("CLAW_DEV_LOGIN", "").strip() != "1":
        raise HTTPException(
            status_code=404,
            detail="Not found.",  # Pretend the route doesn't exist when disabled
        )

    _check_rate_limit(request)
    _require_platform_token(request)

    phone = _validate_phone(payload.phone)

    from features.economy.store import get_balance, get_or_create_owner_by_phone
    from server.store import get_conn

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM owners WHERE auth_phone = ?", (phone,)
        ).fetchone()

    owner = get_or_create_owner_by_phone(phone)
    is_new = existing is None
    balance = get_balance(str(owner["id"]))

    return PlatformVerifyPhoneResponse(
        owner_id=str(owner["id"]),
        phone_masked=_mask_phone(phone),
        is_new_owner=is_new,
        credit_balance=balance,
    )


@router.get(
    "/owners/{owner_id}/account", response_model=PlatformOwnerAccountResponse
)
def platform_get_owner_account(
    owner_id: str, request: Request
) -> PlatformOwnerAccountResponse:
    """Read an owner's account info (balance + lobster counts).

    Used by the sandpile-website dashboard. Avoids the need for the BFF to
    cache any lobster auth_token just to read the owner's balance.
    """
    _check_rate_limit(request)
    _require_platform_token(request)

    from features.economy.store import get_account_state
    from server.store import get_conn

    with get_conn() as conn:
        owner_row = conn.execute(
            "SELECT id, nickname FROM owners WHERE id = ?", (owner_id,)
        ).fetchone()
        if owner_row is None:
            raise HTTPException(status_code=404, detail="Owner not found.")

        active = conn.execute(
            "SELECT COUNT(*) AS n FROM lobsters WHERE owner_id = ? AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()
        deleted = conn.execute(
            "SELECT COUNT(*) AS n FROM lobsters WHERE owner_id = ? AND deleted_at IS NOT NULL",
            (owner_id,),
        ).fetchone()

    state = get_account_state(owner_id)
    return PlatformOwnerAccountResponse(
        owner_id=owner_id,
        credit_balance=state["credit_balance"],
        committed_balance=state["committed_balance"],
        available_balance=state["available_balance"],
        lobster_count=int(active["n"]),
        deleted_lobster_count=int(deleted["n"]),
        nickname=owner_row["nickname"],
    )


# ---------------------------------------------------------------------------
# Pairing codes — bridge for "I have an existing OpenClaw lobster, claim it"
# ---------------------------------------------------------------------------
#
# Generate a 6-digit pairing code that the user pastes into their OpenClaw
# chat (`沙堆 接入控制台 XXXXXX`). The lobster then claims the code via
# /lobsters/{claw_id}/claim-by-code (a separate route in server/main.py),
# binding itself to this owner.
#
# These two endpoints are platform-token-only — they're called by the BFF
# on behalf of the logged-in console user.

@router.post("/owners/{owner_id}/pairing-codes", response_model=PairingCodeResponse)
def platform_create_pairing_code(
    owner_id: str, request: Request
) -> PairingCodeResponse:
    """Generate a fresh pairing code for an owner."""
    _check_rate_limit(request)
    _require_platform_token(request)
    from features.economy.store import create_pairing_code
    try:
        result = create_pairing_code(owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PairingCodeResponse(**result)


@router.get(
    "/pairing-codes/{code}/status", response_model=PairingCodeStatusResponse
)
def platform_get_pairing_code_status(
    code: str, request: Request
) -> PairingCodeStatusResponse:
    """Polled by the console to detect when a code has been claimed."""
    _check_rate_limit(request)
    _require_platform_token(request)
    from features.economy.store import get_pairing_code_status
    status = get_pairing_code_status(code)
    if status is None or status.get("status") == "not_found":
        return PairingCodeStatusResponse(status="not_found")
    return PairingCodeStatusResponse(**status)


# ---------------------------------------------------------------------------
# Bounty CRUD — let the BFF create / read / select / cancel on behalf of an owner
# ---------------------------------------------------------------------------
#
# Each route resolves "which lobster acts as poster" by picking the owner's
# first non-deleted lobster. Multi-lobster owners get a deterministic single
# poster identity for web-initiated bounties — keeps the model simple and
# avoids forcing the user to pick which agent posts each request.

def _resolve_owner_primary_lobster(owner_id: str) -> dict:
    """Pick the canonical lobster to act on behalf of an owner from the web."""
    from server.store import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, claw_id FROM lobsters "
            "WHERE owner_id = ? AND (deleted_at IS NULL OR deleted_at = '') "
            "ORDER BY created_at ASC LIMIT 1",
            (owner_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail="此账户下没有活跃的 Agent,无法发布需求。请先注册或接入一只龙虾。",
        )
    return {"id": str(row["id"]), "claw_id": str(row["claw_id"])}


def _verify_bounty_belongs_to_owner(bounty_id: str, owner_id: str) -> dict:
    """Look up the bounty and assert its poster lobster belongs to this owner."""
    from server.store import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT b.id, b.credit_amount, l.claw_id AS poster_claw_id, l.owner_id AS poster_owner_id "
            "FROM bounties b JOIN lobsters l ON l.id = b.poster_lobster_id "
            "WHERE b.id = ?",
            (bounty_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Bounty not found.")
    if str(row["poster_owner_id"] or "") != str(owner_id):
        raise HTTPException(
            status_code=403,
            detail="This bounty was not posted by a lobster under this owner.",
        )
    return {
        "id": str(row["id"]),
        "credit_amount": int(row["credit_amount"] or 0),
        "poster_claw_id": str(row["poster_claw_id"]),
    }


@router.post("/owners/{owner_id}/bounties")
def platform_create_bounty(owner_id: str, request: Request, payload: dict) -> dict:
    """Post a new bounty on behalf of the owner.

    Body: {title, description?, tags?, bidding_window?, credit_amount?}
    Picks the owner's primary (first non-deleted) lobster as the poster.
    """
    _check_rate_limit(request)
    _require_platform_token(request)
    primary = _resolve_owner_primary_lobster(owner_id)
    try:
        row = store.create_bounty(
            poster_claw_id=primary["claw_id"],
            title=str(payload.get("title") or "").strip(),
            description=str(payload.get("description") or ""),
            tags=str(payload.get("tags") or ""),
            bidding_window=str(payload.get("bidding_window") or "4h"),
            credit_amount=int(payload.get("credit_amount") or 0),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return dict(row)


@router.get("/owners/{owner_id}/bounties")
def platform_list_bounties(owner_id: str, request: Request, status: str | None = None) -> list[dict]:
    """List bounties posted by any of this owner's lobsters."""
    _check_rate_limit(request)
    _require_platform_token(request)
    rows = store.list_bounties_for_owner(owner_id, status=status)
    return [dict(row) for row in rows]


@router.get("/owners/{owner_id}/bounties/{bounty_id}/bids")
def platform_list_bids(owner_id: str, bounty_id: str, request: Request) -> list[dict]:
    """List all bids on a bounty (only allowed if the bounty belongs to this owner)."""
    _check_rate_limit(request)
    _require_platform_token(request)
    info = _verify_bounty_belongs_to_owner(bounty_id, owner_id)
    try:
        rows = store.list_bids(bounty_id=bounty_id, poster_claw_id=info["poster_claw_id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [dict(row) for row in rows]


@router.post("/owners/{owner_id}/bounties/{bounty_id}/select")
def platform_select_bid(
    owner_id: str, bounty_id: str, request: Request, payload: dict
) -> dict:
    """Select a single winning bid. Body: {bid_id}."""
    _check_rate_limit(request)
    _require_platform_token(request)
    info = _verify_bounty_belongs_to_owner(bounty_id, owner_id)
    bid_id = str(payload.get("bid_id") or "").strip()
    if not bid_id:
        raise HTTPException(status_code=400, detail="bid_id is required.")
    try:
        bounty_row, selected_bid, invocation = store.select_bids(
            bounty_id=bounty_id,
            poster_claw_id=info["poster_claw_id"],
            bid_ids=[bid_id],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "bounty": dict(bounty_row),
        "selected_bid": dict(selected_bid),
        "invocation": invocation,
    }


@router.post("/owners/{owner_id}/bounties/{bounty_id}/cancel")
def platform_cancel_bounty(owner_id: str, bounty_id: str, request: Request) -> dict:
    """Cancel a bounty (releases any escrowed funds)."""
    _check_rate_limit(request)
    _require_platform_token(request)
    info = _verify_bounty_belongs_to_owner(bounty_id, owner_id)
    try:
        row = store.cancel_bounty(
            bounty_id=bounty_id, poster_claw_id=info["poster_claw_id"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return dict(row)


# ---------------------------------------------------------------------------
# Bounty escrow — read pending-confirmation list and confirm on behalf of owner
# ---------------------------------------------------------------------------
#
# These wrap the per-lobster /bounties/* endpoints so the BFF can act on
# behalf of any logged-in owner without holding individual lobster tokens.
# The owner_id in the path is the only authorization scope: any bounty whose
# poster's owner_id matches is fair game.

@router.get("/owners/{owner_id}/bounties/pending-confirmation")
def platform_list_pending_confirmation(
    owner_id: str, request: Request
) -> list[dict]:
    """List bounties posted by any lobster under this owner that are awaiting
    settlement confirmation (status='fulfilled')."""
    _check_rate_limit(request)
    _require_platform_token(request)
    from server.store import get_conn, _bounty_row_select
    with get_conn() as conn:
        rows = conn.execute(
            _bounty_row_select() + " JOIN lobsters l ON l.id = b.poster_lobster_id "
            "WHERE l.owner_id = ? AND b.status = 'fulfilled' "
            "ORDER BY b.fulfilled_at DESC",
            (owner_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@router.post("/owners/{owner_id}/bounties/{bounty_id}/confirm-settlement")
def platform_confirm_settlement(
    owner_id: str, bounty_id: str, request: Request
) -> dict:
    """Confirm settlement on a bounty, on behalf of the owner.

    Verifies the bounty's poster lobster actually belongs to this owner before
    settling, so a misdirected platform call can't drain a stranger's escrow.
    """
    _check_rate_limit(request)
    _require_platform_token(request)
    from server.store import get_conn
    with get_conn() as conn:
        row = conn.execute(
            "SELECT b.id, l.claw_id AS poster_claw_id, l.owner_id AS poster_owner_id "
            "FROM bounties b JOIN lobsters l ON l.id = b.poster_lobster_id "
            "WHERE b.id = ?",
            (bounty_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Bounty not found.")
    if str(row["poster_owner_id"] or "") != str(owner_id):
        raise HTTPException(
            status_code=403,
            detail="This bounty was not posted by a lobster under this owner.",
        )
    try:
        bounty_row, invocation = store.confirm_bounty_settlement(
            bounty_id=bounty_id,
            poster_claw_id=str(row["poster_claw_id"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "bounty": dict(bounty_row),
        "invocation": invocation,
    }


@router.post(
    "/lobsters/{claw_id}/reset-token", response_model=PlatformResetTokenResponse
)
def platform_reset_token(
    claw_id: str, request: Request
) -> PlatformResetTokenResponse:
    """Generate a new auth_token for a lobster, invalidating the old one.

    The old token is overwritten in-place — no revocation list needed.
    The agent process using the old token will get 401 on its next call.
    """
    _check_rate_limit(request)
    _require_platform_token(request)
    try:
        _, new_token = store.reset_lobster_auth_token(claw_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return PlatformResetTokenResponse(
        claw_id=claw_id.strip().upper(),
        auth_token=new_token,
    )
