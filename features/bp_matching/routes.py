"""API routes for BP matching (founder-investor matchmaking)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from server.realtime import manager

from .models import (
    BPListingCreateRequest,
    BPListingRow,
    BPListingSummaryRow,
    BPListingUpdateRequest,
    BPIntentCreateRequest,
    BPIntentRow,
    BPIntentReviewRequest,
    InviteCodeCreateRequest,
    InviteCodeRow,
    InviteCodeRedeemRequest,
    InviteCodeRedeemResponse,
    RoleApplicationCreateRequest,
    RoleApplicationRow,
    RoleApplicationReviewRequest,
    OwnerContactSetRequest,
    OwnerContactRow,
    InvestorProfileSetRequest,
    InvestorProfileRow,
    MeetingRequestResponse,
    MeetingUnlockedPayload,
)
from . import store as bp_store
from features.platform.routes import _require_platform_token

router = APIRouter(prefix="/bp", tags=["bp-matching"])


# ---------------------------------------------------------------------------
# Helpers (injected by main app)
# ---------------------------------------------------------------------------

_check_rate_limit = None
_require_http_auth = None
_require_signature_if_keyed = None


def init_helpers(check_rate_limit, require_http_auth, require_signature_if_keyed):
    """Called by main app to inject shared helpers."""
    global _check_rate_limit, _require_http_auth, _require_signature_if_keyed
    _check_rate_limit = check_rate_limit
    _require_http_auth = require_http_auth
    _require_signature_if_keyed = require_signature_if_keyed


def _http_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# BP Listings
# ---------------------------------------------------------------------------

@router.post("/listings", response_model=BPListingRow)
async def create_listing(payload: BPListingCreateRequest, request: Request, claw_id: str) -> BPListingRow:
    """Create a new BP listing. Requires: founder role + phone verified."""
    _check_rate_limit(request)
    lobster = _require_http_auth(request, claw_id.strip().upper())
    await _require_signature_if_keyed(request, lobster)
    try:
        result = bp_store.create_listing(
            claw_id=claw_id.strip().upper(),
            project_name=payload.project_name,
            one_liner=payload.one_liner,
            sector=payload.sector,
            stage=payload.stage,
            funding_ask=payload.funding_ask,
            currency=payload.currency,
            team_size=payload.team_size,
            access_policy=payload.access_policy,
            expires_in_days=payload.expires_in_days or 90,
            problem=payload.problem,
            solution=payload.solution,
            team_intro=payload.team_intro,
            traction=payload.traction,
            business_model=payload.business_model,
            ask_note=payload.ask_note,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    return BPListingRow(**result)


@router.get("/listings", response_model=list[BPListingSummaryRow])
def search_listings(
    request: Request,
    sector: str | None = None,
    stage: str | None = None,
    limit: int = 50,
    before_created_at: str | None = None,
) -> list[BPListingSummaryRow]:
    """Browse public BP summaries. Returns project_name / one_liner / sector
    / stage / funding_ask only — the structured pitch fields (problem,
    solution, team_intro, traction, business_model, ask_note) are gated
    behind founder approval and only available via GET /listings/{id} when
    the caller has an accepted intent (or is the founder).

    Pagination: pass the `created_at` of the last row from the previous
    page as `before_created_at` to fetch older listings. Omit on first call.
    """
    _check_rate_limit(request)
    try:
        results = bp_store.search_listings(
            sector=sector,
            stage=stage,
            limit=limit,
            before_created_at=before_created_at,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    # BPListingSummaryRow ignores any extra keys in `r` (Pydantic default).
    return [BPListingSummaryRow(**r) for r in results]


@router.get("/listings/{listing_id}", response_model=BPListingRow)
async def get_listing(listing_id: str, request: Request, claw_id: str) -> BPListingRow:
    """Get a single BP listing's full detail.

    Authz: caller (`claw_id`) must be the listing's founder, OR have an
    accepted/auto_accepted intent on this listing, OR the listing must be
    access_policy='open'. Otherwise 403 — explicit so the agent can tell
    the user "you need to express interest first" instead of seeing
    blanked-out fields and guessing the BP author wrote nothing.

    Browse the public summary fields via GET /bp/listings (the search
    endpoint).
    """
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    _require_http_auth(request, normalized)
    try:
        result = bp_store.get_listing(listing_id)
    except ValueError as exc:
        raise _http_error(exc) from exc

    if result.get("access_policy") == "open":
        return BPListingRow(**result)
    if normalized == str(result.get("founder_claw_id") or "").upper():
        return BPListingRow(**result)

    from server.store import get_conn
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT bi.status FROM bp_intents bi
            JOIN lobsters inv ON inv.id = bi.investor_lobster_id
            WHERE bi.listing_id = ? AND inv.claw_id = ?
            LIMIT 1
            """,
            (str(result["id"]), normalized),
        ).fetchone()
    if row is not None and row["status"] in ("accepted", "auto_accepted"):
        return BPListingRow(**result)

    raise HTTPException(
        status_code=403,
        detail="想看这份 BP 的完整内容，需要先发意向并被创始人接受。或者发布者把它设为公开（access_policy=open）。",
    )


