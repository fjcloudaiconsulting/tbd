"""Preview-confirm engine for agent writes (TBD-577).

A write tool is never executed by the call that names it. ``propose`` runs the
tool's ``preview`` (built from service read paths, writes nothing), stores the
result as a pending action bound to the principal that asked, and hands the
diff back. ``confirm`` is the only path that executes: one conditional UPDATE
claims the row (so exactly one of any number of concurrent confirms proceeds),
the gates are re-run against the CURRENT world, the preview is recomputed from
the STORED args and must fingerprint identically (else the row goes ``stale``
and a fresh preview is returned), and only then does ``execute`` run. A row
that has left ``pending`` is never run again.

``agent:auto`` on a ``write`` tool over MCP stages and then calls the very same
``confirm`` in the same request. There is no second execute path.

Every write bucket fails CLOSED: no store connection, or any store error, is
``limits_unavailable`` (503), never an unmetered write.

Known ceiling: services commit internally, so the re-preview and the execute
are two transactions. The window is milliseconds and the REST routes these
tools mirror are already last-write-wins.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import asdict
from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import rate_limit_db
from app._time import utcnow_naive
from app.agent import registry
from app.agent.registry import (
    AGENT_SCOPES, Preview, ToolContext, ToolError, ToolSpec,
)
from app.models.agent_pending_action import (
    ActionChannel, ActionMode, ActionRisk, ActionStatus, AgentPendingAction as Action,
)
from app.services import audit_service

logger = structlog.stdlib.get_logger()

PENDING_TTL = timedelta(minutes=10)
MAX_ARGS_BYTES = 4 * 1024
MAX_PREVIEW_BYTES = 16 * 1024
MINUTE, HOUR, DAY = 60, 3600, 86_400

# Live (pending, unexpired) rows: per user, per org.
MAX_LIVE_USER, MAX_LIVE_ORG = 10, 50


# ── helpers ───────────────────────────────────────────────────────────────

def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _principal(ctx: ToolContext, user_id: int) -> str:
    return f"tok:{ctx.api_token_id}" if ctx.channel == "mcp" else f"usr:{user_id}"


def _scope_gate(ctx: ToolContext, scope: str | None, *, decide: frozenset[str] = AGENT_SCOPES) -> None:
    """Confirm and cancel need a well-formed principal. Whether the token's
    scope still covers the STORED tool is gate 5, re-run after the claim."""
    if ctx.channel == "in_app":
        ok = scope is None
    elif ctx.channel == "mcp":
        ok = scope in decide and ctx.api_token_id is not None
    else:
        ok = False
    if not ok:
        raise ToolError("scope_denied", "not a valid agent principal")


async def _hit(key: str, limit: int, window: int, code: str) -> None:
    """Fixed-window counter in the limits DB (rate limits move to MySQL,
    INFRA-121). Fails closed."""
    try:
        n = await asyncio.to_thread(rate_limit_db.hit, key, window)
    except SQLAlchemyError:
        raise ToolError("limits_unavailable", "rate limiting is unavailable") from None
    if n > limit:
        raise ToolError(code, "limit reached, try again later")


# Gate 6 (TBD-561): every MCP tools/call, confirm and cancel draws one call
# from its TOKEN's bucket, after args validation and BEFORE the ``mcp.calls``
# admission, so a throttled harness never spends the org's plan meter.
CALLS_PER_MIN, CALLS_PER_DAY = 120, 2000


async def call_gate(channel: str, api_token_id: int | None, risk: str) -> None:
    """Gate 6. A no-op in-app (its bounds are the turn meter and the chat
    limits). Fails CLOSED for every risk when the limits DB is unavailable."""
    if channel != "mcp":
        return
    if api_token_id is None:
        raise ToolError("scope_denied", "agent token required")
    await _hit(f"agent:tok:{api_token_id}:calls:min", CALLS_PER_MIN, MINUTE, "token_rate_limited")
    await _hit(f"agent:tok:{api_token_id}:calls:day", CALLS_PER_DAY, DAY, "token_rate_limited")


def _preview_dict(pv: Preview) -> dict[str, Any]:
    # Round-trip through JSON so what is fingerprinted is what is stored.
    return json.loads(_canonical({
        "summary": pv.summary,
        "changes": [asdict(c) for c in pv.changes],
        "warnings": list(pv.warnings),
        "context": dict(pv.context),
    }))


def _fingerprint(tool: str, args_json: dict[str, Any], preview: dict[str, Any]) -> str:
    changes = sorted(preview["changes"], key=_canonical)
    return _sha256(_canonical({"tool": tool, "args": args_json, "changes": changes}))


def _new_row(
    ctx: ToolContext, user_id: int, spec: ToolSpec, args_json: dict[str, Any],
    preview: dict[str, Any], mode: ActionMode,
) -> Action:
    if len(_canonical(args_json).encode()) > MAX_ARGS_BYTES:
        raise ToolError("payload_too_large", "arguments exceed the size limit")
    if len(_canonical(preview).encode()) > MAX_PREVIEW_BYTES:
        raise ToolError("payload_too_large", "preview exceeds the size limit")
    now = utcnow_naive()
    return Action(
        id=secrets.token_hex(16), org_id=ctx.org_id, user_id=user_id,
        channel=ActionChannel(ctx.channel), api_token_id=ctx.api_token_id, tool=spec.name,
        risk=ActionRisk(spec.risk), mode=mode, args_json=args_json,
        args_sha256=_sha256(_canonical(args_json)),
        fingerprint=_fingerprint(spec.name, args_json, preview), preview_json=preview,
        status=ActionStatus.PENDING, created_at=now, expires_at=now + PENDING_TTL,
    )


def _staged(row: Action) -> dict[str, Any]:
    p = row.preview_json
    return {
        "action_id": row.id, "summary": p["summary"], "changes": p["changes"],
        "warnings": p["warnings"], "context": p["context"],
        "expires_at": row.expires_at.isoformat(), "requires_confirmation": True,
    }


def _mine(ctx: ToolContext, user_id: int, action_id: str) -> list[Any]:
    """The row's owner, NULL-safe on the token (in-app rows carry none)."""
    return [
        Action.id == action_id, Action.org_id == ctx.org_id, Action.user_id == user_id,
        Action.channel == ActionChannel(ctx.channel),
        Action.api_token_id.is_not_distinct_from(ctx.api_token_id),
    ]


