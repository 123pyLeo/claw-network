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
    DailyBonusResponse,
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


@router.post("/owners/{owner_id}/lobsters/{claw_id}/unbind")
def platform_unbind_lobster(owner_id: str, claw_id: str, request: Request) -> dict:
    """Detach a lobster from an owner (keep lobster body + history).

    Ownership is verified at the store level — we don't just trust the owner_id
    from the URL. This prevents a bug/misuse from detaching someone else's lobster.
    """
    _check_rate_limit(request)
    _require_platform_token(request)
    try:
        row = store.unbind_lobster_from_owner(claw_id, owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "claw_id": str(row["claw_id"]),
        "name": str(row["name"] or ""),
        "owner_id": None,
        "unbound_at": str(row["updated_at"] or ""),
    }


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
# Delivery (交付) — BFF façade for the deliveries module
# ---------------------------------------------------------------------------

@router.post("/deliveries")
def platform_create_delivery(request: Request, payload: dict) -> dict:
    """Create a delivery from the owner who's submitting it.

    Body: {
      order_id, order_kind ('bounty' | 'deal'),
      owner_id (the submitter),
      note, attachments: [ {kind, content?, payload_url?, hash?, byte_size?, filename?} ... ]
    }
    """
    _check_rate_limit(request)
    _require_platform_token(request)
    p = payload or {}
    owner_id = str(p.get("owner_id") or "").strip()
    order_id = str(p.get("order_id") or "").strip()
    order_kind = str(p.get("order_kind") or "").strip()
    note = str(p.get("note") or "").strip()
    attachments = p.get("attachments") or []
    if not owner_id or not order_id or order_kind not in ("bounty", "deal"):
        raise HTTPException(status_code=400, detail="参数不全。")

    # Derive receiver_owner_id + confirmation_window from the order
    from server.store import get_conn
    with get_conn() as conn:
        if order_kind == "bounty":
            row = conn.execute(
                "SELECT b.confirmation_window, l.owner_id AS poster_owner_id "
                "FROM bounties b JOIN lobsters l ON l.id = b.poster_lobster_id "
                "WHERE b.id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Bounty not found.")
            receiver = str(row["poster_owner_id"] or "")
            window = str(row["confirmation_window"] or "7d")
        else:
            row = conn.execute(
                "SELECT confirmation_window, caller_owner_id FROM deals WHERE id = ?",
                (order_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Deal not found.")
            receiver = str(row["caller_owner_id"] or "")
            window = str(row["confirmation_window"] or "7d")
    if not receiver:
        raise HTTPException(status_code=400, detail="订单没有可识别的需求方。")
    if receiver == owner_id:
        raise HTTPException(status_code=400, detail="不能给自己交付。")

    from features.economy.store import create_delivery
    try:
        result = create_delivery(
            order_id=order_id,
            order_kind=order_kind,
            submitter_owner_id=owner_id,
            receiver_owner_id=receiver,
            note=note,
            attachments=attachments,
            confirmation_window=window,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Also flip the underlying order to 'fulfilled' so it surfaces in A's
    # pending-settlement view. Delivery and legacy "沙堆 交付了" path both land here.
    from server.store import get_conn, utc_now
    now = utc_now()
    with get_conn() as conn:
        if order_kind == "bounty":
            conn.execute(
                "UPDATE bounties SET status = 'fulfilled', fulfilled_at = COALESCE(fulfilled_at, ?), updated_at = ? "
                "WHERE id = ? AND status = 'assigned'",
                (now, now, order_id),
            )
        else:
            conn.execute(
                "UPDATE deals SET status = 'fulfilled', fulfilled_at = COALESCE(fulfilled_at, ?), updated_at = ? "
                "WHERE id = ? AND status = 'accepted'",
                (now, now, order_id),
            )
    return result


@router.get("/owners/{owner_id}/bounties/awaiting-delivery")
def platform_list_awaiting_delivery(owner_id: str, request: Request) -> list[dict]:
    """Bounties where this owner is the selected bidder and still needs to deliver."""
    _check_rate_limit(request)
    _require_platform_token(request)
    from server import store as _s
    rows = _s.list_bounties_awaiting_delivery_for_bidder(owner_id)
    return [dict(r) for r in rows]


@router.get("/deliveries/{delivery_id}")
def platform_get_delivery(delivery_id: str, request: Request) -> dict:
    _check_rate_limit(request)
    _require_platform_token(request)
    from features.economy.store import get_delivery
    d = get_delivery(delivery_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Delivery not found.")
    return d


@router.get("/orders/{order_kind}/{order_id}/delivery")
def platform_get_active_delivery(order_kind: str, order_id: str, request: Request) -> dict:
    """Return the currently-active delivery for an order, or 404 if none."""
    _check_rate_limit(request)
    _require_platform_token(request)
    if order_kind not in ("bounty", "deal"):
        raise HTTPException(status_code=400, detail="order_kind 非法。")
    from features.economy.store import list_active_deliveries_for_order
    items = list_active_deliveries_for_order(order_id, order_kind)
    if not items:
        raise HTTPException(status_code=404, detail="暂无交付。")
    return items[0]


@router.post("/deliveries/{delivery_id}/withdraw")
def platform_withdraw_delivery(delivery_id: str, request: Request, payload: dict) -> dict:
    _check_rate_limit(request)
    _require_platform_token(request)
    owner_id = str((payload or {}).get("owner_id") or "").strip()
    if not owner_id:
        raise HTTPException(status_code=400, detail="owner_id 必填。")
    from features.economy.store import withdraw_delivery
    try:
        result = withdraw_delivery(delivery_id, owner_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Also flip the order status back from 'fulfilled' to 'assigned' so B can
    # re-submit a new delivery. (v1: no rework UI, but this matches the
    # semantic — delivery withdrawn = order hasn't actually been delivered.)
    if result and result.get("order_id") and result.get("order_kind"):
        from server.store import get_conn
        now = result["withdrawn_at"] or ""
        with get_conn() as conn:
            if result["order_kind"] == "bounty":
                conn.execute(
                    "UPDATE bounties SET status = 'assigned', updated_at = ? "
                    "WHERE id = ? AND status = 'fulfilled'",
                    (now, result["order_id"]),
                )
            else:
                conn.execute(
                    "UPDATE deals SET status = 'accepted', updated_at = ? "
                    "WHERE id = ? AND status = 'fulfilled'",
                    (now, result["order_id"]),
                )
    return result


@router.post("/deliveries/attachments/upload")
async def platform_upload_attachment_bytes(request: Request) -> dict:
    """Upload an image/file attachment before calling /deliveries.

    Body: multipart form with 'file' field. Response: {hash, byte_size, filename, stored_name}.
    Caller then includes {kind: 'image'|'file', payload_url: '...', hash, byte_size, filename}
    in the create_delivery call.
    """
    _check_rate_limit(request)
    _require_platform_token(request)
    try:
        form = await request.form()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"无效的 multipart 请求: {exc}") from exc
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="缺少 file 字段。")
    data = await upload.read()
    filename = getattr(upload, "filename", "") or ""
    from features.economy.store import save_attachment_bytes
    try:
        result = save_attachment_bytes(data, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "hash": result["hash"],
        "byte_size": result["byte_size"],
        "filename": filename,
        "stored_name": result["stored_name"],
        # Internal URL where A can download later (served by a separate GET route)
        "payload_url": f"/platform/attachments/{result['stored_name']}",
    }


@router.get("/attachments/{stored_name}")
def platform_get_attachment_bytes(stored_name: str, request: Request):
    """Serve raw bytes of an uploaded attachment, while still within TTL."""
    _check_rate_limit(request)
    _require_platform_token(request)
    from features.economy.store import _delivery_bytes_dir
    import pathlib
    # Normalize to prevent path traversal
    safe = pathlib.Path(stored_name).name
    path = _delivery_bytes_dir() / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="附件已清理或不存在。")
    from fastapi.responses import FileResponse
    return FileResponse(str(path), filename=safe)


# ---------------------------------------------------------------------------
# Redemption codes (兑换码) — user enters a code to top up credits
# ---------------------------------------------------------------------------

@router.post("/owners/{owner_id}/redeem")
def platform_redeem_code(owner_id: str, request: Request, payload: dict) -> dict:
    _check_rate_limit(request)
    _require_platform_token(request)
    code = str((payload or {}).get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="兑换码不能为空。")
    from features.economy.store import redeem_code
    from server.store import get_conn
    with get_conn() as conn:
        owner_row = conn.execute("SELECT id FROM owners WHERE id = ?", (owner_id,)).fetchone()
        if owner_row is None:
            raise HTTPException(status_code=404, detail="Owner not found.")
        # Best-effort claw_id for audit. Web redemption is owner-scoped, but we
        # stamp the owner's first active lobster so admin listings aren't blank.
        lobster_row = conn.execute(
            "SELECT claw_id FROM lobsters WHERE owner_id = ? "
            "AND (deleted_at IS NULL OR deleted_at = '') "
            "ORDER BY created_at ASC LIMIT 1",
            (owner_id,),
        ).fetchone()
    audit_claw = str(lobster_row["claw_id"]) if lobster_row else ""
    try:
        return redeem_code(code, owner_id, audit_claw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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

    # Persist confirmation_window separately (create_bounty signature kept stable
    # to avoid disturbing the legacy OpenClaw plugin path).
    window = str(payload.get("confirmation_window") or "7d")
    if window not in ("24h", "3d", "7d", "14d"):
        window = "7d"
    from server.store import get_conn as _gc
    with _gc() as conn:
        conn.execute(
            "UPDATE bounties SET confirmation_window = ? WHERE id = ?",
            (window, row["id"]),
        )
    result = dict(row)
    result["confirmation_window"] = window
    return result


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


@router.post("/owners/{owner_id}/bounties/{bounty_id}/bid")
def platform_bid_bounty(
    owner_id: str, bounty_id: str, request: Request, payload: dict
) -> dict:
    """Bid on a bounty on behalf of the owner's primary lobster."""
    _check_rate_limit(request)
    _require_platform_token(request)
    primary = _resolve_owner_primary_lobster(owner_id)
    pitch = str(payload.get("pitch") or "")
    try:
        bounty_row, bid_row = store.bid_bounty(
            bounty_id=bounty_id,
            bidder_claw_id=primary["claw_id"],
            pitch=pitch,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"bounty": dict(bounty_row), "bid": dict(bid_row)}


@router.get("/owners/{owner_id}/deals")
def platform_list_deals(owner_id: str, request: Request) -> list[dict]:
    """List direct deals for any lobster under this owner."""
    _check_rate_limit(request)
    _require_platform_token(request)
    from server.store import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.*,
                   caller.claw_id AS caller_claw_id, caller.name AS caller_name,
                   callee.claw_id AS callee_claw_id, callee.name AS callee_name
            FROM deals d
            JOIN lobsters caller ON caller.id = d.caller_lobster_id
            JOIN lobsters callee ON callee.id = d.callee_lobster_id
            WHERE caller.owner_id = ? OR callee.owner_id = ?
            ORDER BY d.created_at DESC
            LIMIT 50
            """,
            (owner_id, owner_id),
        ).fetchall()
    return [dict(r) for r in rows]


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


# ---------------------------------------------------------------------------
# Daily login bonus
# ---------------------------------------------------------------------------

@router.post("/daily-bonus/{owner_id}", response_model=DailyBonusResponse)
def platform_claim_daily_bonus(owner_id: str, request: Request):
    """Claim today's login bonus for an owner. Idempotent — once per day."""
    _check_rate_limit(request)
    _require_platform_token(request)
    result = platform_store.claim_daily_bonus(owner_id)
    return DailyBonusResponse(**result)


# ---------------------------------------------------------------------------
# Call traces (调用链可视化)
# ---------------------------------------------------------------------------

from pydantic import BaseModel as _BaseModel


class OAuthLoginRequest(_BaseModel):
    provider: str
    provider_user_id: str
    provider_nickname: str | None = None
    provider_avatar_url: str | None = None
    provider_email: str | None = None
    provider_phone: str | None = None


@router.post("/oauth-login")
def platform_oauth_login(payload: OAuthLoginRequest, request: Request):
    """Resolve an OAuth identity to an owner. Creates owner if needed, auto-merges by phone."""
    _require_platform_token(request)
    from server.store import get_conn, new_uuid, utc_now
    from features.economy.store import INITIAL_CREDIT_BALANCE

    now = utc_now()
    provider = payload.provider.strip().lower()
    puid = str(payload.provider_user_id).strip()

    with get_conn() as conn:
        # 1. Check if this OAuth identity already linked to an owner
        existing = conn.execute(
            "SELECT owner_id FROM oauth_identities WHERE provider = ? AND provider_user_id = ?",
            (provider, puid),
        ).fetchone()

        if existing:
            owner_id = existing["owner_id"]
            owner = conn.execute("SELECT * FROM owners WHERE id = ?", (owner_id,)).fetchone()
            account = conn.execute("SELECT credit_balance FROM accounts WHERE owner_id = ?", (owner_id,)).fetchone()
            return {
                "owner_id": owner_id,
                "is_new_owner": False,
                "phone_masked": _mask_phone(owner["auth_phone"]) if owner and owner["auth_phone"] else None,
                "nickname": (owner["nickname"] if owner else None) or payload.provider_nickname,
                "credit_balance": int(account["credit_balance"]) if account else 0,
            }

        # 2. Try to merge by phone (if watcha provided a phone we already know)
        owner_id = None
        is_new = True
        if payload.provider_phone:
            cleaned_phone = "".join(c for c in payload.provider_phone if c.isdigit())
            if len(cleaned_phone) == 11:
                phone_owner = conn.execute(
                    "SELECT id FROM owners WHERE auth_phone = ?", (cleaned_phone,)
                ).fetchone()
                if phone_owner:
                    owner_id = phone_owner["id"]
                    is_new = False

        # 3. Create new owner if no merge target
        if not owner_id:
            owner_id = new_uuid()
            phone = None
            if payload.provider_phone:
                cleaned = "".join(c for c in payload.provider_phone if c.isdigit())
                if len(cleaned) == 11:
                    phone = cleaned
            conn.execute(
                "INSERT INTO owners (id, auth_phone, auth_email, real_name, nickname, created_at) VALUES (?,?,?,?,?,?)",
                (owner_id, phone, payload.provider_email, None, payload.provider_nickname, now),
            )
            conn.execute(
                "INSERT INTO accounts (owner_id, credit_balance, committed_balance, updated_at) VALUES (?,?,0,?)",
                (owner_id, INITIAL_CREDIT_BALANCE, now),
            )

        # 4. Link OAuth identity to owner
        conn.execute(
            """INSERT INTO oauth_identities
               (id, owner_id, provider, provider_user_id,
                provider_nickname, provider_avatar_url, provider_email, provider_phone, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (new_uuid(), owner_id, provider, puid,
             payload.provider_nickname, payload.provider_avatar_url,
             payload.provider_email, payload.provider_phone, now),
        )

        account = conn.execute("SELECT credit_balance FROM accounts WHERE owner_id = ?", (owner_id,)).fetchone()
        owner = conn.execute("SELECT * FROM owners WHERE id = ?", (owner_id,)).fetchone()

    return {
        "owner_id": owner_id,
        "is_new_owner": is_new,
        "phone_masked": _mask_phone(owner["auth_phone"]) if owner and owner["auth_phone"] else None,
        "nickname": (owner["nickname"] if owner else None) or payload.provider_nickname,
        "credit_balance": int(account["credit_balance"]) if account else 0,
    }


class TraceStepRequest(_BaseModel):
    trace_id: str
    step_order: int = 0
    from_claw_id: str
    to_claw_id: str
    from_name: str = ""
    to_name: str = ""
    action: str = "route"
    status: str = "pending"
    question: str | None = None
    answer: str | None = None


@router.post("/traces")
def platform_write_trace(payload: TraceStepRequest, request: Request):
    _require_platform_token(request)
    from server.store import get_conn, new_uuid, utc_now
    now = utc_now()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO call_traces
               (id, trace_id, step_order, from_claw_id, to_claw_id,
                from_name, to_name, action, status, question, answer, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_uuid(), payload.trace_id, payload.step_order,
             payload.from_claw_id, payload.to_claw_id,
             payload.from_name, payload.to_name,
             payload.action, payload.status,
             payload.question, payload.answer, now),
        )
    return {"ok": True}


@router.get("/traces")
def platform_list_traces(request: Request):
    _require_platform_token(request)
    from server.store import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT trace_id, MIN(created_at) as started_at,
                      MAX(step_order) as total_steps,
                      MIN(question) as question
               FROM call_traces
               GROUP BY trace_id
               ORDER BY started_at DESC
               LIMIT 50"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/public-transactions")
def platform_public_transactions(request: Request):
    """Today's transactions, masked. Used by the public homepage stats page."""
    _require_platform_token(request)
    from server.store import get_conn
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn() as conn:
        total_amount = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM invocations "
            "WHERE created_at >= ? AND settlement_status IN ('instant','settled')",
            (today,),
        ).fetchone()[0]
        total_count = conn.execute(
            "SELECT COUNT(*) FROM invocations "
            "WHERE created_at >= ? AND settlement_status IN ('instant','settled')",
            (today,),
        ).fetchone()[0]
        total_fee = conn.execute(
            "SELECT COALESCE(SUM(platform_fee),0) FROM invocations "
            "WHERE created_at >= ? AND settlement_status IN ('instant','settled')",
            (today,),
        ).fetchone()[0]
        rows = conn.execute("""
            SELECT i.amount, i.platform_fee, i.payee_net, i.source_type, i.created_at,
                   cl.name AS caller_name, ce.name AS callee_name
            FROM invocations i
            LEFT JOIN lobsters cl ON cl.owner_id = i.caller_owner_id
            LEFT JOIN lobsters ce ON ce.owner_id = i.callee_owner_id
            WHERE i.created_at >= ? AND i.settlement_status IN ('instant','settled')
            ORDER BY i.created_at DESC LIMIT 20
        """, (today,)).fetchall()

    def _mask(name: str | None) -> str:
        if not name:
            return "匿名龙虾"
        if len(name) <= 2:
            return name[0] + "*"
        return name[0] + "*" * (len(name) - 2) + name[-1]

    transactions = [
        {
            "amount": int(r["amount"] or 0),
            "platform_fee": int(r["platform_fee"] or 0),
            "payee_net": int(r["payee_net"] or 0) if r["payee_net"] else int(r["amount"] or 0),
            "source_type": r["source_type"],
            "created_at": r["created_at"],
            "caller_name": _mask(r["caller_name"]),
            "callee_name": _mask(r["callee_name"]),
        }
        for r in rows
    ]

    return {
        "total_amount": int(total_amount),
        "total_count": int(total_count),
        "total_fee": int(total_fee),
        "transactions": transactions,
    }


@router.get("/traces/{trace_id}")
def platform_get_trace(trace_id: str, request: Request):
    _require_platform_token(request)
    from server.store import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM call_traces WHERE trace_id = ? ORDER BY step_order",
            (trace_id,),
        ).fetchall()
    return [dict(r) for r in rows]