@router.patch("/listings/{listing_id}", response_model=BPListingRow)
async def update_listing(listing_id: str, payload: BPListingUpdateRequest, request: Request, claw_id: str) -> BPListingRow:
    """Update a BP listing (owner only). Can change access_policy or close."""
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized)
    await _require_signature_if_keyed(request, lobster)
    content_updates = payload.model_dump(
        exclude_none=True,
        exclude={"access_policy", "status"},
    )
    try:
        result = bp_store.update_listing(
            listing_id=listing_id,
            claw_id=normalized,
            access_policy=payload.access_policy,
            status=payload.status,
            content_updates=content_updates,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    # If a key content field changed, nudge investors with still-pending intents.
    notify = result.pop("_notify_investors", [])
    for investor_claw in notify:
        await manager.send_to_agent(investor_claw, {
            "event": "bp_listing_updated",
            "payload": {
                "listing_id": listing_id,
                "project_name": result.get("project_name"),
                "one_liner": result.get("one_liner"),
                "funding_ask": result.get("funding_ask"),
                "stage": result.get("stage"),
            },
        })

    # If closing the listing cancelled active A2A sessions, push a:stalled
    # to both sides of each session so their sidecars surface the end.
    cancelled = result.pop("_cancelled_a2a_sessions", [])
    if cancelled:
        from . import a2a_dispatch
        for sid in cancelled:
            await a2a_dispatch.dispatch(
                {"kind": "stalled", "session_id": sid, "reason": "BP 已被发布者关闭，撮合会话终止。"},
                sid,
            )

    return BPListingRow(**result)


@router.post("/a2a/sessions/{session_id}/vote")
async def a2a_vote_route(session_id: str, payload: dict, request: Request, claw_id: str) -> dict:
    """A plugin sidecar reports its 'should we end this conversation?' vote.

    Body: {"want_end": bool, "reason"?: str}. Auth: lobster's auth_token
    matching `claw_id` query param.

    Returns the resulting state-machine decision so the caller knows what
    happened (waiting for other vote / resumed / concluded).
    """
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    _require_http_auth(request, normalized)
    want_end = bool(payload.get("want_end"))
    try:
        from . import a2a_engine, a2a_dispatch
        result = a2a_engine.record_vote(session_id, normalized, want_end)
    except ValueError as exc:
        raise _http_error(exc) from exc
    # Engine returns None when this caller raced another caller to a
    # state transition (e.g. both sides voted want_end simultaneously and
    # the other call already entered conclude). Nothing to dispatch; the
    # winning caller's path handled the WS pushes.
    if result is None:
        return {"kind": "noop", "note": "vote recorded; another caller already advanced state"}
    await a2a_dispatch.dispatch(result, session_id)
    return result


@router.get("/my-status")
def my_bp_status_route(request: Request, claw_id: str) -> dict:
    """Return the caller's BP-matching status (role, verification, phone).

    Exists so the plugin/agent can ground answers like "am I a verified
    investor?" in real DB state instead of LLM hallucination.
    """
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    _require_http_auth(request, normalized)
    try:
        return bp_store.get_my_bp_status(normalized)
    except ValueError as exc:
        raise _http_error(exc) from exc


@router.get("/my-intents", response_model=list[BPIntentRow])
def my_intents_route(request: Request, claw_id: str) -> list[BPIntentRow]:
    """Investor pipeline view: all intents I've created, newest first.

    For pending intents, queue_position / queue_total reflect where I
    stand in each BP's review queue right now.
    """
    _check_rate_limit(request)
    _require_http_auth(request, claw_id.strip().upper())
    try:
        results = bp_store.list_my_intents(claw_id.strip().upper())
    except ValueError as exc:
        raise _http_error(exc) from exc
    return [BPIntentRow(**r) for r in results]


@router.get("/my-listings", response_model=list[BPListingRow])
def my_listings(request: Request, claw_id: str) -> list[BPListingRow]:
    """Get all my BP listings."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id.strip().upper())
    try:
        results = bp_store.get_my_listings(claw_id.strip().upper())
    except ValueError as exc:
        raise _http_error(exc) from exc
    return [BPListingRow(**r) for r in results]


# ---------------------------------------------------------------------------
# BP Intents
# ---------------------------------------------------------------------------

@router.post("/listings/{listing_id}/intents", response_model=BPIntentRow)
async def create_intent(listing_id: str, payload: BPIntentCreateRequest, request: Request, claw_id: str) -> BPIntentRow:
    """Investor expresses interest in a BP. Requires: verified investor role."""
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized)
    await _require_signature_if_keyed(request, lobster)
    try:
        result = bp_store.create_intent(
            listing_id=listing_id,
            investor_claw_id=normalized,
            personal_note=payload.personal_note,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    # Notify founder via WebSocket
    listing = bp_store.get_listing(listing_id)
    await manager.send_to_agent(listing["founder_claw_id"], {
        "event": "bp_intent",
        "payload": result,
    })

    # Open BPs auto-accept and need to start the A2A dialogue here (the
    # manual-review path does this in review_intent). start_session is
    # idempotent — safe even though store.create_intent used to also call
    # it; that legacy duplicate has been removed.
    if result.get("status") == "auto_accepted":
        from . import a2a_engine, a2a_dispatch
        started = a2a_engine.start_session(result["id"])
        if started is not None:
            sid = started["session"]["id"]
            await a2a_dispatch.dispatch(started["next_action"], sid)
            await a2a_dispatch.push_contact_missing_nudge(sid)

    return BPIntentRow(**result)


@router.get("/listings/{listing_id}/intents", response_model=list[BPIntentRow])
def list_intents(listing_id: str, request: Request, claw_id: str) -> list[BPIntentRow]:
    """List intents for a listing (founder only)."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id.strip().upper())
    try:
        results = bp_store.list_intents(listing_id, claw_id.strip().upper())
    except ValueError as exc:
        raise _http_error(exc) from exc
    return [BPIntentRow(**r) for r in results]


@router.post("/intents/{intent_id}/review", response_model=BPIntentRow)
async def review_intent(intent_id: str, payload: BPIntentReviewRequest, request: Request, claw_id: str) -> BPIntentRow:
    """Founder reviews an intent (accept / reject). Requires signature."""
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized)
    await _require_signature_if_keyed(request, lobster)
    try:
        result = bp_store.review_intent(
            intent_id=intent_id,
            claw_id=normalized,
            decision=payload.decision,
            review_note=payload.note,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    # Notify investor via WebSocket — payload now carries review_note so the
    # investor sees the founder's reasoning (especially important on reject).
    await manager.send_to_agent(result["investor_claw_id"], {
        "event": "bp_intent_reviewed",
        "payload": result,
    })

    # On accept, start the A2A dialogue right here. start_session returns
    # None when the pair isn't both-auto (or session already exists from a
    # prior call); dispatch only fires when there's something to send.
    if payload.decision == "accepted":
        from . import a2a_engine, a2a_dispatch
        started = a2a_engine.start_session(intent_id)
        if started is not None:
            sid = started["session"]["id"]
            await a2a_dispatch.dispatch(started["next_action"], sid)
            await a2a_dispatch.push_contact_missing_nudge(sid)

    # The queue shifted: nudge every still-pending investor on this same
    # listing with their new position so the wait isn't opaque.
    listing_id_for_queue = result["listing_id"]
    try:
        remaining = bp_store.list_pending_intents_for_listing(listing_id_for_queue)
    except Exception:
        remaining = []
    for it in remaining:
        await manager.send_to_agent(it["investor_claw_id"], {
            "event": "bp_queue_update",
            "payload": {
                "intent_id": it["id"],
                "listing_id": listing_id_for_queue,
                "project_name": it["project_name"],
                "queue_position": it["queue_position"],
                "queue_total": it["queue_total"],
            },
        })

    # Conversation kickoff hint: only on accept. Sandpile pushes one
    # `bp_chat_ready` event to each side carrying a role-specific suggestion
    # for who should speak first. Avoids the "both sides wait for the
    # other" deadlock where a freshly accepted intent never advances.
    if result["status"] == "accepted":
        listing = bp_store.get_listing(result["listing_id"])
        founder_claw = listing["founder_claw_id"]
        investor_claw = result["investor_claw_id"]
        await manager.send_to_agent(founder_claw, {
            "event": "bp_chat_ready",
            "payload": {
                "intent_id": intent_id,
                "listing_id": result["listing_id"],
                "your_role": "founder",
                "peer_claw_id": investor_claw,
                "hint": "你刚通过此投资人的兴趣。建议先发一段简短开场白：欢迎、点出 BP 重点、邀请他提问。",
            },
        })
        await manager.send_to_agent(investor_claw, {
            "event": "bp_chat_ready",
            "payload": {
                "intent_id": intent_id,
                "listing_id": result["listing_id"],
                "your_role": "investor",
                "peer_claw_id": founder_claw,
                "hint": "founder 已通过你的兴趣。如果对方未在 30 秒内开场，建议你主动发一个具体的尽调问题（traction、团队、商业模式等任选一项）。",
            },
        })

    return BPIntentRow(**result)


# ---------------------------------------------------------------------------
# Invite codes (admin creates, lobster redeems)
# ---------------------------------------------------------------------------

@router.post("/invite-codes", response_model=InviteCodeRow)
def create_invite_code_route(payload: InviteCodeCreateRequest, request: Request) -> InviteCodeRow:
    """Admin endpoint — generate a new invite code.

    Auth: platform token (admin/trusted frontend only).
    """
    _check_rate_limit(request)
    token_row = _require_platform_token(request)
    try:
        result = bp_store.create_invite_code(
            role=payload.role,
            role_verified=payload.role_verified,
            generated_by=str(token_row.get("name") or "admin"),
            note=payload.note,
            valid_days=payload.valid_days,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    # Add missing fields for response
    result["used_at"] = None
    result["used_by_lobster_id"] = None
    return InviteCodeRow(**result)


@router.get("/invite-codes", response_model=list[InviteCodeRow])
def list_invite_codes_route(request: Request, status: str | None = None, limit: int = 100) -> list[InviteCodeRow]:
    """Admin endpoint — list invite codes. status: unused|used|expired|None(all)."""
    _check_rate_limit(request)
    _require_platform_token(request)
    rows = bp_store.list_invite_codes(status=status, limit=limit)
    # Normalize role_verified from int to bool
    for r in rows:
        r["role_verified"] = bool(r.get("role_verified"))
    return [InviteCodeRow(**r) for r in rows]


@router.post("/invite-codes/redeem", response_model=InviteCodeRedeemResponse)
async def redeem_invite_code_route(
    payload: InviteCodeRedeemRequest,
    request: Request,
    claw_id: str,
) -> InviteCodeRedeemResponse:
    """Lobster redeems an invite code to claim a role. Requires lobster auth."""
    _check_rate_limit(request)
    lobster = _require_http_auth(request, claw_id.strip().upper())
    await _require_signature_if_keyed(request, lobster)
    try:
        result = bp_store.redeem_invite_code(payload.code, str(lobster["id"]))
    except bp_store.InviteCodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return InviteCodeRedeemResponse(**result)


# ---------------------------------------------------------------------------
# Role applications (lobster submits, admin reviews)
# ---------------------------------------------------------------------------

@router.post("/role-applications", response_model=RoleApplicationRow)
async def submit_role_application_route(
    payload: RoleApplicationCreateRequest,
    request: Request,
    claw_id: str,
) -> RoleApplicationRow:
    """Lobster submits a role application.

    - founder: auto-approved (light auth)
    - investor: queued for admin review
    """
    _check_rate_limit(request)
    lobster = _require_http_auth(request, claw_id.strip().upper())
    await _require_signature_if_keyed(request, lobster)
    try:
        result = bp_store.submit_role_application(
            lobster_id=str(lobster["id"]),
            requested_role=payload.requested_role,
            intro_text=payload.intro_text,
            org_name=payload.org_name,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    # Fetch full row for response
    full = bp_store.get_role_application(result["id"])
    if full:
        full.setdefault("claw_id", str(lobster["claw_id"]))
        full.setdefault("lobster_name", str(lobster["name"]) if "name" in lobster.keys() else "")
    return RoleApplicationRow(**full)


@router.get("/role-applications", response_model=list[RoleApplicationRow])
def list_pending_applications_route(request: Request, limit: int = 50) -> list[RoleApplicationRow]:
    """Admin — list pending role applications."""
    _check_rate_limit(request)
    _require_platform_token(request)
    rows = bp_store.list_pending_applications(limit=limit)
    return [RoleApplicationRow(**r) for r in rows]


@router.post("/role-applications/{app_id}/review", response_model=RoleApplicationRow)
def review_role_application_route(
    app_id: str,
    payload: RoleApplicationReviewRequest,
    request: Request,
) -> RoleApplicationRow:
    """Admin reviews a pending role application."""
    _check_rate_limit(request)
    token_row = _require_platform_token(request)
    try:
        bp_store.review_role_application(
            app_id=app_id,
            decision=payload.decision,
            reviewer=str(token_row.get("name") or "admin"),
            review_note=payload.review_note,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    full = bp_store.get_role_application(app_id)
    if full is None:
        raise HTTPException(status_code=404, detail="Application not found after review.")
    return RoleApplicationRow(**full)


# ---------------------------------------------------------------------------
# Owner contact info (for State 4 unlock)
# ---------------------------------------------------------------------------

@router.put("/owners/{owner_id}/contact")
def set_owner_contact_route(
    owner_id: str,
    payload: OwnerContactSetRequest,
    request: Request,
) -> dict:
    """Set / update an owner's contact info (admin only for now)."""
    _check_rate_limit(request)
    _require_platform_token(request)
    try:
        bp_store.set_owner_contact(
            owner_id=owner_id,
            primary_contact=payload.primary_contact,
            primary_contact_type=payload.primary_contact_type,
            secondary_contacts=payload.secondary_contacts,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    return {"ok": True}


@router.get("/owners/{owner_id}/contact", response_model=OwnerContactRow)
def get_owner_contact_route(owner_id: str, request: Request) -> OwnerContactRow:
    """Read an owner's contact info. Admin-only."""
    _check_rate_limit(request)
    _require_platform_token(request)
    result = bp_store.get_owner_contact(owner_id)
    return OwnerContactRow(**result)


@router.put("/my-contact")
async def set_my_contact_route(
    payload: OwnerContactSetRequest,
    request: Request,
    claw_id: str,
) -> dict:
    """Set / update the caller's own contact info.

    Looks up the caller's owner_id from their lobster record, then writes
    the contact into that owner row. This is the user-facing counterpart
    to the admin-only PUT /bp/owners/{owner_id}/contact.
    """
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized)
    await _require_signature_if_keyed(request, lobster)
    owner_id = str(lobster["owner_id"] or "")
    if not owner_id:
        raise HTTPException(
            status_code=400,
            detail="请先完成手机验证后再设置联系方式。",
        )
    try:
        bp_store.set_owner_contact(
            owner_id=owner_id,
            primary_contact=payload.primary_contact,
            primary_contact_type=payload.primary_contact_type,
            secondary_contacts=payload.secondary_contacts,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    return {"ok": True}


@router.get("/my-contact", response_model=OwnerContactRow)
async def get_my_contact_route(
    request: Request,
    claw_id: str,
) -> OwnerContactRow:
    """Read the caller's own contact info."""
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized)
    await _require_signature_if_keyed(request, lobster)
    owner_id = str(lobster["owner_id"] or "")
    if not owner_id:
        return OwnerContactRow(primary_contact=None, primary_contact_type=None, secondary_contacts={})
    result = bp_store.get_owner_contact(owner_id)
    return OwnerContactRow(**result)


# ---------------------------------------------------------------------------
# Investor preference card
# ---------------------------------------------------------------------------

@router.put("/my-investor-profile", response_model=InvestorProfileRow)
async def set_my_investor_profile_route(
    payload: InvestorProfileSetRequest,
    request: Request,
    claw_id: str,
) -> InvestorProfileRow:
    """Drip-fill the caller's investor profile. Only fields explicitly
    passed get updated, so the guided Q&A can submit one field at a time."""
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized)
    await _require_signature_if_keyed(request, lobster)
    if (dict(lobster).get("role") or "") not in ("investor", "both"):
        raise HTTPException(status_code=403, detail="只有投资人角色才能填写投资偏好卡。")
    fields = payload.model_dump(exclude_unset=True)
    try:
        result = bp_store.set_investor_profile(normalized, **fields)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return InvestorProfileRow(**result)


@router.get("/my-investor-profile", response_model=InvestorProfileRow)
async def get_my_investor_profile_route(
    request: Request,
    claw_id: str,
) -> InvestorProfileRow:
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized)
    await _require_signature_if_keyed(request, lobster)
    result = bp_store.get_investor_profile(normalized)
    return InvestorProfileRow(**result)


@router.get("/investor-profile/{claw_id}", response_model=InvestorProfileRow)
async def get_investor_profile_for_founder_route(
    claw_id: str,
    request: Request,
    viewer_claw_id: str,
) -> InvestorProfileRow:
    """Founder-side lookup: when a founder receives an intent, their agent
    can fetch the investor's preference card to show in the IM card.

    Authz: viewer must be a founder who has at least one BP listing on
    which the target investor has expressed an intent. Without this gate,
    any verified lobster could mass-scrape every investor's thesis (org,
    sectors, ticket range, decision cycle) — that's a privacy leak we
    promised investors wouldn't happen ('preferences only shown to founders
    you're already engaging with').
    """
    _check_rate_limit(request)
    viewer_normalized = viewer_claw_id.strip().upper()
    target_normalized = claw_id.strip().upper()
    lobster = _require_http_auth(request, viewer_normalized)
    await _require_signature_if_keyed(request, lobster)

    # Verify the viewer founder has a listing the target investor has
    # acted on. EXISTS check is cheap; one row in either direction suffices.
    from server.store import get_conn
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM bp_intents bi
            JOIN bp_listings bl ON bl.id = bi.listing_id
            JOIN lobsters fnd ON fnd.id = bl.founder_lobster_id
            JOIN lobsters inv ON inv.id = bi.investor_lobster_id
            WHERE fnd.claw_id = ? AND inv.claw_id = ?
            LIMIT 1
            """,
            (viewer_normalized, target_normalized),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=403,
            detail="只有该投资人在你 BP 上表达过意向后，才能查看其偏好卡。",
        )

    result = bp_store.get_investor_profile(target_normalized)
    return InvestorProfileRow(**result)


# ---------------------------------------------------------------------------
# State 4: Meeting request + contact unlock
# ---------------------------------------------------------------------------

@router.post("/intents/{intent_id}/request-meeting", response_model=MeetingRequestResponse)
async def request_meeting_route(
    intent_id: str,
    request: Request,
    claw_id: str,
) -> MeetingRequestResponse:
    """Either side (investor or founder) signals they want to meet.

    When both sides have signaled, the intent is 'unlocked' and sandpile
    pushes contact info to each side via WebSocket.
    """
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized)
    await _require_signature_if_keyed(request, lobster)

    # Determine which side this lobster is on
    from server.store import get_conn as _gc
    with _gc() as conn:
        row = conn.execute(
            """SELECT bi.investor_lobster_id, bl.founder_lobster_id,
                      bi.listing_id
               FROM bp_intents bi
               JOIN bp_listings bl ON bl.id = bi.listing_id
               WHERE bi.id = ?""",
            (intent_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Intent not found.")

    lobster_id = str(lobster["id"])
    if lobster_id == str(row["investor_lobster_id"]):
        side = "investor"
        peer_claw_id_side = "founder"
    elif lobster_id == str(row["founder_lobster_id"]):
        side = "founder"
        peer_claw_id_side = "investor"
    else:
        raise HTTPException(status_code=403, detail="你不是该 intent 的参与方。")

    # Front-load: caller must have their own contact info ready before they
    # can signal "want to meet". Otherwise the unlock would be one-sided —
    # they'd receive the peer's contact while delivering nothing in return.
    caller_owner_id = str(lobster["owner_id"] or "")
    if not caller_owner_id:
        raise HTTPException(status_code=400, detail="请先完成手机验证并补全联系方式后再发起约见。")
    caller_contact = bp_store.get_owner_contact(caller_owner_id)
    if not caller_contact.get("primary_contact"):
        raise HTTPException(
            status_code=400,
            detail="请先补全你的联系方式（微信或电话）后再发起约见——对方解锁到的将是你的这份联系方式。",
        )

    try:
        result = bp_store.request_meeting(intent_id, side)
    except ValueError as exc:
        raise _http_error(exc) from exc

    # Notify the peer about a fresh "wants to meet" signal — but only the
    # first time this side marks it, so the peer isn't pinged repeatedly
    # when the caller's agent re-issues the same request.
    if result["side_newly_set"]:
        with _gc() as conn:
            peer = conn.execute(
                """SELECT l.claw_id FROM lobsters l
                   WHERE l.id = ?""",
                (str(row["investor_lobster_id"]) if side == "founder" else str(row["founder_lobster_id"]),),
            ).fetchone()
        if peer:
            await manager.send_to_agent(str(peer["claw_id"]), {
                "event": "bp_meeting_interest",
                "payload": {
                    "intent_id": intent_id,
                    "from_side": side,
                    "unlocked": result["unlocked"],
                },
            })

    # Push unlock events (with contact info) only on the transition into
    # unlocked — never re-broadcast for subsequent calls on an already-unlocked intent.
    if result["first_unlock"]:
        # Fetch each side's claw_id
        with _gc() as conn:
            inv_claw = conn.execute(
                "SELECT claw_id FROM lobsters WHERE id = ?",
                (str(row["investor_lobster_id"]),),
            ).fetchone()
            fnd_claw = conn.execute(
                "SELECT claw_id FROM lobsters WHERE id = ?",
                (str(row["founder_lobster_id"]),),
            ).fetchone()

        # Investor receives founder's contact
        try:
            inv_payload = bp_store.get_meeting_unlock_payload(intent_id, "investor")
            if inv_claw:
                await manager.send_to_agent(str(inv_claw["claw_id"]), {
                    "event": "bp_meeting_unlocked",
                    "payload": inv_payload,
                })
        except ValueError:
            pass

        # Founder receives investor's contact
        try:
            fnd_payload = bp_store.get_meeting_unlock_payload(intent_id, "founder")
            if fnd_claw:
                await manager.send_to_agent(str(fnd_claw["claw_id"]), {
                    "event": "bp_meeting_unlocked",
                    "payload": fnd_payload,
                })
        except ValueError:
            pass

    return MeetingRequestResponse(**result)


@router.post("/admin/expire-stale-meetings")
def expire_stale_meetings_route(request: Request) -> dict:
    """Admin helper: mark overdue meeting requests as expired."""
    _check_rate_limit(request)
    _require_platform_token(request)
    count = bp_store.expire_stale_meeting_requests()
    return {"expired": count}