async def _check_ceiling(ctx: ToolContext, user_id: int) -> None:
    now = utcnow_naive()
    live = select(func.count()).select_from(Action).where(
        Action.org_id == ctx.org_id, Action.status == ActionStatus.PENDING,
        Action.expires_at > now,
    )
    db = ctx.db
    # No separate per-principal ceiling: a principal's rows are a subset of its
    # user's and both limits are 10, so it could never fire first.
    if await db.scalar(live) >= MAX_LIVE_ORG:
        raise ToolError("too_many_pending_actions", "cancel or confirm pending actions first")
    if await db.scalar(live.where(Action.user_id == user_id)) >= MAX_LIVE_USER:
        raise ToolError("too_many_pending_actions", "cancel or confirm pending actions first")


def inverse_args(spec: ToolSpec, args_json: dict[str, Any], preview_json: dict[str, Any]) -> dict:
    """The args that restore a done write: the stored args with every field of
    the PRIMARY entity (``changes[0]``) put back to its ``before``. Derived
    changes (a learned rule) are not inverted; the revert's own preview
    re-discloses them."""
    changes = preview_json["changes"]
    p = changes[0]
    restore = {
        c["field"]: c["before"] for c in changes
        if (c["entity"], c["id"]) == (p["entity"], p["id"])
    }
    if not restore.keys() <= spec.args.model_fields.keys():
        raise ToolError(
            "not_revertible", "this action cannot be inverted", data={"reason": "no_inverse"}
        )
    return {**args_json, **restore}


# ── propose ───────────────────────────────────────────────────────────────

