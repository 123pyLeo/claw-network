"""API routes for BP matching (founder-investor matchmaking)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from server.realtime import manager

from .models import (
    BPListingCreateRequest,
    BPListingRow,
    BPListingUpdateRequest,
    BPIntentCreateRequest,
    BPIntentRow,
    BPIntentReviewRequest,
)
from . import store as bp_store

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
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    return BPListingRow(**result)


@router.get("/listings", response_model=list[BPListingRow])
def search_listings(
    request: Request,
    sector: str | None = None,
    stage: str | None = None,
    limit: int = 50,
) -> list[BPListingRow]:
    """Search active BP listings. Public endpoint."""
    _check_rate_limit(request)
    try:
        results = bp_store.search_listings(sector=sector, stage=stage, limit=limit)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return [BPListingRow(**r) for r in results]


@router.get("/listings/{listing_id}", response_model=BPListingRow)
def get_listing(listing_id: str, request: Request) -> BPListingRow:
    """Get a single BP listing. Public endpoint."""
    _check_rate_limit(request)
    try:
        result = bp_store.get_listing(listing_id)
    except ValueError as exc:
        raise _http_error(exc) from exc
    return BPListingRow(**result)


@router.patch("/listings/{listing_id}", response_model=BPListingRow)
async def update_listing(listing_id: str, payload: BPListingUpdateRequest, request: Request, claw_id: str) -> BPListingRow:
    """Update a BP listing (owner only). Can change access_policy or close."""
    _check_rate_limit(request)
    normalized = claw_id.strip().upper()
    lobster = _require_http_auth(request, normalized)
    await _require_signature_if_keyed(request, lobster)
    try:
        result = bp_store.update_listing(
            listing_id=listing_id,
            claw_id=normalized,
            access_policy=payload.access_policy,
            status=payload.status,
        )
    except ValueError as exc:
        raise _http_error(exc) from exc
    return BPListingRow(**result)


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
        )
    except ValueError as exc:
        raise _http_error(exc) from exc

    # Notify investor via WebSocket
    await manager.send_to_agent(result["investor_claw_id"], {
        "event": "bp_intent_reviewed",
        "payload": result,
    })

    return BPIntentRow(**result)
