"""API routes for economy layer."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .models import AccountInfoResponse, InvocationRow, JoinRequestReviewRequest, JoinRequestRow, OwnerLobsterRow
from . import store as economy_store

router = APIRouter(prefix="/accounts", tags=["economy"])

_check_rate_limit = None
_require_http_auth = None


def init_helpers(check_rate_limit, require_http_auth):
    global _check_rate_limit, _require_http_auth
    _check_rate_limit = check_rate_limit
    _require_http_auth = require_http_auth


@router.get("/{claw_id}", response_model=AccountInfoResponse)
def get_account(claw_id: str, request: Request) -> AccountInfoResponse:
    """Get account info for a lobster (their owner's balance)."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id)
    owner = economy_store.get_owner_by_lobster_claw_id(claw_id)
    if owner is None:
        return AccountInfoResponse(
            claw_id=claw_id,
            owner_id=None,
            credit_balance=0,
            has_account=False,
        )
    balance = economy_store.get_balance(str(owner["id"]))
    return AccountInfoResponse(
        claw_id=claw_id,
        owner_id=str(owner["id"]),
        credit_balance=balance,
        has_account=True,
    )


@router.get("/{claw_id}/invocations", response_model=list[InvocationRow])
def list_invocations(claw_id: str, request: Request, limit: int = 50) -> list[InvocationRow]:
    """List invocation history for a lobster's owner."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id)
    owner = economy_store.get_owner_by_lobster_claw_id(claw_id)
    if owner is None:
        return []
    rows = economy_store.list_invocations_for_owner(str(owner["id"]), limit=limit)
    return [InvocationRow(**r) for r in rows]


@router.get("/{claw_id}/lobsters", response_model=list[OwnerLobsterRow])
def list_owner_lobsters(claw_id: str, request: Request) -> list[OwnerLobsterRow]:
    """List all lobsters belonging to the same owner as this lobster."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id)
    owner = economy_store.get_owner_by_lobster_claw_id(claw_id)
    if owner is None:
        return []
    rows = economy_store.list_lobsters_for_owner(str(owner["id"]))
    return [OwnerLobsterRow(**r) for r in rows]


# Join request review endpoints (placed at module level, prefix /accounts is shared)

@router.post("/join-requests/{request_id}/review")
def review_join_request(request_id: str, payload: JoinRequestReviewRequest, request: Request, claw_id: str) -> dict:
    """Review a pending owner-join request. Caller must be a lobster of the target owner."""
    _check_rate_limit(request)
    lobster = _require_http_auth(request, claw_id)
    try:
        result = economy_store.review_join_request(
            request_id=request_id,
            reviewer_lobster_id=str(lobster["id"]),
            decision=payload.decision,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.get("/{claw_id}/join-requests", response_model=list[JoinRequestRow])
def list_pending_join_requests(claw_id: str, request: Request) -> list[JoinRequestRow]:
    """List pending join requests targeting this lobster's owner."""
    _check_rate_limit(request)
    _require_http_auth(request, claw_id)
    owner = economy_store.get_owner_by_lobster_claw_id(claw_id)
    if owner is None:
        return []
    rows = economy_store.list_pending_join_requests_for_owner(str(owner["id"]))
    return [JoinRequestRow(**r) for r in rows]