async def propose(
    ctx: ToolContext, spec: ToolSpec, args: Any, *, scope: str | None, reverts: str | None = None,
) -> dict:
    """Stage ``spec`` with ``args``; for ``agent:auto`` on a ``write`` tool,
    then confirm it in this request. ``reverts`` (TBD-589) records the action
    this one undoes, in the preview's context."""
    db = ctx.db
    user_id, org_id = ctx.user.id, ctx.org_id
    if ctx.channel == "mcp" and ctx.api_token_id is None:
        raise ToolError("scope_denied", "agent token required")
    # Auto is keyed on channel AND scope AND risk: no JWT principal reaches it
    # and a sensitive tool never does.
    auto = ctx.auto and spec.risk == "write"
    p = _principal(ctx, user_id)

    if auto:  # BEFORE staging: an exhausted budget never degrades to a preview
        await _hit(f"agent:{p}:auto:day", 100, DAY, "auto_budget_exhausted")
        await _hit(f"agent:usr:{user_id}:auto:day", 200, DAY, "auto_budget_exhausted")
    await _hit(f"agent:{p}:preview:min", 20, MINUTE, "preview_rate_limited")
    await _hit(f"agent:{p}:preview:day", 200, DAY, "preview_rate_limited")
    if ctx.channel == "mcp":  # in-app, the principal bucket IS the per-user one
        await _hit(f"agent:usr:{user_id}:preview:day", 200, DAY, "preview_rate_limited")
    await _hit(f"agent:org:{org_id}:preview:day", 1000, DAY, "preview_rate_limited")
    await _check_ceiling(ctx, user_id)

    pv = _preview_dict(await registry.call_mapped(spec.preview, ctx, args))
    if reverts is not None:
        pv["context"]["reverts"] = reverts
    row = _new_row(
        ctx, user_id, spec, args.model_dump(mode="json"), pv,
        ActionMode.AUTO if auto else ActionMode.CONFIRM,
    )
    action_id = row.id
    staged = _staged(row)
    db.add(row)
    await db.commit()
    if not auto:
        return staged

    try:
        return await confirm(ctx, action_id, scope=scope)
    except ToolError:
        # Refused before the claim (a limit): never leave an auto row pending.
        # A claimed row is already failed/stale and this is a no-op for it.
        await db.rollback()
        await db.execute(
            update(Action).where(Action.id == action_id, Action.status == ActionStatus.PENDING)
            .values(status=ActionStatus.CANCELLED, decided_at=utcnow_naive())
        )
        await db.commit()
        raise


# ── confirm / cancel ──────────────────────────────────────────────────────

