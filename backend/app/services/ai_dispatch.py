"""AI dispatch chokepoint — ``call_llm`` (PR2 of AI tier train).

This is the only code path that talks to a real provider. Feature
surfaces (categorization, chat, smart_forecast — none of which are
wired in PR2) MUST go through this function. Direct adapter calls
bypass cap enforcement and the ledger and are a bug.

Flow (spec §3, PR2 scope):

1. Resolve routing via ``ai_routing_service.get_routing_for_feature``.
   Feature override beats default; no row at all -> 412 with code
   ``ai_routing_not_configured``.
2. Pre-check caps: aggregate the rolling-window (calendar month)
   ``est_cost_cents`` for the org from ``ai_usage_ledger``. Compare
   against both ``org_ai_default_caps`` AND ``org_ai_feature_caps``
   for the feature; whichever is tighter wins. Hard cap exceeded ->
   402 with code ``ai_hard_cap_exceeded`` (no adapter call, no
   ledger row).
3. Soft-cap warning: if the current usage crosses ``soft_cap_cents``
   for the first time in the period, enqueue a notification via
   ``notification_service.dispatch_notification`` for every owner /
   admin of the org. Idempotent per (org, feature_key, period) via
   a Redis marker with a 35-day TTL. The warning fires from two
   sites: a pre-call check (catches usage that was already at-or-above
   the cap going in — e.g., marker expired, retroactive ledger rows)
   and a post-write check (catches the boundary call that takes usage
   from below to at-or-above the cap for the first time). The Redis
   marker dedupes across both sites.
4. Decrypt credentials via ``ai_credential_crypto.decrypt``, build
   the adapter via ``ai_providers.get_adapter``, dispatch.
5. Time the call. Write a ledger row with token counts, cost,
   latency, success=true.
6. On adapter failure: write a ledger row with success=false,
   error_class set, tokens 0, cost 0. Re-raise as ``AIDispatchFailed``
   (passes through ``NoRoutingConfigured`` and ``AICapExceeded``
   unchanged because those are caught before the adapter call).

Soft-cap warning idempotence — design note:

The Redis marker ``ai_soft_cap_warned:{org_id}:{feature_key}:{period}``
is keyed on ``feature_key="__default__"`` when the soft cap that
fired was the org-wide default rather than a per-feature override.
This keeps a default-only soft-cap from re-firing once per feature
in the same month.

Open spec ambiguity resolved here: the brief asks whether a
rejected-by-hard-cap call writes a ledger row. **We do not write a
ledger row for hard-cap rejections.** Rationale: rejected calls
never reach the provider, so there's no cost/latency to record,
and writing zero-cost rows would inflate the row count without
adding ops signal. Hard-cap events are surfaced via the structlog
``ai.dispatch.cap.exceeded`` event and (in PR3+) an audit row.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import redis_client
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.notification import NotificationCategory
from app.models.org_ai_caps import OrgAIDefaultCaps, OrgAIFeatureCaps
from app.models.org_ai_credential import OrgAICredential
from app.models.user import Role, User
from app.services import ai_routing_service, notification_service
from app.services.ai_credential_crypto import decrypt
from app.services.ai_pricing import estimate_cost_cents
from app.services.ai_providers import (
    AIProviderError,
    LLMResponse,
    NativeNotAvailable,
    get_adapter,
)
from app.services.notification_templates import ai_cap_soft_warning


logger = structlog.stdlib.get_logger()


# 35-day TTL so the marker spans an entire monthly billing window
# plus a small buffer. The actual key incorporates the period
# (``YYYY-MM``) so once a new month starts, a stale marker won't fire.
SOFT_CAP_WARNED_TTL_SECONDS = 35 * 24 * 60 * 60


# --- Typed exceptions -------------------------------------------------


class AIDispatchError(Exception):
    """Base for typed dispatch errors. Maps to an HTTPException at the
    router layer via the route-side helpers below.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NoRoutingConfigured(AIDispatchError):
    """Neither a per-feature override nor an org default routing row
    exists. Spec §3 maps this to HTTP 412.
    """

    def __init__(self) -> None:
        super().__init__("ai_routing_not_configured")


