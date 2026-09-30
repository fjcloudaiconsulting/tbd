"""In-app decisions on staged agent actions (TBD-577).

``POST /api/v1/agent/actions/{id}/confirm`` and ``/cancel``, and
``GET /api/v1/agent/actions`` (the review list). Interactive sessions only: a
PAT can never confirm, and there is no model-facing confirm tool. Confirm takes
NO request body: the action runs with the arguments stored at preview time.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import registry
from app.agent.registry import ToolError, wrap_untrusted
from app.auth.feature_deps import require_feature
from app.auth.pat import require_interactive_session
from app.database import get_db
from app.models.agent_pending_action import (
    ActionMode, ActionStatus, AgentPendingAction as Action,
)
from app.models.user import User
from app.rate_limit import limiter

router = APIRouter(
    prefix="/api/v1/agent/actions",
    tags=["agent"],
    dependencies=[Depends(require_feature(registry.AGENT_FEATURE_KEY))],
)

_STATUS: dict[str, int] = {
    "action_not_found": 404,
    "action_expired": 410, "tool_retired": 410,
    "action_already_decided": 409, "action_in_progress": 409, "preview_stale": 409,
    "feature_not_entitled": 403, "feature_disabled": 403, "insufficient_role": 403,
    "scope_denied": 403,
    "invalid_arguments": 422, "no_change": 422,
    "payload_too_large": 413,
    "not_found": 404,
    "preview_rate_limited": 429, "too_many_pending_actions": 429,
    "confirm_rate_limited": 429, "auto_budget_exhausted": 429,
    "sensitive_budget_exhausted": 429,
    "limits_unavailable": 503,
}


def _http(exc: ToolError) -> HTTPException:
    """``{"detail": {"code", "message", ...data}}``; an unmapped code is a 500."""
    return HTTPException(
        status_code=_STATUS.get(exc.code, 500),
        detail={"code": exc.code, "message": exc.detail, **exc.data},
    )


async def _decide(fn, db: AsyncSession, user: User, action_id: str) -> dict[str, Any]:
    try:
        return (await fn(
            db, user, action_id, channel="in_app", scope=None, api_token_id=None
        ))["data"]
    except ToolError as exc:
        raise _http(exc) from None


@router.post("/{action_id}/confirm")
@limiter.shared_limit("30/minute", scope="agent.confirm")
async def confirm(
    request: Request,
    action_id: str,
    user: User = Depends(require_interactive_session),
    db: AsyncSession = Depends(get_db),
):
    return await _decide(registry.confirm_action, db, user, action_id)


@router.post("/{action_id}/cancel")
@limiter.shared_limit("30/minute", scope="agent.cancel")
async def cancel(
    request: Request,
    action_id: str,
    user: User = Depends(require_interactive_session),
    db: AsyncSession = Depends(get_db),
):
    return await _decide(registry.cancel_action, db, user, action_id)


@router.get("")
async def list_actions(
    user: User = Depends(require_interactive_session),
    db: AsyncSession = Depends(get_db),
    mode: Literal["confirm", "auto"] | None = None,
    status: Literal["pending", "executing", "done", "failed", "stale", "cancelled"] | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10_000),
):
    """The caller's own actions (org and user), newest first."""
    stmt = select(Action).where(Action.org_id == user.org_id, Action.user_id == user.id)
    if mode is not None:
        stmt = stmt.where(Action.mode == ActionMode(mode))
    if status is not None:
        stmt = stmt.where(Action.status == ActionStatus(status))
    rows = (await db.scalars(
        stmt.order_by(Action.created_at.desc(), Action.id.desc()).limit(limit).offset(offset)
    )).all()
    return {
        "items": [
            wrap_untrusted({
                "action_id": r.id, "tool": r.tool, "channel": r.channel.value,
                "risk": r.risk.value, "mode": r.mode.value, "status": r.status.value,
                "preview": r.preview_json, "result": r.result_json, "error_code": r.error_code,
                "created_at": r.created_at.isoformat(), "expires_at": r.expires_at.isoformat(),
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            })
            for r in rows
        ],
        "limit": limit, "offset": offset,
    }
