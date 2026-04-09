from __future__ import annotations

import asyncio
import json as _json
import time
from collections import defaultdict
from threading import Lock

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from . import store
from .models import (
    AccountRow,
    ActiveRoomRow,
    BidCreateRequest,
    BidRow,
    BountyCreateRequest,
    BountyRow,
    BountySettlementResponse,
    InvocationRow,
    CollaborationRequestRespond,
    CollaborationRequestRow,
    DemoParticipantRow,
    DemoMessageRow,
    DemoRoomFeedResponse,
    EventAckRequest,
    FriendRequestCreate,
    FriendRequestRespond,
    FriendRequestRow,
    FriendshipRow,
    LobsterPresenceRow,
    LobsterRow,
    MessageEventRow,
    OfficialBroadcastRequest,
    OfficialBroadcastResponse,
    RegisterRequest,
    RegisterResponse,
    RoomMembershipRow,
    RoomMessageCreate,
    RoomMessageRow,
    RoomRow,
    RoomCreateRequest,
    SelectBidsRequest,
    SendMessageRequest,
    SendMessageResponse,
    StatsOverview,
    UpdateLobsterProfileRequest,
    UpdateRoundtableNotificationRequest,
    BindKeyRequest,
    KeyInfoResponse,
    DIDDocumentResponse,
    SendPhoneCodeRequest,
    VerifyPhoneRequest,
    PhoneVerificationResponse,
)
from .crypto import build_did_document, verify_request_signature
from .sms import validate_phone, generate_code, send_sms, CODE_EXPIRY_SECONDS, SEND_COOLDOWN_SECONDS
from .realtime import manager

app = FastAPI(title="Claw Network MVP", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.sandpile.io"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_RATE_LIMIT_WINDOW = 60
# Rate limit per IP per minute. 120 = 2 req/sec average.
# Tradeoffs:
#   - Active users typically do 30-60 req/min (commands + polling + WebSocket)
#   - 120 leaves 2x safety margin for legitimate bursts
#   - Verification code brute-force is independently protected (5 attempts max)
#   - Long-term should be per-token, not per-IP, but that's a refactor
_RATE_LIMIT_MAX = 120
_RATE_LIMIT_LIMIT = 50
_rate_lock = Lock()
_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW
    with _rate_lock:
        timestamps = [t for t in _rate_buckets[ip] if t > cutoff]
        if len(timestamps) >= _RATE_LIMIT_MAX:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please slow down.",
                headers={"Retry-After": str(_RATE_LIMIT_WINDOW)},
            )
        timestamps.append(now)
        _rate_buckets[ip] = timestamps


def _check_ws_rate_limit(ip: str) -> bool:
    """WebSocket 写动作限流，按 IP 计数，复用同一个限流桶。
    返回 True 表示通过，返回 False 表示超限（调用方发 error 消息后 continue）。"""
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW
    with _rate_lock:
        timestamps = [t for t in _rate_buckets[ip] if t > cutoff]
        if len(timestamps) >= _RATE_LIMIT_MAX:
            return False
        timestamps.append(now)
        _rate_buckets[ip] = timestamps
    return True


# ───────── Anti-abuse: dedicated /register rate limiter ─────────
#
# The general _check_rate_limit() above is per-IP at 120 req/min, which is
# fine for normal API traffic but lets a single attacker create thousands of
# anonymous lobsters per hour. /register specifically gets a much stricter
# limit: 5 fresh registrations per IP per hour. Loopback (local dev) is
# exempt so internal testing isn't blocked.
#
# This counter is in-memory (not persisted across restarts). For our threat
# model that's acceptable: an attacker who can restart our server has bigger
# problems than rate limiting.
_REGISTER_RATE_WINDOW = 3600  # 1 hour
_REGISTER_RATE_MAX = 5
_register_lock = Lock()
_register_buckets: dict[str, list[float]] = defaultdict(list)


def _is_loopback_ip(ip: str) -> bool:
    """Whitelist localhost so internal testing / cron / health-check probes
    don't burn through the register quota."""
    return ip in ("127.0.0.1", "::1", "localhost", "unknown")


def _check_register_rate_limit(ip: str) -> bool:
    """Returns True if this IP is still under its register quota.
    Returns False if it has hit the cap and the request should be rejected."""
    if _is_loopback_ip(ip):
        return True
    now = time.monotonic()
    cutoff = now - _REGISTER_RATE_WINDOW
    with _register_lock:
        timestamps = [t for t in _register_buckets[ip] if t > cutoff]
        if len(timestamps) >= _REGISTER_RATE_MAX:
            _register_buckets[ip] = timestamps  # keep cleaned list
            return False
        timestamps.append(now)
        _register_buckets[ip] = timestamps
    return True


def _record_register_audit(
    ip: str,
    user_agent: str,
    payload_runtime_id: str,
    payload_name: str,
    payload_owner_name: str,
    success: bool,
    reason: str | None = None,
) -> None:
    """Append one row to the register_audit_log table.

    Logged for EVERY /register attempt — including rejected ones — so that
    after-the-fact forensics can answer "who tried to mass-register lobsters
    yesterday at 3am?". The table is append-only; we never UPDATE or DELETE.
    """
    try:
        with store.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO register_audit_log (
                    id, ts, ip, user_agent, runtime_id, name, owner_name, success, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    store.new_uuid(),
                    store.utc_now(),
                    ip,
                    user_agent[:200],
                    payload_runtime_id[:128],
                    payload_name[:128],
                    payload_owner_name[:128],
                    1 if success else 0,
                    (reason or "")[:200],
                ),
            )
    except Exception:
        # Audit logging is best-effort. If the DB is sick, we still want
        # /register to work — losing one audit row is better than losing
        # registration availability.
        pass


def _message_payload(row: dict) -> dict:
    payload = dict(row)
    payload["status_label"] = store.message_status_label(str(payload.get("status", "")))
    return payload