async def _explain_miss(ctx: ToolContext, user_id: int, action_id: str) -> None:
    """The conditional UPDATE matched nothing: say why. Ownership is part of the
    lookup, so an id that is not yours is indistinguishable from one that does
    not exist."""
    row = (await ctx.db.execute(
        select(Action).where(*_mine(ctx, user_id, action_id))
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if row is None:
        raise ToolError("action_not_found")
    if row.status == ActionStatus.PENDING and row.expires_at <= utcnow_naive():
        raise ToolError("action_expired")
    data = {"status": row.status.value, "result": row.result_json, "error_code": row.error_code}
    if row.status == ActionStatus.EXECUTING:
        raise ToolError("action_in_progress", data=data)
    raise ToolError("action_already_decided", data=data)


async def cancel(ctx: ToolContext, action_id: str, *, scope: str | None) -> dict:
    _scope_gate(ctx, scope, decide=frozenset({"agent:write", "agent:auto"}))
    user_id = ctx.user.id
    now = utcnow_naive()
    res = await ctx.db.execute(
        update(Action).where(
            *_mine(ctx, user_id, action_id), Action.status == ActionStatus.PENDING,
            Action.expires_at > now,
        ).values(status=ActionStatus.CANCELLED, decided_at=now)
    )
    await ctx.db.commit()
    if res.rowcount == 0:
        await _explain_miss(ctx, user_id, action_id)
    return {"action_id": action_id, "status": "cancelled"}


async def confirm(ctx: ToolContext, action_id: str, *, scope: str | None) -> dict:
    db = ctx.db
    # Read now: a rollback expires the ORM user and a lazy load would raise.
    user, user_id, email, org_id = ctx.user, ctx.user.id, ctx.user.email, ctx.org_id
    _scope_gate(ctx, scope)
    p = _principal(ctx, user_id)
    await _hit(f"agent:{p}:confirm", 30, HOUR, "confirm_rate_limited")

    # 1. Claim. ONE conditional UPDATE, committed: the WHERE is the lock.
    now = utcnow_naive()
    res = await db.execute(
        update(Action).where(
            *_mine(ctx, user_id, action_id), Action.status == ActionStatus.PENDING,
            Action.expires_at > now,
        ).values(status=ActionStatus.EXECUTING, decided_at=now)
    )
    await db.commit()
    if res.rowcount == 0:
        await _explain_miss(ctx, user_id, action_id)
    row = (await db.execute(
        select(Action).where(Action.id == action_id).execution_options(populate_existing=True)
    )).scalar_one()
    tool, args_json, preview_json = row.tool, row.args_json, row.preview_json
    fingerprint, args_sha, risk, mode = row.fingerprint, row.args_sha256, row.risk.value, row.mode
    token_id = row.api_token_id
    reverts = (preview_json.get("context") or {}).get("reverts")  # TBD-589: the action this undoes

    status: ActionStatus = ActionStatus.FAILED
    error_code: str | None = "internal"
    result: dict | None = None
    raised: ToolError | None = None
    fresh: Action | None = None
    finished = False

    async def finish() -> None:
        # Known ceiling: if execute committed and THIS commit then fails, the row
        # reads failed/internal although the domain write happened (the audit row
        # and result are lost; the REST route it mirrors is last-write-wins).
        # Status, the one audit row and any fresh preview commit together.
        await db.execute(
            update(Action).where(Action.id == action_id, Action.status == ActionStatus.EXECUTING)
            .values(status=status, result_json=result, error_code=error_code)
        )
        audit = audit_service.add_audit_event_to_session(
            db, event_type="agent.action.executed", actor_user_id=user_id, actor_email=email,
            target_org_id=org_id, target_org_name=None,
            request_id=structlog.contextvars.get_contextvars().get("request_id"),
            ip_address=None,
            outcome="success" if status == ActionStatus.DONE else "failure",
            detail={
                "action_id": action_id, "tool": tool, "channel": ctx.channel, "risk": risk,
                "mode": mode.value, "args_sha256": args_sha, "status": status.value,
                "error_code": error_code,
                **({"reverts": reverts} if reverts is not None else {}),
            },
        )
        audit.api_token_id = token_id
        if fresh is not None:
            db.add(fresh)
        await db.commit()

    try:
        try:
            spec = registry.get_tool(tool)
            if spec is None:
                raise ToolError("tool_retired", tool)
            await registry.check_gates(db, user, spec, ctx.channel, scope)
            if ctx.channel == "mcp" and spec.risk == "sensitive":
                await _hit(f"agent:{p}:sensitive:day", 10, DAY, "sensitive_budget_exhausted")
            try:
                args = spec.args.model_validate(args_json)
            except Exception:
                raise ToolError("invalid_arguments", "stored arguments no longer validate") from None
            new_preview = _preview_dict(await registry.call_mapped(spec.preview, ctx, args))
            if reverts is not None:
                new_preview["context"]["reverts"] = reverts
            if _fingerprint(tool, args_json, new_preview) != fingerprint:
                # The world moved since the preview: nothing runs. A fresh
                # preview replaces this row (exempt from the live ceiling: this
                # row has left pending, so the count does not grow).
                fresh = _new_row(ctx, user_id, spec, args_json, new_preview, ActionMode.CONFIRM)
                status, error_code = ActionStatus.STALE, "preview_stale"
                raised = ToolError(
                    "preview_stale", "the data changed since the preview",
                    data=_staged(fresh),
                )
            else:
                out = await registry.call_mapped(spec.execute, ctx, args)
                result = json.loads(_canonical(out))
                status, error_code = ActionStatus.DONE, None
        except ToolError as exc:
            status, error_code, raised = ActionStatus.FAILED, exc.code, exc
            await db.rollback()
        except Exception:
            await logger.aexception("agent.action.failed", tool=tool, org_id=org_id)
            await db.rollback()
            status, error_code = ActionStatus.FAILED, "internal"
            raised = ToolError("internal", "the action failed")
        await finish()
        finished = True
    finally:
        if not finished:  # an escape (cancellation, a failed commit): never leave it executing
            await db.rollback()
            status, error_code, result, fresh = ActionStatus.FAILED, "internal", None, None
            await finish()
    if raised is not None:
        raise raised
    return {
        "action_id": action_id, "status": "done", "summary": preview_json["summary"],
        "changes": preview_json["changes"], "warnings": preview_json["warnings"],
        "result": result,
    }