class AICapExceeded(AIDispatchError):
    """Hard cap was exceeded at the dispatch chokepoint. Spec §3 maps
    this to HTTP 402.
    """

    def __init__(self) -> None:
        super().__init__("ai_hard_cap_exceeded")


class AIDispatchFailed(AIDispatchError):
    """Adapter raised a typed error and we wrote the ledger row before
    re-raising. The router maps this to HTTP 502.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)


# --- HTTPException mappers ------------------------------------------


def http_for_dispatch_error(exc: AIDispatchError) -> HTTPException:
    """Translate a typed dispatch error to an HTTPException for routers.

    Feature surfaces (PR3+) call this in their except handler when
    they want to surface ``call_llm`` failures with the spec's typed
    codes. Not used inside ``call_llm`` itself — the dispatcher just
    raises the typed error and the caller decides whether to surface
    it as 412/402/502 or recover internally.
    """
    if isinstance(exc, NoRoutingConfigured):
        return HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={"code": exc.code},
        )
    if isinstance(exc, AICapExceeded):
        return HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": exc.code},
        )
    if isinstance(exc, NativeNotAvailable):
        return HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={"code": "ai_native_not_available"},
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": exc.code},
    )


# --- Cap resolution + check -------------------------------------------


@dataclass(frozen=True)
class _ResolvedCaps:
    soft_cap_cents: Optional[int]
    hard_cap_cents: Optional[int]
    # ``True`` if the resolved row was the per-feature override, which
    # changes the Redis marker key shape.
    from_feature_override: bool


def _tighter_cap(a: Optional[int], b: Optional[int]) -> Optional[int]:
    """Return the smaller of two optional cap values.

    ``None`` means "no cap"; a real integer is always tighter than
    None. Two ``None``s means no cap at all.
    """
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


async def _resolve_caps(
    db: AsyncSession, *, org_id: int, feature_key: str
) -> _ResolvedCaps:
    """Resolve effective caps for (org_id, feature_key).

    Both default + feature caps must pass — whichever is tighter
    wins. We return a single (soft, hard) tuple representing the
    composite enforcement.
    """
    default_row = (
        await db.execute(
            select(OrgAIDefaultCaps).where(OrgAIDefaultCaps.org_id == org_id)
        )
    ).scalar_one_or_none()
    feat_row = (
        await db.execute(
            select(OrgAIFeatureCaps).where(
                OrgAIFeatureCaps.org_id == org_id,
                OrgAIFeatureCaps.feature_key == feature_key,
            )
        )
    ).scalar_one_or_none()

    default_soft = default_row.soft_cap_cents if default_row else None
    default_hard = default_row.hard_cap_cents if default_row else None
    feat_soft = feat_row.soft_cap_cents if feat_row else None
    feat_hard = feat_row.hard_cap_cents if feat_row else None

    soft = _tighter_cap(default_soft, feat_soft)
    hard = _tighter_cap(default_hard, feat_hard)
    # The marker key distinguishes "feature override caused this" vs
    # "org default caused this". Whichever cap value actually fired
    # wins. The choice only matters for soft-cap warning dedup.
    from_feature_override = feat_row is not None and (
        feat_soft is not None or feat_hard is not None
    )
    return _ResolvedCaps(
        soft_cap_cents=soft,
        hard_cap_cents=hard,
        from_feature_override=from_feature_override,
    )


def _current_period(now: Optional[datetime] = None) -> str:
    """Return the calendar-month period string used for cap windows.

    Format ``YYYY-MM`` in UTC. Aggregates across all rows whose
    ``dispatched_at`` falls inside this calendar month for the org.
    """
    now = now or datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def _month_start(now: Optional[datetime] = None) -> datetime:
    """First instant of the current calendar month (UTC, naive)."""
    now = now or datetime.now(timezone.utc)
    # Naive because the ledger column is naive DateTime on SQLite +
    # MySQL alike. The server_default for ``dispatched_at`` is also
    # naive, so this matches the comparison shape.
    return datetime(now.year, now.month, 1)


async def _aggregate_cost_cents(
    db: AsyncSession, *, org_id: int, since: datetime
) -> int:
    """Sum est_cost_cents for the org for the current period."""
    total = await db.scalar(
        select(func.coalesce(func.sum(AIUsageLedger.est_cost_cents), 0)).where(
            AIUsageLedger.org_id == org_id,
            AIUsageLedger.dispatched_at >= since,
        )
    )
    return int(total or 0)


# --- Soft-cap warning -------------------------------------------------


async def _list_org_admin_user_ids(
    db: AsyncSession, *, org_id: int
) -> list[int]:
    """Return user_ids for org admins/owners; recipients for the warning.

    PR2 dispatches the warning to every owner+admin of the org. The
    notification service is per-user, so a single soft-cap event
    fans out to N rows. Idempotence (per org+feature+period) lives
    in the Redis marker that wraps this whole branch.
    """
    res = await db.execute(
        select(User.id).where(
            User.org_id == org_id,
            User.role.in_([Role.OWNER, Role.ADMIN]),
        )
    )
    return [int(r) for r in res.scalars().all()]


async def _maybe_warn_soft_cap(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: str,
    resolved: _ResolvedCaps,
    cost_before_call: int,
    period: str,
) -> bool:
    """Fire the first-time soft-cap warning for this period if needed.

    Returns ``True`` if a warning was dispatched (or attempted), else
    ``False``. Idempotence is guarded by a Redis marker; if Redis is
    unavailable the warning still fires (degrades to "warn every call"
    rather than skipping silently).
    """
    if resolved.soft_cap_cents is None:
        return False
    if cost_before_call < resolved.soft_cap_cents:
        return False

    marker_feature = (
        feature_key if resolved.from_feature_override else "__default__"
    )
    marker_key = (
        f"ai_soft_cap_warned:{org_id}:{marker_feature}:{period}"
    )

    redis = redis_client.get_client()
    if redis is not None:
        try:
            # SET NX + EX guarantees a single warning per period.
            set_ok = await redis.set(
                marker_key,
                "1",
                ex=SOFT_CAP_WARNED_TTL_SECONDS,
                nx=True,
            )
            if not set_ok:
                return False
        except Exception as exc:  # pragma: no cover - resilience path
            # Don't let a Redis blip suppress the warning. Worst case
            # the org sees the warning more than once per period; the
            # ops signal still surfaces.
            logger.warning(
                "ai.dispatch.soft_cap_marker.redis_error",
                error_class=type(exc).__name__,
                org_id=org_id,
                feature_key=feature_key,
            )

    percent = 0
    if resolved.soft_cap_cents > 0:
        percent = min(
            100, int(round((cost_before_call / resolved.soft_cap_cents) * 100))
        )
    title, body, link_url = ai_cap_soft_warning(
        feature_key=feature_key, period=period, percent=percent
    )

    recipients = await _list_org_admin_user_ids(db, org_id=org_id)
    for user_id in recipients:
        await notification_service.dispatch_notification(
            db,
            user_id=user_id,
            category=NotificationCategory.ORG_ADMIN,
            event_type="ai.cap.soft_warning",
            title=title,
            body=body,
            link_url=link_url,
        )
    await db.commit()

    logger.info(
        "ai.dispatch.soft_cap.warned",
        org_id=org_id,
        feature_key=feature_key,
        period=period,
        soft_cap_cents=resolved.soft_cap_cents,
        cost_before_call=cost_before_call,
        recipients=len(recipients),
    )
    return True


# --- Ledger write -----------------------------------------------------


async def _write_ledger_row(
    db: AsyncSession,
    *,
    org_id: int,
    credential_id: int,
    feature_key: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    est_cost_cents_value: int,
    latency_ms: int,
    success: bool,
    error_class: Optional[str],
) -> AIUsageLedger:
    row = AIUsageLedger(
        org_id=org_id,
        credential_id=credential_id,
        feature_key=feature_key,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        est_cost_cents=est_cost_cents_value,
        latency_ms=latency_ms,
        success=success,
        error_class=error_class,
        dispatched_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


# --- Public chokepoint ------------------------------------------------


@dataclass(frozen=True)
class DispatchResult:
    """Internal-only result type for ``call_llm`` callers.

    Carries the LLMResponse plus the ledger row id so the caller can
    correlate downstream events (PR3+ may need this for audit).
    """

    response: LLMResponse
    ledger_id: int


async def call_llm(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: str,
    request_payload: dict,
    capability: str = "chat",
) -> DispatchResult:
    """Dispatch a single LLM call through the cap + ledger chokepoint.

    Args:
        db: per-request AsyncSession.
        org_id: routing + cap scope.
        feature_key: maps to a ``ROUTABLE_FEATURE_NAMES`` entry.
        request_payload: provider-neutral request body. PR2 only
            wires ``capability="chat"``; the payload must carry
            ``messages: list[dict]`` and may carry ``max_tokens``.
        capability: which adapter method to call. PR2 ships ``"chat"``
            only; other capabilities raise NotImplementedError.

    Returns:
        DispatchResult with the LLMResponse and the ledger row id.

    Raises:
        NoRoutingConfigured: no routing row at all -> HTTP 412.
        AICapExceeded: hard cap reached -> HTTP 402.
        NativeNotAvailable: native gate still off / not implemented.
        AIDispatchFailed: any provider failure (network, 4xx, 5xx,
            timeout, JSON parse). Ledger row written with success=false.
    """
    if capability != "chat":
        # PR2 wires chat only. Spec §3 future-allows embed/function_call/
        # stream but those land in PR3+.
        raise NotImplementedError(
            f"capability={capability!r} not wired in PR2; chat only"
        )

    # 1. Resolve routing.
    routing = await ai_routing_service.get_routing_for_feature(
        db, org_id=org_id, feature_name=feature_key
    )
    if routing is None:
        logger.info(
            "ai.dispatch.routing.missing",
            org_id=org_id,
            feature_key=feature_key,
        )
        raise NoRoutingConfigured()
    credential_id, model = routing

    # Pull the credential row (we need provider, base_url, encrypted
    # key/bearer). The routing FK structurally pins it to org_id, but
    # check belt-and-suspenders anyway.
    cred = (
        await db.execute(
            select(OrgAICredential).where(
                OrgAICredential.id == credential_id,
                OrgAICredential.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if cred is None:
        # Defensive: routing FK should make this unreachable.
        logger.error(
            "ai.dispatch.routing.dangling",
            org_id=org_id,
            feature_key=feature_key,
            credential_id=credential_id,
        )
        raise NoRoutingConfigured()

    # 2. Pre-check caps.
    resolved = await _resolve_caps(
        db, org_id=org_id, feature_key=feature_key
    )
    cost_so_far = await _aggregate_cost_cents(
        db, org_id=org_id, since=_month_start()
    )
    if (
        resolved.hard_cap_cents is not None
        and cost_so_far >= resolved.hard_cap_cents
    ):
        logger.info(
            "ai.dispatch.cap.exceeded",
            org_id=org_id,
            feature_key=feature_key,
            cost_so_far=cost_so_far,
            hard_cap_cents=resolved.hard_cap_cents,
        )
        raise AICapExceeded()

    # 3. Soft-cap warning (first-time-in-period).
    await _maybe_warn_soft_cap(
        db,
        org_id=org_id,
        feature_key=feature_key,
        resolved=resolved,
        cost_before_call=cost_so_far,
        period=_current_period(),
    )

    # 4. Build adapter.
    api_key = decrypt(cred.encrypted_api_key)
    bearer = (
        decrypt(cred.encrypted_bearer_token)
        if cred.encrypted_bearer_token
        else None
    )
    adapter = get_adapter(
        cred.provider,
        api_key=api_key,
        bearer_token=bearer,
        base_url=cred.base_url,
    )

    messages = request_payload.get("messages") or []
    max_tokens = request_payload.get("max_tokens")

    # 5. Time + dispatch.
    start = time.perf_counter()
    try:
        response: LLMResponse = await adapter.chat(  # type: ignore[attr-defined]
            model=model,
            messages=messages,
            max_tokens=max_tokens,
        )
    except NativeNotAvailable:
        # Don't write a ledger row — the call never reached a provider
        # and "rejected because native is dormant" is the same posture
        # as "rejected because routing missing" (no spend, no signal
        # for the ops view).
        logger.info(
            "ai.dispatch.native.unavailable",
            org_id=org_id,
            feature_key=feature_key,
        )
        raise
    except AIProviderError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        await _write_ledger_row(
            db,
            org_id=org_id,
            credential_id=credential_id,
            feature_key=feature_key,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            est_cost_cents_value=0,
            latency_ms=latency_ms,
            success=False,
            error_class=exc.code,
        )
        logger.info(
            "ai.dispatch.adapter.failed",
            org_id=org_id,
            feature_key=feature_key,
            error_class=exc.code,
            latency_ms=latency_ms,
        )
        raise AIDispatchFailed(exc.code) from None
    except Exception as exc:  # pragma: no cover - defensive
        latency_ms = int((time.perf_counter() - start) * 1000)
        await _write_ledger_row(
            db,
            org_id=org_id,
            credential_id=credential_id,
            feature_key=feature_key,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            est_cost_cents_value=0,
            latency_ms=latency_ms,
            success=False,
            error_class=type(exc).__name__,
        )
        logger.warning(
            "ai.dispatch.unexpected_error",
            org_id=org_id,
            feature_key=feature_key,
            error_class=type(exc).__name__,
            latency_ms=latency_ms,
        )
        raise AIDispatchFailed(type(exc).__name__) from None

    latency_ms = int((time.perf_counter() - start) * 1000)
    cost = estimate_cost_cents(
        model=model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )

    # 6. Success ledger row.
    ledger = await _write_ledger_row(
        db,
        org_id=org_id,
        credential_id=credential_id,
        feature_key=feature_key,
        model=model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        est_cost_cents_value=cost,
        latency_ms=latency_ms,
        success=True,
        error_class=None,
    )

    # 7. Post-write boundary check: catch the very first call that
    # CROSSES the soft cap. The pre-call check (step 3) only fires when
    # ``cost_so_far`` is already at-or-above the cap, so the call that
    # takes us from below to at-or-above the cap was previously missed.
    # The Redis dedupe marker shared with ``_maybe_warn_soft_cap``
    # ensures we don't double-fire when both checks would otherwise
    # match (pre-call already set the marker -> post-write SET NX
    # returns False -> warning skipped).
    if (
        resolved.soft_cap_cents is not None
        and cost_so_far < resolved.soft_cap_cents <= cost_so_far + cost
    ):
        await _maybe_warn_soft_cap(
            db,
            org_id=org_id,
            feature_key=feature_key,
            resolved=resolved,
            cost_before_call=cost_so_far + cost,
            period=_current_period(),
        )

    logger.info(
        "ai.dispatch.success",
        org_id=org_id,
        feature_key=feature_key,
        model=model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        est_cost_cents=cost,
        latency_ms=latency_ms,
        ledger_id=ledger.id,
    )

    # Best-effort last_used_at refresh — same fire-and-forget posture
    # as the unwrap path in PR1's credential_service.
    asyncio.create_task(_touch_last_used(cred.id))  # noqa: RUF006

    return DispatchResult(response=response, ledger_id=ledger.id)


async def _touch_last_used(credential_id: int) -> None:  # pragma: no cover
    """Stub for the deferred last_used_at refresh hook.

    PR1's credential_service.unwrap already has the bookkeeping live;
    this stub is a placeholder so the call site in ``call_llm`` keeps
    its current shape when the unified hook lands.
    """
    _ = credential_id
    return