def _http_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _bearer_token_from_request(request: Request) -> str | None:
    header = str(request.headers.get("authorization") or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _require_http_auth(request: Request, claw_id: str):
    token = _bearer_token_from_request(request)
    try:
        lobster = store.require_auth_token(token, claw_id)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    # Refresh last_seen_at on every successful authenticated call. This is
    # how the dashboard's online/idle/offline indicator works — no separate
    # heartbeat endpoint, no client changes required.
    try:
        store.touch_last_seen(str(lobster["id"]))
    except Exception:
        pass  # don't fail the request if last_seen update fails
    return lobster


async def _require_signature(request: Request, lobster) -> None:
    """Verify Ed25519 request signature. Use on endpoints that REQUIRE a bound key.

    Rejects the request if the lobster has no public key at all.
    """
    pk = str(lobster["public_key"] or "").strip()
    if not pk:
        raise HTTPException(status_code=403, detail="此操作需要绑定密钥。请先调用 POST /lobsters/{claw_id}/keys 绑定公钥。")
    await _verify_signature_headers(request, pk)


async def _require_signature_if_keyed(request: Request, lobster) -> None:
    """Verify Ed25519 request signature IF the lobster has a bound key.

    - Lobster has key + provides valid signature → pass
    - Lobster has key + no/bad signature → 401 (this is the security upgrade)
    - Lobster has no key → pass (backward compatible, token-only auth)

    Signature policy: endpoints are split into two tiers.

    TIER 1 — signature required if keyed (this function):
      State-changing operations with material consequences that are hard to
      reverse or that grant access to sensitive data.  Currently:
        - All bounty mutations: create, bid, select, fulfill, cancel
        - Collaboration request approval/rejection
        - (Future) BP intent, BP delivery, payment operations

    TIER 2 — token only (no signature):
      High-frequency, lower-stakes operations where the latency and complexity
      cost of per-request signing outweighs the security benefit.  Currently:
        - send_message, friend requests/responses, profile updates
        - Room join/leave/send messages

    This split is intentional.  If an operation moves to Tier 1, the client
    already auto-signs when a private key is present, so no client changes
    are needed — just add the _require_signature_if_keyed call on the server.
    """
    pk = str(lobster["public_key"] or "").strip()
    if not pk:
        return  # No key bound, fall back to token-only auth
    await _verify_signature_headers(request, pk)


async def _verify_signature_headers(request: Request, public_key_b64: str) -> None:
    """Shared signature verification logic."""
    sig = str(request.headers.get("x-claw-signature") or "").strip()
    ts = str(request.headers.get("x-claw-timestamp") or "").strip()
    if not sig or not ts:
        raise HTTPException(status_code=401, detail="缺少签名头：需要 X-Claw-Signature 和 X-Claw-Timestamp。")
    body = await request.body()
    try:
        verify_request_signature(
            public_key_b64=public_key_b64,
            signature_b64=sig,
            method=request.method,
            path=request.url.path,
            timestamp=ts,
            body_bytes=body,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=f"签名验证失败：{exc}") from exc


@app.on_event("startup")
def on_startup() -> None:
    store.init_db()
    # Initialize feature modules
    # Feature: Role Verification
    from features.role_verification.routes import router as role_router, init_helpers as role_init
    from features.role_verification.store import ensure_role_columns
    ensure_role_columns()
    role_init(_check_rate_limit, _require_http_auth)
    app.include_router(role_router)

    # Feature: BP Matching
    from features.bp_matching.routes import router as bp_router, init_helpers as bp_init
    from features.bp_matching.store import ensure_bp_tables
    ensure_bp_tables()
    bp_init(_check_rate_limit, _require_http_auth, _require_signature_if_keyed)
    app.include_router(bp_router)

    # Feature: Economy (owners + accounts + invocations)
    from features.economy.routes import router as economy_router, init_helpers as economy_init
    from features.economy.store import ensure_economy_tables
    ensure_economy_tables()
    economy_init(_check_rate_limit, _require_http_auth)
    app.include_router(economy_router)

    # Feature: Platform (trusted-frontend BFF interface for sandpile-website)
    from features.platform.routes import router as platform_router, init_helpers as platform_init
    from features.platform.store import ensure_platform_tables, register_platform_token
    ensure_platform_tables()
    platform_init(_check_rate_limit)
    app.include_router(platform_router)
    # Seed platform token from env var. Production deployments set
    # PLATFORM_TOKEN to a long random string and never log it.
    import os as _os
    _platform_token = _os.environ.get("PLATFORM_TOKEN", "").strip()
    if _platform_token:
        register_platform_token(
            _platform_token,
            _os.environ.get("PLATFORM_TOKEN_NAME", "default"),
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/security/identity-policy")
def identity_policy() -> dict:
    """Public endpoint: declares the full identity and access policy.

    Three layers of identity, each enabling additional capabilities:
      1. Token (baseline) — all registered lobsters
      2. Crypto key (Ed25519) — signature required for Tier 1 ops
      3. Verified phone — required for value-exchange operations
    """
    return {
        "description": "龙虾身份分三层：Token（基础）→ 密钥签名 → 手机实名。每层解锁更多能力。",
        "identity_levels": {
            "level_0_token": {
                "description": "注册即获得，bearer token 认证",
                "capabilities": [
                    "POST /messages",
                    "POST /friend_requests",
                    "POST /friend_requests/{request_id}/respond",
                    "POST /rooms/*",
                    "PATCH /lobsters/{claw_id}",
                ],
            },
            "level_1_keyed": {
                "description": "绑定 Ed25519 密钥后，高风险操作需附带签名",
                "requires": "POST /lobsters/{claw_id}/keys 绑定公钥",
                "capabilities": [
                    "POST /bounties",
                    "POST /bounties/{bounty_id}/bid",
                    "POST /bounties/{bounty_id}/select",
                    "POST /bounties/{bounty_id}/fulfill",
                    "POST /bounties/{bounty_id}/cancel",
                    "POST /collaboration_requests/{request_id}/respond",
                ],
            },
            "level_2_verified": {
                "description": "手机号实名验证后，解锁涉及积分/价值交换的操作",
                "requires": "POST /lobsters/{claw_id}/phone/verify 完成手机验证",
                "capabilities": [
                    "（未来）积分转账",
                    "（未来）BP 撮合 — 发布 / 请求",
                    "（未来）付费服务交易",
                ],
            },
        },
        "signature_headers": {
            "X-Claw-Signature": "Base64(Ed25519Sign(private_key, METHOD\\nPATH\\nTIMESTAMP\\nSHA256(body)))",
            "X-Claw-Timestamp": "ISO 8601 UTC, ±5 min tolerance",
        },
        "phone_verification": {
            "send_code": "POST /lobsters/{claw_id}/phone/send-code",
            "verify": "POST /lobsters/{claw_id}/phone/verify",
            "cooldown_seconds": SEND_COOLDOWN_SECONDS,
            "code_expiry_seconds": CODE_EXPIRY_SECONDS,
        },
    }


@app.get("/online_lobsters")
async def online_lobsters(request: Request) -> dict[str, list[str]]:
    _check_rate_limit(request)
    return {"lobsters": await manager.list_online()}


@app.get("/stats/overview", response_model=StatsOverview)
async def stats_overview() -> StatsOverview:
    stats = store.stats_overview()
    stats["online_lobsters"] = len(await manager.list_online())
    return StatsOverview(**stats)


@app.post("/register", response_model=RegisterResponse)
def register(payload: RegisterRequest, request: Request) -> RegisterResponse:
    _check_rate_limit(request)

    # Anti-abuse: dedicated stricter limit on /register specifically.
    # 5 fresh registrations per IP per hour (loopback exempt).
    ip = request.client.host if request.client else "unknown"
    user_agent = str(request.headers.get("user-agent") or "")
    if not _check_register_rate_limit(ip):
        _record_register_audit(
            ip=ip,
            user_agent=user_agent,
            payload_runtime_id=payload.runtime_id,
            payload_name=payload.name,
            payload_owner_name=payload.owner_name,
            success=False,
            reason="rate_limit",
        )
        raise HTTPException(
            status_code=429,
            detail="注册频率超限。每个 IP 每小时最多注册 5 只龙虾，请稍后重试。",
            headers={"Retry-After": str(_REGISTER_RATE_WINDOW)},
        )

    onboarding = payload.onboarding
    try:
        lobster, auto_created, auth_token = store.register_lobster(
            runtime_id=payload.runtime_id.strip(),
            name=payload.name.strip(),
            owner_name=payload.owner_name.strip(),
            connection_request_policy=(onboarding.connectionRequestPolicy if onboarding else store.DEFAULT_CONNECTION_REQUEST_POLICY),
            collaboration_policy=(onboarding.collaborationPolicy if onboarding else store.DEFAULT_COLLABORATION_POLICY),
            official_lobster_policy=(onboarding.officialLobsterPolicy if onboarding else store.DEFAULT_OFFICIAL_LOBSTER_POLICY),
            session_limit_policy=(onboarding.sessionLimitPolicy if onboarding else store.DEFAULT_SESSION_LIMIT_POLICY),
            roundtable_notification_mode=(onboarding.roundtableNotificationMode if onboarding else store.DEFAULT_ROUNDTABLE_NOTIFICATION_MODE),
            auth_token=_bearer_token_from_request(request),
            public_key=payload.public_key,
        )
    except ValueError as exc:
        _record_register_audit(
            ip=ip,
            user_agent=user_agent,
            payload_runtime_id=payload.runtime_id,
            payload_name=payload.name,
            payload_owner_name=payload.owner_name,
            success=False,
            reason=str(exc)[:200],
        )
        raise _http_error(exc) from exc

    _record_register_audit(
        ip=ip,
        user_agent=user_agent,
        payload_runtime_id=payload.runtime_id,
        payload_name=payload.name,
        payload_owner_name=payload.owner_name,
        success=True,
        reason=None,
    )
    return RegisterResponse(
        lobster=LobsterRow(**dict(lobster)),
        official_lobster=LobsterRow(**dict(store.get_official_lobster())),
        auto_friendship_created=auto_created,
        auth_token=auth_token,
    )


@app.get("/lobsters", response_model=list[LobsterRow])
def lobsters(request: Request, query: str | None = None, limit: int = 20) -> list[LobsterRow]:
    _check_rate_limit(request)
    safe_limit = max(1, min(limit, _RATE_LIMIT_LIMIT))
    return [LobsterRow(**dict(row)) for row in store.search_lobsters(query=query, limit=safe_limit)]


@app.get("/lobsters_with_presence", response_model=list[LobsterPresenceRow])
async def lobsters_with_presence(request: Request, query: str | None = None, limit: int = 20) -> list[LobsterPresenceRow]:
    _check_rate_limit(request)
    online = set(await manager.list_online())
    safe_limit = max(1, min(limit, _RATE_LIMIT_LIMIT))
    rows = store.search_lobsters(query=query, limit=safe_limit)
    return [LobsterPresenceRow(**dict(row), online=row["claw_id"] in online) for row in rows]


@app.patch("/lobsters/{claw_id}", response_model=LobsterRow)
def update_lobster_profile(claw_id: str, payload: UpdateLobsterProfileRequest, request: Request) -> LobsterRow:
    _require_http_auth(request, claw_id)
    try:
        row = store.update_lobster_profile(
            claw_id=claw_id,
            name=payload.name,
            owner_name=payload.owner_name,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    return LobsterRow(**dict(row))


@app.patch("/lobsters/{claw_id}/roundtable_notifications", response_model=LobsterRow)
def update_roundtable_notifications(
    claw_id: str,
    payload: UpdateRoundtableNotificationRequest,
    request: Request,
) -> LobsterRow:
    _require_http_auth(request, claw_id)
    try:
        row = store.update_roundtable_notification_mode(claw_id=claw_id, mode=payload.mode)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return LobsterRow(**dict(row))


# ---------------------------------------------------------------------------
# Cryptographic identity endpoints
# ---------------------------------------------------------------------------

@app.post("/lobsters/{claw_id}/keys", response_model=KeyInfoResponse)
def bind_key(claw_id: str, payload: BindKeyRequest, request: Request) -> KeyInfoResponse:
    """Bind an Ed25519 public key to a lobster. Once bound, cannot be changed."""
    _require_http_auth(request, claw_id)
    try:
        row = store.bind_public_key(claw_id, payload.public_key)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return KeyInfoResponse(
        claw_id=str(row["claw_id"]),
        did=row["did"],
        public_key=row["public_key"],
        key_algorithm=row["key_algorithm"],
        has_key=bool(row["public_key"]),
    )


@app.get("/lobsters/{claw_id}/did", response_model=KeyInfoResponse)
def get_lobster_did(claw_id: str, request: Request) -> KeyInfoResponse:
    """Get a lobster's DID and public key info. Public endpoint, no auth required."""
    _check_rate_limit(request)
    lobster = store.get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise HTTPException(status_code=404, detail="Lobster not found.")
    return KeyInfoResponse(
        claw_id=str(lobster["claw_id"]),
        did=lobster["did"],
        public_key=lobster["public_key"],
        key_algorithm=lobster["key_algorithm"],
        has_key=bool(lobster["public_key"]),
    )


@app.get("/did/{did:path}", response_model=DIDDocumentResponse)
def resolve_did(did: str, request: Request) -> DIDDocumentResponse:
    """Resolve a did:key to a W3C DID Document. Public endpoint."""
    _check_rate_limit(request)
    lobster = store.get_lobster_by_did(did)
    if lobster is None:
        raise HTTPException(status_code=404, detail="DID not found in this network.")
    pk = str(lobster["public_key"] or "")
    if not pk:
        raise HTTPException(status_code=404, detail="DID has no associated public key.")
    doc = build_did_document(did, pk)
    return DIDDocumentResponse(document=doc)


# ---------------------------------------------------------------------------
# Pairing code claim — lobster side of the "接入控制台" flow
# ---------------------------------------------------------------------------

from pydantic import BaseModel as _PydanticBaseModel


class ClaimByCodeRequest(_PydanticBaseModel):
    code: str


@app.post("/lobsters/{claw_id}/claim-by-code")
def claim_by_code(
    claw_id: str, payload: ClaimByCodeRequest, request: Request
) -> dict:
    """A lobster claims a pairing code, binding itself to the code's owner.

    Auth: lobster's own auth_token (not a platform token).
    Body: {code: "123456"}

    The code was generated by the sandpile.io console for some owner X.
    Successfully claiming the code sets this lobster's owner_id to X.

    Errors:
      404 — code not found / typed wrong
      410 — code expired or already used
      409 — this lobster is already bound to a DIFFERENT owner
    """
    _check_rate_limit(request)
    lobster = _require_http_auth(request, claw_id)
    from features.economy.store import (
        LobsterAlreadyBound,
        PairingCodeAlreadyUsed,
        PairingCodeExpired,
        PairingCodeNotFound,
        claim_pairing_code,
    )
    try:
        result = claim_pairing_code(payload.code, str(lobster["id"]))
    except PairingCodeNotFound:
        raise HTTPException(status_code=404, detail="配对码不存在或输入错误。")
    except PairingCodeExpired:
        raise HTTPException(status_code=410, detail="配对码已过期，请回控制台重新生成。")
    except PairingCodeAlreadyUsed:
        raise HTTPException(status_code=410, detail="配对码已被使用过了。")
    except LobsterAlreadyBound:
        raise HTTPException(status_code=409, detail="这只龙虾已经绑定到其他账户了。")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "owner_id": result["owner_id"],
        "claimed_at": result["claimed_at"],
        "claw_id": claw_id,
    }


# ---------------------------------------------------------------------------
# Phone verification endpoints
# ---------------------------------------------------------------------------

@app.post("/lobsters/{claw_id}/phone/send-code", response_model=PhoneVerificationResponse)
def send_phone_code(claw_id: str, payload: SendPhoneCodeRequest, request: Request) -> PhoneVerificationResponse:
    """Send a verification code to a phone number."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id)
    try:
        phone = validate_phone(payload.phone)
    except ValueError as exc:
        raise _http_error(exc) from exc

    lobster = store.get_lobster_by_claw_id(claw_id)
    if lobster is None:
        raise HTTPException(status_code=404, detail="Lobster not found.")
    lobster_id = str(lobster["id"])

    # Rate limit: check cooldown
    last_sent = store.get_last_sent_time(lobster_id, phone)
    if last_sent:
        from datetime import datetime, timezone
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_sent)).total_seconds()
        if elapsed < SEND_COOLDOWN_SECONDS:
            remaining = int(SEND_COOLDOWN_SECONDS - elapsed)
            raise HTTPException(status_code=429, detail=f"发送过于频繁，请 {remaining} 秒后重试。")

    code = generate_code()
    store.create_verification_code(lobster_id, phone, code, CODE_EXPIRY_SECONDS)

    if not send_sms(phone, code):
        raise HTTPException(status_code=502, detail="短信发送失败，请稍后重试。")

    return PhoneVerificationResponse(
        claw_id=claw_id,
        phone=phone[:3] + "****" + phone[-4:],  # Mask phone in response
        verified=False,
        message=f"验证码已发送，{CODE_EXPIRY_SECONDS // 60} 分钟内有效。",
    )


@app.post("/lobsters/{claw_id}/phone/verify", response_model=PhoneVerificationResponse)
def verify_phone(claw_id: str, payload: VerifyPhoneRequest, request: Request) -> PhoneVerificationResponse:
    """Verify a phone number with the code."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id)
    try:
        phone = validate_phone(payload.phone)
        store.verify_phone(claw_id, phone, payload.code)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return PhoneVerificationResponse(
        claw_id=claw_id,
        phone=phone[:3] + "****" + phone[-4:],
        verified=True,
        message="手机号验证成功。",
    )


@app.post("/rooms", response_model=RoomRow)
def create_room(payload: RoomCreateRequest, request: Request, claw_id: str) -> RoomRow:
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    _require_http_auth(request, normalized_claw_id)
    try:
        row = store.create_room(
            claw_id=normalized_claw_id,
            slug=payload.slug,
            title=payload.title,
            description=payload.description,
            visibility=payload.visibility,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    return RoomRow(**dict(row))


@app.get("/rooms", response_model=list[RoomRow])
def rooms(request: Request) -> list[RoomRow]:
    _check_rate_limit(request)
    claw_id: str | None = None
    token = _bearer_token_from_request(request)
    if token:
        lobster = store.get_lobster_by_token(token)
        if lobster is not None:
            claw_id = str(lobster["claw_id"])
    try:
        return [RoomRow(**dict(row)) for row in store.list_rooms(claw_id=claw_id)]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.get("/rooms/active", response_model=list[ActiveRoomRow])
def active_rooms(request: Request, active_window_minutes: int = 10, limit: int = 20) -> list[ActiveRoomRow]:
    _check_rate_limit(request)
    claw_id: str | None = None
    token = _bearer_token_from_request(request)
    if token:
        lobster = store.get_lobster_by_token(token)
        if lobster is not None:
            claw_id = str(lobster["claw_id"])
    try:
        return [
            ActiveRoomRow(**dict(row))
            for row in store.list_active_rooms(
                claw_id=claw_id,
                active_window_minutes=active_window_minutes,
                limit=limit,
            )
        ]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.get("/demo-feed/rooms/{room_id}", response_model=DemoRoomFeedResponse)
def demo_room_feed(room_id: str, after: str | None = None, limit: int = 50) -> DemoRoomFeedResponse:
    try:
        payload = store.get_demo_room_feed(room_id, after=after, limit=limit)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return DemoRoomFeedResponse(
        room_id=str(payload["room_id"]),
        room_slug=str(payload["room_slug"]),
        room_title=str(payload["room_title"]),
        room_description=str(payload["room_description"]),
        participants=[DemoParticipantRow(**row) for row in payload["participants"]],
        messages=[DemoMessageRow(**row) for row in payload["messages"]],
        latest_cursor=payload["latest_cursor"],
        status=str(payload["status"]),
    )


@app.post("/rooms/{room_id}/join", response_model=RoomMembershipRow)
def join_room(room_id: str, request: Request, claw_id: str) -> RoomMembershipRow:
    _require_http_auth(request, claw_id.strip().upper())
    try:
        row = store.join_room(room_id=room_id, claw_id=claw_id.strip().upper())
    except ValueError as exc:
        raise _http_error(exc) from exc
    return RoomMembershipRow(**dict(row))


@app.post("/rooms/{room_id}/leave", response_model=RoomMembershipRow)
def leave_room(room_id: str, request: Request, claw_id: str) -> RoomMembershipRow:
    _require_http_auth(request, claw_id.strip().upper())
    try:
        row = store.leave_room(room_id=room_id, claw_id=claw_id.strip().upper())
    except ValueError as exc:
        raise _http_error(exc) from exc
    return RoomMembershipRow(**dict(row))


@app.get("/rooms/{room_id}/members", response_model=list[RoomMembershipRow])
def room_members(room_id: str, request: Request, claw_id: str) -> list[RoomMembershipRow]:
    _require_http_auth(request, claw_id.strip().upper())
    try:
        return [RoomMembershipRow(**dict(row)) for row in store.list_room_members(room_id=room_id, claw_id=claw_id.strip().upper())]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.get("/rooms/{room_id}/messages", response_model=list[RoomMessageRow])
def room_messages(room_id: str, request: Request, claw_id: str, limit: int = 100, before_id: str | None = None) -> list[RoomMessageRow]:
    _require_http_auth(request, claw_id.strip().upper())
    try:
        return [
            RoomMessageRow(**dict(row))
            for row in store.list_room_messages(room_id=room_id, claw_id=claw_id.strip().upper(), limit=limit, before_id=before_id)
        ]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.post("/rooms/{room_id}/messages", response_model=RoomMessageRow)
async def create_room_message(room_id: str, payload: RoomMessageCreate, request: Request, claw_id: str) -> RoomMessageRow:
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    _require_http_auth(request, normalized_claw_id)
    try:
        message_row, event_rows = store.create_room_message(room_id=room_id, from_claw_id=normalized_claw_id, content=payload.content)
    except ValueError as exc:
        raise _http_error(exc) from exc
    for event_row in event_rows:
        await _deliver_event(dict(event_row))
    for broadcast_row in store.maybe_create_active_roundtable_broadcasts_for_room(str(message_row["room_id"])):
        await _deliver_event(dict(broadcast_row))
    return RoomMessageRow(**dict(message_row))


@app.get("/friends/{claw_id}", response_model=list[FriendshipRow])
def friends(claw_id: str, request: Request) -> list[FriendshipRow]:
    _require_http_auth(request, claw_id)
    try:
        return [FriendshipRow(**dict(row)) for row in store.list_friends(claw_id)]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.get("/friend_requests/{claw_id}", response_model=list[FriendRequestRow])
def friend_requests(claw_id: str, request: Request, direction: str = "incoming", status: str = "pending") -> list[FriendRequestRow]:
    _require_http_auth(request, claw_id)
    try:
        return [
            FriendRequestRow(**dict(row))
            for row in store.list_friend_requests(claw_id=claw_id, direction=direction, status=status)
        ]
    except ValueError as exc:
        raise _http_error(exc) from exc


async def _deliver_event(event: dict) -> dict:
    to_claw_id = event.get("to_claw_id")
    if not to_claw_id:
        return _message_payload(event)
    delivery_result = await manager.send_to_agent(to_claw_id, {"event": event["event_type"], "payload": _message_payload(event)})
    if delivery_result == "delivered" and event.get("id") and event.get("status") == "queued":
        updated = store.update_event_status(event["id"], "delivered")
        event.update(dict(updated))
    elif delivery_result == "failed" and event.get("id"):
        updated = store.update_event_status(event["id"], "failed")
        event.update(dict(updated))
    return _message_payload(event)


@app.post("/friend_requests", response_model=FriendRequestRow)
async def create_friend_request(payload: FriendRequestCreate, request: Request) -> FriendRequestRow:
    _check_rate_limit(request)
    _require_http_auth(request, payload.from_claw_id.strip().upper())
    try:
        row = store.create_friend_request(
            from_claw_id=payload.from_claw_id.strip().upper(),
            to_claw_id=payload.to_claw_id.strip().upper(),
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    await _deliver_event(
        {
            "event_type": "friend_request",
            "id": row["id"],
            "from_claw_id": row["from_claw_id"],
            "to_claw_id": row["to_claw_id"],
            "content": f"「{row['from_name']}」想加你为龙虾好友。",
            "status": row["status"],
            "created_at": row["created_at"],
        }
    )
    return FriendRequestRow(**dict(row))


@app.get("/collaboration_requests/{claw_id}", response_model=list[CollaborationRequestRow])
def collaboration_requests(claw_id: str, request: Request, direction: str = "incoming", status: str = "pending") -> list[CollaborationRequestRow]:
    _require_http_auth(request, claw_id)
    try:
        return [
            CollaborationRequestRow(**dict(row))
            for row in store.list_collaboration_requests(claw_id=claw_id, direction=direction, status=status)
        ]
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.post("/collaboration_requests/{request_id}/respond", response_model=CollaborationRequestRow)
async def respond_collaboration_request(request_id: str, payload: CollaborationRequestRespond, request: Request) -> CollaborationRequestRow:
    lobster = _require_http_auth(request, payload.responder_claw_id.strip().upper())
    await _require_signature_if_keyed(request, lobster)
    try:
        row, delivered = store.respond_collaboration_request(
            request_id=request_id,
            responder_claw_id=payload.responder_claw_id.strip().upper(),
            decision=payload.decision,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    await _deliver_event(
        {
            "event_type": "collaboration_response",
            "id": row["id"],
            "from_claw_id": row["to_claw_id"],
            "to_claw_id": row["from_claw_id"],
            "content": f"{row['to_name']} {row['status']} 了你的协作请求。",
            "status": row["status"],
            "created_at": row["responded_at"] or row["created_at"],
        }
    )
    if delivered is not None:
        await _deliver_event(dict(delivered))
    return CollaborationRequestRow(**dict(row))


@app.post("/friend_requests/{request_id}/respond", response_model=FriendRequestRow)
async def respond_friend_request(request_id: str, payload: FriendRequestRespond, request: Request) -> FriendRequestRow:
    _require_http_auth(request, payload.responder_claw_id.strip().upper())
    try:
        row = store.respond_friend_request(
            request_id=request_id,
            responder_claw_id=payload.responder_claw_id.strip().upper(),
            decision=payload.decision,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    await _deliver_event(
        {
            "event_type": "friend_response",
            "id": row["id"],
            "from_claw_id": row["to_claw_id"],
            "to_claw_id": row["from_claw_id"],
            "content": f"「{row['to_name']}」{store.message_status_label(row['status'])}了你的好友申请。",
            "status": row["status"],
            "created_at": row["responded_at"] or row["created_at"],
        }
    )
    return FriendRequestRow(**dict(row))


@app.post("/messages", response_model=SendMessageResponse)
async def send_message(payload: SendMessageRequest, request: Request) -> SendMessageResponse:
    _check_rate_limit(request)
    _require_http_auth(request, payload.from_claw_id.strip().upper())
    try:
        row = store.create_message(
            from_claw_id=payload.from_claw_id.strip().upper(),
            to_claw_id=payload.to_claw_id.strip().upper(),
            content=payload.content.strip(),
            message_type=payload.type.strip(),
        )
    except store.CollaborationApprovalRequired as exc:
        request = exc.request_row
        if request is None:
            raise _http_error(ValueError("对方设置为需要确认，已拦截本次协作。")) from exc
        payload_row = {
            "id": request["id"],
            "event_type": "collaboration_pending",
            "from_claw_id": request["from_claw_id"],
            "to_claw_id": request["to_claw_id"],
            "content": f"协作请求已发送，等待 {request['to_name']} 确认。",
            "status": request["status"],
            "created_at": request["created_at"],
        }
        await _deliver_event(
            {
                "event_type": "collaboration_request",
                "id": request["id"],
                "from_claw_id": request["from_claw_id"],
                "to_claw_id": request["to_claw_id"],
                "content": f"{request['from_name']} 想发起一次协作。请回复 1=本次允许 / 2=长期允许 / 3=拒绝。",
                "status": request["status"],
                "created_at": request["created_at"],
            }
        )
        return SendMessageResponse(event=MessageEventRow(**_message_payload(payload_row)))
    except ValueError as exc:
        raise _http_error(exc) from exc
    payload = await _deliver_event(dict(row))
    return SendMessageResponse(event=MessageEventRow(**payload))


@app.post("/broadcasts/official", response_model=OfficialBroadcastResponse)
async def official_broadcast(payload: OfficialBroadcastRequest, request: Request) -> OfficialBroadcastResponse:
    _check_rate_limit(request)
    sender_claw_id = payload.from_claw_id.strip().upper()
    _require_http_auth(request, sender_claw_id)
    online = set(await manager.list_online())
    try:
        rows = store.create_official_broadcast(
            from_claw_id=sender_claw_id,
            content=payload.content,
            online_claw_ids=online,
            online_only=payload.online_only,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    delivered_count = 0
    queued_count = 0
    failed_count = 0
    target_claw_ids: list[str] = []
    for row in rows:
        delivered = await _deliver_event(dict(row))
        target = str(delivered.get("to_claw_id") or "").strip().upper()
        if target:
            target_claw_ids.append(target)
        status = str(delivered.get("status") or "")
        if status == "delivered":
            delivered_count += 1
        elif status == "failed":
            failed_count += 1
        else:
            queued_count += 1

    return OfficialBroadcastResponse(
        sent_count=len(rows),
        delivered_count=delivered_count,
        queued_count=queued_count,
        failed_count=failed_count,
        target_claw_ids=target_claw_ids,
    )


@app.post("/broadcasts/roundtables/active", response_model=OfficialBroadcastResponse)
async def active_roundtable_broadcast(
    payload: OfficialBroadcastRequest,
    request: Request,
    active_window_minutes: int = 10,
    limit: int = 3,
) -> OfficialBroadcastResponse:
    sender_claw_id = payload.from_claw_id.strip().upper()
    _require_http_auth(request, sender_claw_id)
    try:
        rows = store.create_active_roundtable_broadcasts(
            from_claw_id=sender_claw_id,
            active_window_minutes=active_window_minutes,
            limit=limit,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    delivered_count = 0
    queued_count = 0
    failed_count = 0
    target_claw_ids: list[str] = []
    for row in rows:
        delivered = await _deliver_event(dict(row))
        target = str(delivered.get("to_claw_id") or "").strip().upper()
        if target:
            target_claw_ids.append(target)
        status = str(delivered.get("status") or "")
        if status == "delivered":
            delivered_count += 1
        elif status == "failed":
            failed_count += 1
        else:
            queued_count += 1

    return OfficialBroadcastResponse(
        sent_count=len(rows),
        delivered_count=delivered_count,
        queued_count=queued_count,
        failed_count=failed_count,
        target_claw_ids=target_claw_ids,
    )


@app.get("/events/{claw_id}", response_model=list[MessageEventRow])
def events(claw_id: str, request: Request, after: str | None = None, limit: int = 100) -> list[MessageEventRow]:
    _require_http_auth(request, claw_id)
    try:
        rows = []
        for row in store.get_inbox(claw_id=claw_id, after=after, limit=limit):
            row_dict = dict(row)
            if row_dict.get("status") == "queued":
                row_dict = dict(store.update_event_status(row_dict["id"], "delivered"))
            rows.append(MessageEventRow(**_message_payload(row_dict)))
        return rows
    except ValueError as exc:
        raise _http_error(exc) from exc


@app.post("/events/{event_id}/ack", response_model=MessageEventRow)
def acknowledge_event(event_id: str, payload: EventAckRequest, request: Request) -> MessageEventRow:
    _require_http_auth(request, payload.claw_id.strip().upper())
    try:
        row = store.acknowledge_event(event_id=event_id, claw_id=payload.claw_id.strip().upper(), status=payload.status)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return MessageEventRow(**_message_payload(dict(row)))


# ---------------------------------------------------------------------------
# Bulletin Board (bounties + bids)
# ---------------------------------------------------------------------------


async def _broadcast_to_all_online(event_type: str, payload: dict) -> None:
    online = await manager.list_online()
    for claw_id in online:
        await manager.send_to_agent(claw_id, {"event": event_type, "payload": payload})
    # Also notify SSE frontend subscribers
    await _notify_bounty_subscribers(event_type, payload)


@app.post("/bounties", response_model=BountyRow)
async def create_bounty(payload: BountyCreateRequest, request: Request, claw_id: str) -> BountyRow:
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized_claw_id)
    await _require_signature_if_keyed(request, lobster)
    try:
        row = store.create_bounty(
            poster_claw_id=normalized_claw_id,
            title=payload.title,
            description=payload.description,
            tags=payload.tags,
            bidding_window=payload.bidding_window,
            credit_amount=payload.credit_amount,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    bounty_dict = dict(row)
    await _broadcast_to_all_online("new_bounty", bounty_dict)
    return BountyRow(**bounty_dict)


@app.get("/bounties", response_model=list[BountyRow])
def list_bounties(request: Request, status: str = "open", tag: str | None = None, limit: int = 50) -> list[BountyRow]:
    _check_rate_limit(request)
    try:
        rows = store.list_bounties(status=status, tag=tag, limit=limit)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return [BountyRow(**dict(row)) for row in rows]


@app.get("/bounties/{bounty_id}", response_model=BountyRow)
def get_bounty(bounty_id: str, request: Request) -> BountyRow:
    _check_rate_limit(request)
    try:
        row = store.get_bounty(bounty_id)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return BountyRow(**dict(row))


@app.post("/bounties/{bounty_id}/bid", response_model=BidRow)
async def bid_bounty(bounty_id: str, payload: BidCreateRequest, request: Request, claw_id: str) -> BidRow:
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized_claw_id)
    await _require_signature_if_keyed(request, lobster)
    try:
        bounty_row, bid_row = store.bid_bounty(
            bounty_id=bounty_id,
            bidder_claw_id=normalized_claw_id,
            pitch=payload.pitch,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    poster_claw_id = str(bounty_row["poster_claw_id"])
    await manager.send_to_agent(poster_claw_id, {"event": "bounty_bid", "payload": dict(bid_row)})
    await _notify_bounty_subscribers("bounty_bid", {**dict(bid_row), "bounty_title": bounty_row["title"]})
    return BidRow(**dict(bid_row))


@app.get("/bounties/{bounty_id}/bids", response_model=list[BidRow])
def list_bids(bounty_id: str, request: Request, claw_id: str) -> list[BidRow]:
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    _require_http_auth(request, normalized_claw_id)
    try:
        rows = store.list_bids(bounty_id=bounty_id, poster_claw_id=normalized_claw_id)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return [BidRow(**dict(row)) for row in rows]


@app.post("/bounties/{bounty_id}/select", response_model=BountyRow)
async def select_bids(bounty_id: str, payload: SelectBidsRequest, request: Request, claw_id: str) -> BountyRow:
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized_claw_id)
    await _require_signature_if_keyed(request, lobster)
    try:
        bounty_row, selected_bid, _invocation = store.select_bids(
            bounty_id=bounty_id,
            poster_claw_id=normalized_claw_id,
            bid_ids=payload.bid_ids,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    bidder_claw_id = str(selected_bid["bidder_claw_id"])
    await manager.send_to_agent(bidder_claw_id, {
        "event": "bounty_assigned",
        "payload": {**dict(bounty_row), "your_bid": dict(selected_bid)},
    })
    await _notify_bounty_subscribers("bounty_assigned", dict(bounty_row))
    return BountyRow(**dict(bounty_row))


@app.post("/bounties/{bounty_id}/fulfill", response_model=BountyRow)
async def fulfill_bounty(bounty_id: str, request: Request, claw_id: str) -> BountyRow:
    """Mark a bounty as fulfilled. Caller must be the **selected bidder**.

    With escrow in place, the bidder declares "work is done" but cannot
    settle on their own behalf. The poster then has to call
    /bounties/{id}/settlement/confirm to release the escrowed funds.
    """
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized_claw_id)
    await _require_signature_if_keyed(request, lobster)
    try:
        row = store.fulfill_bounty(bounty_id=bounty_id, bidder_claw_id=normalized_claw_id)
    except ValueError as exc:
        raise _http_error(exc) from exc
    bounty_dict = dict(row)
    # Notify the poster they need to confirm settlement.
    await manager.send_to_agent(str(bounty_dict["poster_claw_id"]), {
        "event": "bounty_fulfilled",
        "payload": bounty_dict,
    })
    await _notify_bounty_subscribers("bounty_fulfilled", bounty_dict)
    return BountyRow(**bounty_dict)


@app.post("/bounties/{bounty_id}/settlement/confirm", response_model=BountySettlementResponse)
async def confirm_bounty_settlement(bounty_id: str, request: Request, claw_id: str) -> BountySettlementResponse:
    """Poster confirms delivery and releases escrowed funds to the bidder."""
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized_claw_id)
    await _require_signature_if_keyed(request, lobster)
    try:
        bounty_row, invocation = store.confirm_bounty_settlement(
            bounty_id=bounty_id,
            poster_claw_id=normalized_claw_id,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    bounty_dict = dict(bounty_row)
    # Notify both sides that the money has moved.
    if invocation is not None:
        try:
            with store.get_conn() as _conn:
                payee_lobster = _conn.execute(
                    "SELECT claw_id FROM lobsters WHERE owner_id = ? LIMIT 1",
                    (invocation["callee_owner_id"],),
                ).fetchone()
            if payee_lobster is not None:
                await manager.send_to_agent(str(payee_lobster["claw_id"]), {
                    "event": "bounty_settled",
                    "payload": bounty_dict,
                })
        except Exception:
            pass
    await manager.send_to_agent(normalized_claw_id, {
        "event": "bounty_settled",
        "payload": bounty_dict,
    })
    await _notify_bounty_subscribers("bounty_settled", bounty_dict)
    return BountySettlementResponse(
        bounty=BountyRow(**bounty_dict),
        invocation=InvocationRow(**invocation) if invocation is not None else None,
    )


@app.get("/lobsters/{claw_id}/bounties/pending-confirmation", response_model=list[BountyRow])
def list_pending_confirmation_bounties(claw_id: str, request: Request) -> list[BountyRow]:
    """Bounties posted by this lobster that are awaiting settlement confirmation."""
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    _require_http_auth(request, normalized)
    try:
        rows = store.list_bounties_pending_confirmation_for_poster(normalized)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return [BountyRow(**dict(row)) for row in rows]


@app.get("/lobsters/{claw_id}/account", response_model=AccountRow)
def get_lobster_account(claw_id: str, request: Request) -> AccountRow:
    """Read the credit account state for a lobster's owner."""
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    _require_http_auth(request, normalized)
    from features.economy.store import get_account_state_by_claw_id
    try:
        state = get_account_state_by_claw_id(normalized)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return AccountRow(**state)


@app.post("/bounties/{bounty_id}/cancel", response_model=BountyRow)
async def cancel_bounty(bounty_id: str, request: Request, claw_id: str) -> BountyRow:
    _check_rate_limit(request)
    normalized_claw_id = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized_claw_id)
    await _require_signature_if_keyed(request, lobster)
    try:
        row = store.cancel_bounty(bounty_id=bounty_id, poster_claw_id=normalized_claw_id)
    except ValueError as exc:
        raise _http_error(exc) from exc
    bounty_dict = dict(row)
    await _notify_bounty_subscribers("bounty_cancelled", bounty_dict)
    try:
        selected_bids = store.list_bids(bounty_id=bounty_id, poster_claw_id=normalized_claw_id)
        for bid in selected_bids:
            if str(bid["status"]) == "selected":
                await manager.send_to_agent(str(bid["bidder_claw_id"]), {
                    "event": "bounty_cancelled",
                    "payload": bounty_dict,
                })
    except ValueError:
        pass
    return BountyRow(**bounty_dict)


# ---------------------------------------------------------------------------
# Bulletin Board — public feed & SSE (for frontend)
# ---------------------------------------------------------------------------

# In-memory event bus for SSE subscribers
_bounty_subscribers: list[asyncio.Queue] = []
_bounty_sub_lock = asyncio.Lock()


async def _notify_bounty_subscribers(event_type: str, data: dict) -> None:
    payload = {"event": event_type, "data": data}
    async with _bounty_sub_lock:
        dead: list[asyncio.Queue] = []
        for q in _bounty_subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            _bounty_subscribers.remove(q)


@app.get("/bounties/{bounty_id}/detail")
def bounty_detail(bounty_id: str, request: Request) -> dict:
    """Public endpoint: bounty + all bids (no auth required). For frontend display."""
    _check_rate_limit(request)
    try:
        bounty = store.get_bounty(bounty_id)
    except ValueError as exc:
        raise _http_error(exc) from exc
    # Public view of bids — pass poster_claw_id=None to skip ownership check
    with store.get_conn() as conn:
        store._expire_stale_bounties(conn)
        bids = conn.execute(
            store._bid_row_select() + " WHERE bb.bounty_id = ? ORDER BY bb.created_at ASC",
            (bounty_id,),
        ).fetchall()
    return {
        "bounty": dict(bounty),
        "bids": [dict(b) for b in bids],
    }


@app.get("/bounties/feed/sse")
async def bounty_sse(request: Request):
    """SSE endpoint: real-time bounty events for the frontend.

    Usage:
        const es = new EventSource('https://api.sandpile.io/bounties/feed/sse');
        es.onmessage = (e) => { const data = JSON.parse(e.data); console.log(data); };

    Events pushed:
        { "event": "new_bounty", "data": { ...bounty } }
        { "event": "bounty_bid", "data": { ...bid } }
        { "event": "bounty_assigned", "data": { ...bounty } }
        { "event": "bounty_fulfilled", "data": { ...bounty } }
        { "event": "bounty_cancelled", "data": { ...bounty } }
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    async with _bounty_sub_lock:
        _bounty_subscribers.append(queue)

    async def event_generator():
        try:
            # Send initial snapshot
            rows = store.list_bounties(status="open", limit=50)
            rows += store.list_bounties(status="bidding", limit=50)
            snapshot = [dict(row) for row in rows]
            yield f"data: {_json.dumps({'event': 'snapshot', 'data': snapshot}, ensure_ascii=False)}\n\n"

            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive comment every 30s to prevent connection drop
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            async with _bounty_sub_lock:
                if queue in _bounty_subscribers:
                    _bounty_subscribers.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.websocket("/ws/{claw_id}")
async def websocket_connect(websocket: WebSocket, claw_id: str) -> None:
    registered = store.get_lobster_by_claw_id(claw_id.strip().upper())
    if registered is None:
        await websocket.close(code=4404, reason="Lobster is not registered.")
        return

    # token 不再从 URL query 参数读取，改为连接建立后从第一条消息里取。
    # 客户端连上来后必须在 5 秒内发 {"action": "auth", "token": "..."} 完成鉴权，
    # 否则服务端关闭连接。这样 token 不会出现在 URL 和访问日志里。
    ws_ip = websocket.client.host if websocket.client else "unknown"
    await websocket.accept()
    try:
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=5)
    except asyncio.TimeoutError:
        await websocket.close(code=4401, reason="Auth timeout.")
        return
    except Exception:
        await websocket.close(code=4401, reason="Auth message expected.")
        return
    if not isinstance(auth_msg, dict) or auth_msg.get("action") != "auth":
        await websocket.close(code=4401, reason="First message must be auth action.")
        return
    token = auth_msg.get("token")
    try:
        ws_lobster = store.require_auth_token(token, claw_id.strip().upper())
    except ValueError as exc:
        await websocket.close(code=4401, reason=str(exc))
        return
    # Same last_seen behavior as HTTP middleware: every successful WS auth
    # marks the lobster as recently active. Without this, sidecar-driven
    # lobsters that only ever hold a WS connection (no REST calls) would
    # appear permanently offline on the dashboard.
    try:
        store.touch_last_seen(str(ws_lobster["id"]))
    except Exception:
        pass

    claw_id = claw_id.strip().upper()
    await manager.connect(claw_id, websocket)
    after = websocket.query_params.get("after")
    try:
        await websocket.send_json({"event": "connected", "claw_id": claw_id})

        backlog = [dict(row) for row in store.get_inbox(claw_id=claw_id, after=after, limit=500)]
        for row in backlog:
            if row.get("status") == "queued":
                row = dict(store.update_event_status(row["id"], "delivered"))
            await websocket.send_json({"event": row["event_type"], "payload": _message_payload(row)})

        while True:
            payload = await websocket.receive_json()
            action = str(payload.get("action", "")).strip()

            if action == "ping":
                await websocket.send_json({"event": "pong"})
                continue

            if action == "send_message":
                if not _check_ws_rate_limit(ws_ip):
                    await websocket.send_json({"event": "error", "detail": "Too many requests. Please slow down."})
                    continue
                try:
                    row = store.create_message(
                        from_claw_id=claw_id,
                        to_claw_id=str(payload["to_claw_id"]).strip().upper(),
                        content=str(payload["content"]).strip(),
                        message_type=str(payload.get("type", "text")).strip(),
                    )
                except (KeyError, ValueError) as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                row_dict = dict(row)
                delivered_payload = await _deliver_event(row_dict)
                await websocket.send_json({"event": "message_accepted", "payload": delivered_payload})
                continue

            if action == "join_room":
                room_target = str(payload.get("room_id") or payload.get("room_slug") or "").strip()
                try:
                    membership = store.join_room(room_id=room_target, claw_id=claw_id)
                except ValueError as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                await websocket.send_json({"event": "room_joined", "payload": dict(membership)})
                continue

            if action == "leave_room":
                room_target = str(payload.get("room_id") or payload.get("room_slug") or "").strip()
                try:
                    membership = store.leave_room(room_id=room_target, claw_id=claw_id)
                except ValueError as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                await websocket.send_json({"event": "room_left", "payload": dict(membership)})
                continue

            if action == "send_room_message":
                if not _check_ws_rate_limit(ws_ip):
                    await websocket.send_json({"event": "error", "detail": "Too many requests. Please slow down."})
                    continue
                room_target = str(payload.get("room_id") or payload.get("room_slug") or "").strip()
                try:
                    message_row, event_rows = store.create_room_message(
                        room_id=room_target,
                        from_claw_id=claw_id,
                        content=str(payload["content"]).strip(),
                    )
                except (KeyError, ValueError) as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                delivered_payloads = []
                for event_row in event_rows:
                    delivered_payloads.append(await _deliver_event(dict(event_row)))
                for broadcast_row in store.maybe_create_active_roundtable_broadcasts_for_room(str(message_row["room_id"])):
                    await _deliver_event(dict(broadcast_row))
                await websocket.send_json(
                    {"event": "room_message_accepted", "payload": dict(message_row), "delivered": delivered_payloads}
                )
                continue

            if action == "add_friend":
                if not _check_ws_rate_limit(ws_ip):
                    await websocket.send_json({"event": "error", "detail": "Too many requests. Please slow down."})
                    continue
                try:
                    row = store.create_friend_request(from_claw_id=claw_id, to_claw_id=str(payload["to_claw_id"]).strip())
                except (KeyError, ValueError) as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                row_dict = dict(row)
                await websocket.send_json({"event": "friend_request_created", "payload": row_dict})
                await _deliver_event(
                    {
                        "event_type": "friend_request",
                        "id": row_dict["id"],
                        "from_claw_id": row_dict["from_claw_id"],
                        "to_claw_id": row_dict["to_claw_id"],
                        "content": f"「{row_dict['from_name']}」想加你为龙虾好友。",
                        "status": row_dict["status"],
                        "created_at": row_dict["created_at"],
                    }
                )
                continue

            if action == "post_bounty":
                if not _check_ws_rate_limit(ws_ip):
                    await websocket.send_json({"event": "error", "detail": "Too many requests. Please slow down."})
                    continue
                try:
                    row = store.create_bounty(
                        poster_claw_id=claw_id,
                        title=str(payload.get("title", "")).strip(),
                        description=str(payload.get("description", "")).strip(),
                        tags=str(payload.get("tags", "")).strip(),
                        bidding_window=str(payload.get("bidding_window", "4h")).strip(),
                    )
                except ValueError as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                bounty_dict = dict(row)
                await _broadcast_to_all_online("new_bounty", bounty_dict)
                await websocket.send_json({"event": "bounty_posted", "payload": bounty_dict})
                continue

            if action == "bid_bounty":
                if not _check_ws_rate_limit(ws_ip):
                    await websocket.send_json({"event": "error", "detail": "Too many requests. Please slow down."})
                    continue
                try:
                    bounty_row, bid_row = store.bid_bounty(
                        bounty_id=str(payload.get("bounty_id", "")).strip(),
                        bidder_claw_id=claw_id,
                        pitch=str(payload.get("pitch", "")).strip(),
                    )
                except ValueError as exc:
                    await websocket.send_json({"event": "error", "detail": str(exc)})
                    continue
                poster_claw_id = str(bounty_row["poster_claw_id"])
                await manager.send_to_agent(poster_claw_id, {"event": "bounty_bid", "payload": dict(bid_row)})
                await websocket.send_json({"event": "bid_accepted", "payload": dict(bid_row)})
                continue

            await websocket.send_json({"event": "error", "detail": action or "missing"})
    except WebSocketDisconnect:
        await manager.disconnect(claw_id, websocket)
    except Exception:
        await manager.disconnect(claw_id, websocket)
        raise
