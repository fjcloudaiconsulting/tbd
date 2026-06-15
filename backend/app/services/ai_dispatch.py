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
fired was the org-wide default rather than a per-feature soft-cap
override. This keeps a default-only soft-cap from re-firing once per
feature in the same month.

The "did the feature row override this" decision is made on the
**soft cap alone**. A feature row that supplies only a hard cap (soft
left null, default soft still winning) MUST NOT scope the marker to
the feature — that would let the same org-wide soft-cap warning fire
once per feature in a period. See ``_resolve_caps`` for the source-
tracking implementation.

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
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import structlog
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import redis_client
from app.config import settings
from app.models.ai_usage_ledger import AIUsageLedger
from app.models.notification import NotificationCategory
from app.models.org_ai_caps import OrgAIDefaultCaps, OrgAIFeatureCaps
from app.models.org_ai_credential import OrgAICredential
from app.models.user import Role, User
from app.services import ai_routing_service, notification_service
from app.services.ai_credential_crypto import decrypt
from app.services.ai_pricing import estimate_cost_cents
from app.services.ai_token_estimate import (
    default_max_output_tokens_for,
    estimate_prompt_tokens_from_messages,
)
from app.services.ai_providers import (
    AIProviderError,
    CapabilityNotSupported,
    EmbedResponse,
    FunctionCallResponse,
    LLMResponse,
    NativeNotAvailable,
    StreamChunk,
    StructuredOutputError,
    StructuredResponse,
    TokenUsage,
    get_adapter,
)
from app.services.notification_templates import ai_cap_soft_warning


# Architect lock #13 (StructuredOutputCapable retry cap): max 2 retries
# on JSON parse / schema-validation failure (3 total attempts) before
# ``STATUS_ERROR_STRUCTURED_OUTPUT``. The counter lives at the SERVICE
# level (not the adapter) — each adapter emits a single response.
STRUCTURED_OUTPUT_MAX_RETRIES = 2


# Typed error code for a dispatch that blew the wall-clock bound. Routed
# through ``AIProviderError`` so the existing per-capability except
# handlers write a system-failure ledger row and re-raise it as
# ``AIDispatchFailed("provider_timeout")`` (a 5xx). Audit semantics:
# ``ai_dispatch_failed:provider_timeout`` is a SYSTEM failure, not a
# user-state precondition (see reference_ai_audit_outcome_semantics).
DISPATCH_TIMEOUT_ERROR_CODE = "provider_timeout"


logger = structlog.stdlib.get_logger()


async def _with_dispatch_timeout(awaitable):
    """Bound a single provider awaitable by the configured wall clock.

    The per-provider HTTP adapters carry their own coarse connect/read
    timeouts (10 s validate, 30-60 s chat-stream), but those are too
    loose to stop a slow or hung provider from pinning a dispatch
    worker. ``settings.ai_dispatch_timeout_s`` (default 5 s) is the hard
    ceiling on any one adapter call; on expiry the underlying coroutine
    is cancelled and we raise a sanitized ``AIProviderError`` so the
    caller's existing failure path writes the ledger row and re-raises
    as ``AIDispatchFailed("provider_timeout")``.

    The raw provider payload is never carried through — only the typed
    ``provider_timeout`` code, same posture as the rest of the adapter
    error surface.
    """
    try:
        return await asyncio.wait_for(
            awaitable, timeout=settings.ai_dispatch_timeout_s
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise AIProviderError(code=DISPATCH_TIMEOUT_ERROR_CODE) from exc


def _stream_with_dispatch_timeout(
    stream: "AsyncIterator[StreamChunk]",
) -> "AsyncIterator[StreamChunk]":
    """Bound the wait for EACH chunk of a provider stream by the wall
    clock — NOT the total stream duration.

    A stream can't be wrapped in a single ``wait_for`` — it's an async
    iterator, not one awaitable. Instead we bound the wait for each
    chunk: if any single ``__anext__`` takes longer than the ceiling,
    the pull is cancelled and surfaced as the same ``provider_timeout``
    ``AIProviderError`` the non-stream paths raise. Inter-chunk gaps on
    a healthy stream are far below the bound, so this only fires on a
    genuinely stalled stream.

    Deliberate limitation: because the bound is per-chunk, a provider
    that dribbles one chunk just under the ceiling forever is NOT
    caught — only a fully stalled stream (no chunk inside the bound)
    trips it. This is intentional: a slow-but-healthy stream should
    keep flowing rather than be killed for failing to finish by some
    total deadline. Only a hang is an error here.

    Resource lifecycle: the underlying provider ``stream`` is an async
    generator whose ``yield``s sit inside an ``async with
    httpx.AsyncClient()`` block. On a mid-stream timeout (we raise) or
    an early consumer break (``call_llm_stream`` breaks on
    ``chunk.done``, throwing ``GeneratorExit`` into this generator), we
    MUST close the source iterator so that ``async with`` unwinds and
    the HTTP connection is released promptly instead of waiting on GC.
    The ``finally`` below awaits ``aclose`` when the iterator exposes
    it (every provider async generator does).
    """

    async def _bounded() -> AsyncIterator[StreamChunk]:
        iterator = stream.__aiter__()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        iterator.__anext__(),
                        timeout=settings.ai_dispatch_timeout_s,
                    )
                except StopAsyncIteration:
                    return
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    raise AIProviderError(
                        code=DISPATCH_TIMEOUT_ERROR_CODE
                    ) from exc
                yield chunk
        finally:
            # Finalize the provider stream (closes its httpx client) on
            # any exit: mid-stream timeout, early consumer break
            # (GeneratorExit), or normal completion.
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                await aclose()

    return _bounded()


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


class AICapabilityNotSupported(AIDispatchError):
    """The credential resolved by routing does not advertise the
    requested capability in its ``discovered_capabilities``. Maps to
    HTTP 412 ``ai_capability_not_supported``. Caller is expected to
    surface a "reconfigure routing" hint to the user.

    Distinct from ``CapabilityNotSupported`` (adapter-level — raised
    when the wire-call itself isn't honored, e.g. Ollama function
    calling on a non-tool model). This dispatch-level error fires
    BEFORE the adapter call.
    """

    def __init__(self, *, capability: str, feature_key: str) -> None:
        super().__init__("ai_capability_not_supported")
        self.capability = capability
        self.feature_key = feature_key


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
    if isinstance(exc, AICapabilityNotSupported):
        return HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={
                "code": exc.code,
                "capability": exc.capability,
                "feature_key": exc.feature_key,
                "message": (
                    f"Configured credential for {exc.feature_key} does not "
                    f"support {exc.capability}. Reconfigure routing to use "
                    "a provider that supports it."
                ),
            },
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
    # ``True`` iff the EFFECTIVE soft cap (the one ``soft_cap_cents``
    # holds) came from the per-feature override row. This is used to
    # shape the Redis dedupe marker for soft-cap warnings.
    #
    # IMPORTANT: this tracks the source of the SOFT cap only. The hard
    # cap's source is intentionally not tracked here — it does not
    # affect warning dedupe, and conflating the two would let a
    # feature-specific hard cap silently fragment an org-wide soft-cap
    # warning into one-per-feature.
    soft_from_feature_override: bool


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


def _soft_cap_from_feature(
    default_soft: Optional[int], feat_soft: Optional[int]
) -> bool:
    """Decide whether the effective soft cap came from the feature row.

    The effective soft cap is ``_tighter_cap(default_soft, feat_soft)``.
    "From feature" means the feature row supplied a soft cap AND that
    soft cap is the one that won (i.e. the feature soft cap is tighter
    than the default, or the default is absent).

    Examples:
      default=None, feat=None      -> False  (no soft cap at all)
      default=100,  feat=None      -> False  (default wins)
      default=None, feat=50        -> True   (feature wins, default absent)
      default=100,  feat=50        -> True   (feature wins, tighter)
      default=50,   feat=100       -> False  (default wins, tighter)
      default=50,   feat=50        -> False  (tie — default wins; the
                                              dedupe scope is org-wide
                                              and that's the safer default)
    """
    if feat_soft is None:
        return False
    if default_soft is None:
        return True
    # Strict less-than: ties resolve to the default (org-wide marker).
    return feat_soft < default_soft


async def _resolve_caps(
    db: AsyncSession, *, org_id: int, feature_key: str
) -> _ResolvedCaps:
    """Resolve effective caps for (org_id, feature_key).

    Both default + feature caps must pass — whichever is tighter
    wins. We return a single (soft, hard) tuple representing the
    composite enforcement.

    Source tracking — only the SOFT cap's source is tracked, via
    ``soft_from_feature_override``. The hard cap's source is not
    surfaced because it does not affect any downstream behavior; in
    particular it must NOT influence the soft-cap warning dedupe
    marker. Bug guard: a feature row with ``soft_cap_cents=None`` and
    ``hard_cap_cents=300`` (default soft still wins) must produce
    ``soft_from_feature_override=False`` so the warning dedupe stays
    org-wide via the ``__default__`` marker.
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
    return _ResolvedCaps(
        soft_cap_cents=soft,
        hard_cap_cents=hard,
        soft_from_feature_override=_soft_cap_from_feature(
            default_soft, feat_soft
        ),
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


async def _resolve_caps_and_cost(
    db: AsyncSession, *, org_id: int, feature_key: str
) -> tuple[_ResolvedCaps, int]:
    """Resolve effective caps + the org's spend in the current period.

    Single source of truth for the (resolve caps, aggregate cost over the
    current calendar month) pair that both the dispatch chokepoint and the
    estimate pre-check need. Same feature-key cap resolution, same
    ``_month_start`` boundary, same aggregation window as before.
    """
    resolved = await _resolve_caps(
        db, org_id=org_id, feature_key=feature_key
    )
    cost_so_far = await _aggregate_cost_cents(
        db, org_id=org_id, since=_month_start()
    )
    return resolved, cost_so_far


async def remaining_hard_cap_cents(
    db: AsyncSession, *, org_id: int, feature_key: str
) -> Optional[int]:
    """Remaining hard-cap headroom (cents) for (org_id, feature_key).

    Composes the same internals the dispatcher uses to gate spend:
    resolve the effective hard cap (default + feature, tighter wins) and
    subtract the org's already-spent cost for the current calendar month.

    Returns ``None`` when no hard cap is configured — the sentinel for
    "unlimited headroom" that callers already treat as no gate. Otherwise
    returns ``hard_cap_cents - cost_so_far``, which may be zero or
    negative when the org is at or over its cap. The dispatch refusal
    boundary is ``cost_so_far >= hard_cap`` (i.e. ``remaining <= 0``).

    Resolves caps FIRST and short-circuits on the no-cap path: the
    ``cost_so_far`` aggregation (a ``SUM`` over the ledger) is only run
    when a hard cap actually exists, since the no-cap path discards it.
    The two ``_prepare_dispatch`` sites keep using ``_resolve_caps_and_cost``
    because they always need the aggregate (soft-cap warning + ledger).
    """
    resolved = await _resolve_caps(
        db, org_id=org_id, feature_key=feature_key
    )
    if resolved.hard_cap_cents is None:
        return None
    cost_so_far = await _aggregate_cost_cents(
        db, org_id=org_id, since=_month_start()
    )
    return resolved.hard_cap_cents - cost_so_far


# --- Projected-overspend gate ----------------------------------------


def _projected_cost_cents(
    model: str,
    messages: list[dict],
    max_tokens: Optional[int],
    *,
    retry_multiplier: int = 1,
) -> int:
    """Conservative worst-case cost (cents) for a not-yet-made call.

    Prompt tokens come from the shared char heuristic over ``messages``;
    completion tokens are the caller's pinned ``max_tokens`` or the
    model's worst-case output ceiling when unpinned. ``retry_multiplier``
    scales the single-call estimate for callers that retry and aggregate
    spend across attempts (e.g. structured output). Unknown models price
    via ``estimate_cost_cents``'s conservative ``_default`` row, so the
    projection is always computable.
    """
    prompt_tokens = estimate_prompt_tokens_from_messages(messages)
    completion_tokens = (
        max_tokens if max_tokens else default_max_output_tokens_for(model)
    )
    single = estimate_cost_cents(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    return single * retry_multiplier


def _enforce_cap(
    *,
    resolved: "_ResolvedCaps",
    cost_so_far: int,
    model: str,
    messages: list[dict],
    max_tokens: Optional[int],
    retry_multiplier: int,
    org_id: int,
    feature_key: str,
    capability: Optional[str] = None,
) -> None:
    """Universal hard-cap gate: block if already exhausted OR if this
    call's conservative projected cost would tip the org over its hard
    cap. Raises ``AICapExceeded`` (→ 402 ``ai_hard_cap_exceeded``).

    No cap configured (``hard_cap_cents is None``) → no gate.

    Fail-closed: if projection raises for any reason, ``projected`` is
    pinned to 0 and a warning is logged. This degrades the gate to
    exhausted-only enforcement — it never skips the gate and never 500s
    the dispatch hot path. The explicit ``cost_so_far >= hard_cap`` arm
    keeps an at-cap org blocked even when ``projected`` is 0.

    Writes no ledger row and no audit event at this layer (matches the
    prior inline exhausted block); ``AICapExceeded`` is audited as a
    success-precondition by the routers.
    """
    if resolved.hard_cap_cents is None:
        return

    try:
        projected = _projected_cost_cents(
            model,
            messages,
            max_tokens,
            retry_multiplier=retry_multiplier,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed, never crash dispatch
        projected = 0
        logger.warning(
            "ai.dispatch.cap.projection_failed",
            org_id=org_id,
            feature_key=feature_key,
            capability=capability,
            error_class=type(exc).__name__,
        )

    if (
        cost_so_far >= resolved.hard_cap_cents
        or cost_so_far + projected > resolved.hard_cap_cents
    ):
        logger.info(
            "ai.dispatch.cap.exceeded",
            org_id=org_id,
            feature_key=feature_key,
            capability=capability,
            cost_so_far=cost_so_far,
            hard_cap_cents=resolved.hard_cap_cents,
            projected_cost_cents=projected,
            reason=(
                "exhausted"
                if cost_so_far >= resolved.hard_cap_cents
                else "projected"
            ),
        )
        raise AICapExceeded()


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

    # The marker is scoped by where the EFFECTIVE soft cap came from.
    # If the org-default soft cap is the one that fired, the marker is
    # ``__default__`` so the warning fires ONCE for the org per period
    # (not once per feature). If a feature row supplied its own tighter
    # soft cap, that warning is feature-scoped instead.
    marker_feature = (
        feature_key if resolved.soft_from_feature_override else "__default__"
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
    retries_used: int = 0,
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
        retries_used=retries_used,
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

    # 2. Pre-check caps (exhausted + projected-overspend gate).
    resolved, cost_so_far = await _resolve_caps_and_cost(
        db, org_id=org_id, feature_key=feature_key
    )
    _enforce_cap(
        resolved=resolved,
        cost_so_far=cost_so_far,
        model=model,
        messages=(request_payload.get("messages") or []),
        max_tokens=request_payload.get("max_tokens"),
        retry_multiplier=1,
        org_id=org_id,
        feature_key=feature_key,
        capability=capability,
    )

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
    api_key = (
        decrypt(cred.encrypted_api_key) if cred.encrypted_api_key else None
    )
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
        response: LLMResponse = await _with_dispatch_timeout(
            adapter.chat(  # type: ignore[attr-defined]
                model=model,
                messages=messages,
                max_tokens=max_tokens,
            )
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


# ----------------------------------------------------------------------
# PR3 capability dispatch wrappers
# ----------------------------------------------------------------------
#
# The chat path (``call_llm``) above stays unchanged. The PR3 wrappers
# share a ``_prepare_dispatch`` helper that handles routing,
# credential decryption, adapter construction, the capability check,
# and the cap pre-check. Each wrapper then dispatches the adapter
# method, writes the ledger row, and applies its capability-specific
# post-processing (retry budget for structured output, end-of-stream
# ledger write for streams, etc.).


@dataclass(frozen=True)
class _PreparedDispatch:
    """Output of ``_prepare_dispatch``.

    Carries everything ``call_llm_*`` wrappers need to talk to the
    adapter and write the ledger row at the end.
    """

    adapter: Any
    credential_id: int
    credential_pk_id: int
    model: str
    resolved: _ResolvedCaps
    cost_so_far: int


async def _prepare_dispatch(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: str,
    capability: str,
    messages: list[dict],
    max_tokens: Optional[int],
    retry_multiplier: int = 1,
) -> _PreparedDispatch:
    """Resolve routing + credential + caps for one dispatch.

    ``messages``/``max_tokens``/``retry_multiplier`` feed the universal
    projected-overspend gate (``_enforce_cap``) after ``model`` is
    resolved. Embedding callers synthesize a ``messages`` payload from
    their INPUT (the texts to embed) so the gate projects the embedding
    INPUT cost as prompt tokens, with ``max_tokens=None`` resolving to
    the model output ceiling (0 for embedding models, which emit no
    completion tokens); structured-output callers pass their retry
    budget.

    Raises ``NoRoutingConfigured``, ``AICapExceeded``, or
    ``AICapabilityNotSupported`` before the adapter is built. The
    capability check uses ``discovered_capabilities`` on the
    credential row (populated by the validate() probe) — if the
    credential doesn't list the capability we refuse with a 412 so
    the caller routes to a different provider.
    """
    routing = await ai_routing_service.get_routing_for_feature(
        db, org_id=org_id, feature_name=feature_key
    )
    if routing is None:
        logger.info(
            "ai.dispatch.routing.missing",
            org_id=org_id,
            feature_key=feature_key,
            capability=capability,
        )
        raise NoRoutingConfigured()
    credential_id, model = routing

    cred = (
        await db.execute(
            select(OrgAICredential).where(
                OrgAICredential.id == credential_id,
                OrgAICredential.org_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if cred is None:
        logger.error(
            "ai.dispatch.routing.dangling",
            org_id=org_id,
            feature_key=feature_key,
            credential_id=credential_id,
            capability=capability,
        )
        raise NoRoutingConfigured()

    # Capability check — credential must advertise the capability.
    # ``chat`` is always allowed (the original call_llm path doesn't
    # do this check for backcompat); other capabilities check the
    # credential's ``discovered_capabilities`` array. An empty/None
    # array on a legacy credential row falls through to "no
    # capabilities known" → refuse so the user re-runs validate.
    if capability != "chat":
        capabilities = cred.discovered_capabilities or []
        if capability not in capabilities:
            logger.info(
                "ai.dispatch.capability.unsupported",
                org_id=org_id,
                feature_key=feature_key,
                capability=capability,
                credential_id=credential_id,
                discovered=capabilities,
            )
            raise AICapabilityNotSupported(
                capability=capability, feature_key=feature_key
            )

    resolved, cost_so_far = await _resolve_caps_and_cost(
        db, org_id=org_id, feature_key=feature_key
    )
    _enforce_cap(
        resolved=resolved,
        cost_so_far=cost_so_far,
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        retry_multiplier=retry_multiplier,
        org_id=org_id,
        feature_key=feature_key,
        capability=capability,
    )

    await _maybe_warn_soft_cap(
        db,
        org_id=org_id,
        feature_key=feature_key,
        resolved=resolved,
        cost_before_call=cost_so_far,
        period=_current_period(),
    )

    api_key = (
        decrypt(cred.encrypted_api_key) if cred.encrypted_api_key else None
    )
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

    return _PreparedDispatch(
        adapter=adapter,
        credential_id=credential_id,
        credential_pk_id=cred.id,
        model=model,
        resolved=resolved,
        cost_so_far=cost_so_far,
    )


async def _post_write_soft_cap_crossing(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: str,
    resolved: _ResolvedCaps,
    cost_so_far: int,
    cost_this_call: int,
) -> None:
    """Catch the call that takes usage from below to at-or-above the
    soft cap (post-write check). Mirrors step 7 of ``call_llm``.
    """
    if (
        resolved.soft_cap_cents is not None
        and cost_so_far < resolved.soft_cap_cents <= cost_so_far + cost_this_call
    ):
        await _maybe_warn_soft_cap(
            db,
            org_id=org_id,
            feature_key=feature_key,
            resolved=resolved,
            cost_before_call=cost_so_far + cost_this_call,
            period=_current_period(),
        )


# --- Schema validation -------------------------------------------------


def _validate_json_against_schema(data: Any, schema: dict) -> Optional[str]:
    """Minimal JSON-schema check for structured-output validation.

    Returns ``None`` on success, or a short error string on failure.
    Only checks the schema's ``type`` ("object" or "array") and
    ``required`` keys — feature surfaces that need richer validation
    should pair this with a Pydantic model and re-validate after the
    dispatch returns.

    A full JSON Schema validator is a PR3-followup candidate; the
    retry-cap contract only requires "parse failure or structural
    failure triggers a retry", and required-keys is the structural
    property feature surfaces care about today.
    """
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(data, dict):
            return "expected object"
        required = schema.get("required") or []
        for key in required:
            if key not in data:
                return f"missing required key {key!r}"
    elif schema_type == "array" and not isinstance(data, list):
        return "expected array"
    return None


# --- call_llm_structured ---------------------------------------------


@dataclass(frozen=True)
class StructuredDispatchResult:
    response: StructuredResponse
    ledger_id: int


async def call_llm_structured(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: str,
    messages: list[dict],
    response_schema: dict,
    max_tokens: Optional[int] = None,
) -> StructuredDispatchResult:
    """Dispatch a structured-output call with the architect-locked
    retry cap (max 2 retries, 3 total attempts).

    Flow:
    1. Resolve routing/credential/caps via ``_prepare_dispatch``.
    2. Call the adapter's ``chat_structured`` up to 3 times. After a
       JSON parse / schema-validation failure, append a system message
       asking for a valid JSON object and try again.
    3. On the third failure, write the failure ledger row with
       ``retries_used=2`` and raise ``StructuredOutputError``.
    4. On success, the ledger row carries ``retries_used`` = the
       number of retries actually taken.
    """
    prepared = await _prepare_dispatch(
        db,
        org_id=org_id,
        feature_key=feature_key,
        capability="structured_output",
        messages=messages,
        max_tokens=max_tokens,
        # Structured output retries up to STRUCTURED_OUTPUT_MAX_RETRIES and
        # aggregates token spend across every attempt, so project the
        # worst case: all attempts billed.
        retry_multiplier=STRUCTURED_OUTPUT_MAX_RETRIES + 1,
    )

    retry_message = {
        "role": "system",
        "content": (
            "Previous response was not valid JSON matching the schema. "
            "Output ONLY the JSON object."
        ),
    }
    attempt_messages = list(messages)

    start = time.perf_counter()
    last_error: Optional[str] = None
    # Aggregate token spend across every attempt that successfully
    # returned from the adapter. The ledger row written at the end
    # (success or exhaustion) reflects the cumulative cost of THIS
    # ``call_llm_structured`` invocation, so cap accounting captures
    # tokens billed by intermediate retries that the previous
    # last-attempt-only ledger row silently dropped.
    total_prompt_tokens = 0
    total_completion_tokens = 0

    for attempt in range(STRUCTURED_OUTPUT_MAX_RETRIES + 1):
        try:
            response: LLMResponse = await _with_dispatch_timeout(
                prepared.adapter.chat_structured(
                    model=prepared.model,
                    messages=attempt_messages,
                    schema=response_schema,
                    max_tokens=max_tokens,
                )
            )
        except NativeNotAvailable:
            logger.info(
                "ai.dispatch.native.unavailable",
                org_id=org_id,
                feature_key=feature_key,
                capability="structured_output",
            )
            raise
        except (AIProviderError, CapabilityNotSupported) as exc:
            latency_ms = int((time.perf_counter() - start) * 1000)
            error_code = (
                exc.code
                if isinstance(exc, AIProviderError)
                else "capability_not_supported"
            )
            # Bill any tokens already consumed by previous attempts
            # before the adapter raised; first-attempt failures
            # naturally land at zero because no successful response
            # ever returned.
            cost = estimate_cost_cents(
                model=prepared.model,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
            )
            await _write_ledger_row(
                db,
                org_id=org_id,
                credential_id=prepared.credential_id,
                feature_key=feature_key,
                model=prepared.model,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                est_cost_cents_value=cost,
                latency_ms=latency_ms,
                success=False,
                error_class=error_code,
                retries_used=attempt,
            )
            logger.info(
                "ai.dispatch.structured.failed",
                org_id=org_id,
                feature_key=feature_key,
                error_class=error_code,
                latency_ms=latency_ms,
            )
            raise AIDispatchFailed(error_code) from None

        # Successful provider call — its tokens count regardless of
        # whether downstream JSON parse / schema validation passes.
        total_prompt_tokens += response.prompt_tokens
        total_completion_tokens += response.completion_tokens

        try:
            parsed = json.loads(response.content)
        except (TypeError, ValueError) as exc:
            last_error = f"json_decode:{type(exc).__name__}"
            attempt_messages = attempt_messages + [retry_message]
            continue
        err = _validate_json_against_schema(parsed, response_schema)
        if err is None:
            latency_ms = int((time.perf_counter() - start) * 1000)
            cost = estimate_cost_cents(
                model=prepared.model,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
            )
            ledger = await _write_ledger_row(
                db,
                org_id=org_id,
                credential_id=prepared.credential_id,
                feature_key=feature_key,
                model=prepared.model,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                est_cost_cents_value=cost,
                latency_ms=latency_ms,
                success=True,
                error_class=None,
                retries_used=attempt,
            )
            await _post_write_soft_cap_crossing(
                db,
                org_id=org_id,
                feature_key=feature_key,
                resolved=prepared.resolved,
                cost_so_far=prepared.cost_so_far,
                cost_this_call=cost,
            )
            logger.info(
                "ai.dispatch.structured.success",
                org_id=org_id,
                feature_key=feature_key,
                retries_used=attempt,
                ledger_id=ledger.id,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
            )
            asyncio.create_task(  # noqa: RUF006
                _touch_last_used(prepared.credential_pk_id)
            )
            return StructuredDispatchResult(
                response=StructuredResponse(
                    parsed=parsed,
                    raw_text=response.content,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                    model=response.model,
                    retries_used=attempt,
                ),
                ledger_id=ledger.id,
            )
        else:
            last_error = f"schema:{err}"
            attempt_messages = attempt_messages + [retry_message]

    # Exhausted retries. Bill the SUM of tokens consumed across all
    # attempts so the cap accounting reflects real spend.
    latency_ms = int((time.perf_counter() - start) * 1000)
    cost = estimate_cost_cents(
        model=prepared.model,
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
    )
    ledger = await _write_ledger_row(
        db,
        org_id=org_id,
        credential_id=prepared.credential_id,
        feature_key=feature_key,
        model=prepared.model,
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
        est_cost_cents_value=cost,
        latency_ms=latency_ms,
        success=False,
        error_class="STATUS_ERROR_STRUCTURED_OUTPUT",
        retries_used=STRUCTURED_OUTPUT_MAX_RETRIES,
    )
    await _post_write_soft_cap_crossing(
        db,
        org_id=org_id,
        feature_key=feature_key,
        resolved=prepared.resolved,
        cost_so_far=prepared.cost_so_far,
        cost_this_call=cost,
    )
    logger.warning(
        "ai.dispatch.structured.exhausted",
        org_id=org_id,
        feature_key=feature_key,
        retries_used=STRUCTURED_OUTPUT_MAX_RETRIES,
        last_error=last_error,
        ledger_id=ledger.id,
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
    )
    raise StructuredOutputError("STATUS_ERROR_STRUCTURED_OUTPUT")


# --- call_llm_embed --------------------------------------------------


@dataclass(frozen=True)
class EmbedDispatchResult:
    response: EmbedResponse
    ledger_id: int


async def call_llm_embed(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: str,
    texts: list[str],
    model: Optional[str] = None,
) -> EmbedDispatchResult:
    """Dispatch an embedding call through the cap + ledger chokepoint.

    The routing row's ``model`` is the default embedding model; the
    caller can override with the ``model`` kwarg if a feature surface
    pins a specific model.
    """
    # Embeddings bill on their INPUT, so feed ``texts`` to the gate as a
    # synthetic prompt payload. ``_projected_cost_cents`` then estimates
    # prompt tokens from the joined input and prices it at the embedding
    # input rate; ``max_tokens=None`` resolves the model output ceiling,
    # which is 0 for embedding models (they emit no completion tokens),
    # so projected == the embedding INPUT cost. Without this the gate saw
    # an empty payload (projected 0) and embeddings got exhausted-only
    # protection, letting a bulk batch overspend by its full cost.
    gate_messages = [{"role": "user", "content": "\n".join(texts)}]
    prepared = await _prepare_dispatch(
        db,
        org_id=org_id,
        feature_key=feature_key,
        capability="embed",
        messages=gate_messages,
        max_tokens=None,
        retry_multiplier=1,
    )
    embed_model = model or prepared.model

    start = time.perf_counter()
    try:
        response: EmbedResponse = await _with_dispatch_timeout(
            prepared.adapter.embed(texts=texts, model=embed_model)
        )
    except NativeNotAvailable:
        raise
    except NotImplementedError:
        # Anthropic path — surface as a typed dispatch error and log a
        # failure ledger row so the misconfiguration is visible.
        latency_ms = int((time.perf_counter() - start) * 1000)
        await _write_ledger_row(
            db,
            org_id=org_id,
            credential_id=prepared.credential_id,
            feature_key=feature_key,
            model=embed_model,
            prompt_tokens=0,
            completion_tokens=0,
            est_cost_cents_value=0,
            latency_ms=latency_ms,
            success=False,
            error_class="embed_not_implemented",
        )
        raise AIDispatchFailed("embed_not_implemented") from None
    except AIProviderError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        await _write_ledger_row(
            db,
            org_id=org_id,
            credential_id=prepared.credential_id,
            feature_key=feature_key,
            model=embed_model,
            prompt_tokens=0,
            completion_tokens=0,
            est_cost_cents_value=0,
            latency_ms=latency_ms,
            success=False,
            error_class=exc.code,
        )
        raise AIDispatchFailed(exc.code) from None

    latency_ms = int((time.perf_counter() - start) * 1000)
    cost = estimate_cost_cents(
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=0,
    )
    ledger = await _write_ledger_row(
        db,
        org_id=org_id,
        credential_id=prepared.credential_id,
        feature_key=feature_key,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=0,
        est_cost_cents_value=cost,
        latency_ms=latency_ms,
        success=True,
        error_class=None,
    )
    await _post_write_soft_cap_crossing(
        db,
        org_id=org_id,
        feature_key=feature_key,
        resolved=prepared.resolved,
        cost_so_far=prepared.cost_so_far,
        cost_this_call=cost,
    )
    logger.info(
        "ai.dispatch.embed.success",
        org_id=org_id,
        feature_key=feature_key,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        ledger_id=ledger.id,
    )
    asyncio.create_task(_touch_last_used(prepared.credential_pk_id))  # noqa: RUF006
    return EmbedDispatchResult(response=response, ledger_id=ledger.id)


# --- call_llm_function -----------------------------------------------


@dataclass(frozen=True)
class FunctionCallDispatchResult:
    response: FunctionCallResponse
    ledger_id: int


async def call_llm_function(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: Optional[int] = None,
) -> FunctionCallDispatchResult:
    """Dispatch a function-calling chat through the chokepoint."""
    prepared = await _prepare_dispatch(
        db,
        org_id=org_id,
        feature_key=feature_key,
        capability="function_call",
        messages=messages,
        max_tokens=max_tokens,
        retry_multiplier=1,
    )

    start = time.perf_counter()
    try:
        response: FunctionCallResponse = await _with_dispatch_timeout(
            prepared.adapter.function_call(
                model=prepared.model,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
            )
        )
    except NativeNotAvailable:
        raise
    except CapabilityNotSupported as exc:
        # Adapter-level (model-level) refusal — Ollama with a non-tool
        # model. Surface as a dispatch-level 412 so the caller can
        # reconfigure to a supported model.
        latency_ms = int((time.perf_counter() - start) * 1000)
        await _write_ledger_row(
            db,
            org_id=org_id,
            credential_id=prepared.credential_id,
            feature_key=feature_key,
            model=prepared.model,
            prompt_tokens=0,
            completion_tokens=0,
            est_cost_cents_value=0,
            latency_ms=latency_ms,
            success=False,
            error_class="capability_not_supported",
        )
        logger.info(
            "ai.dispatch.function_call.model_unsupported",
            org_id=org_id,
            feature_key=feature_key,
            model=exc.model,
        )
        raise AICapabilityNotSupported(
            capability="function_call", feature_key=feature_key
        ) from None
    except AIProviderError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        await _write_ledger_row(
            db,
            org_id=org_id,
            credential_id=prepared.credential_id,
            feature_key=feature_key,
            model=prepared.model,
            prompt_tokens=0,
            completion_tokens=0,
            est_cost_cents_value=0,
            latency_ms=latency_ms,
            success=False,
            error_class=exc.code,
        )
        raise AIDispatchFailed(exc.code) from None

    latency_ms = int((time.perf_counter() - start) * 1000)
    cost = estimate_cost_cents(
        model=prepared.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )
    ledger = await _write_ledger_row(
        db,
        org_id=org_id,
        credential_id=prepared.credential_id,
        feature_key=feature_key,
        model=prepared.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        est_cost_cents_value=cost,
        latency_ms=latency_ms,
        success=True,
        error_class=None,
    )
    await _post_write_soft_cap_crossing(
        db,
        org_id=org_id,
        feature_key=feature_key,
        resolved=prepared.resolved,
        cost_so_far=prepared.cost_so_far,
        cost_this_call=cost,
    )
    logger.info(
        "ai.dispatch.function_call.success",
        org_id=org_id,
        feature_key=feature_key,
        model=prepared.model,
        tool_calls=len(response.tool_calls),
        ledger_id=ledger.id,
    )
    asyncio.create_task(_touch_last_used(prepared.credential_pk_id))  # noqa: RUF006
    return FunctionCallDispatchResult(response=response, ledger_id=ledger.id)


# --- call_llm_stream -------------------------------------------------


async def call_llm_stream(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: str,
    messages: list[dict],
    max_tokens: Optional[int] = None,
) -> AsyncIterator[StreamChunk]:
    """Dispatch a streamed chat call.

    Yields adapter ``StreamChunk``s. Writes exactly ONE ledger row at
    end-of-stream — never per-chunk — using the final usage block
    from the provider when available, falling back to a char/4
    estimate of the accumulated delta text when not. Errors mid-stream
    wrap into ``AIDispatchFailed`` and write a single failure ledger
    row.
    """
    prepared = await _prepare_dispatch(
        db,
        org_id=org_id,
        feature_key=feature_key,
        capability="stream",
        messages=messages,
        max_tokens=max_tokens,
        retry_multiplier=1,
    )

    accumulated: list[str] = []
    final_usage: Optional[TokenUsage] = None
    start = time.perf_counter()
    try:
        async for chunk in _stream_with_dispatch_timeout(
            prepared.adapter.stream(
                model=prepared.model,
                messages=messages,
                max_tokens=max_tokens,
            )
        ):
            if chunk.done:
                final_usage = chunk.final_usage
                yield chunk
                break
            accumulated.append(chunk.delta_text)
            yield chunk
    except NativeNotAvailable:
        raise
    except AIProviderError as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        await _write_ledger_row(
            db,
            org_id=org_id,
            credential_id=prepared.credential_id,
            feature_key=feature_key,
            model=prepared.model,
            prompt_tokens=0,
            completion_tokens=0,
            est_cost_cents_value=0,
            latency_ms=latency_ms,
            success=False,
            error_class=exc.code,
        )
        raise AIDispatchFailed(exc.code) from None

    latency_ms = int((time.perf_counter() - start) * 1000)
    if final_usage is None:
        # Fallback estimate when the provider doesn't emit usage at
        # end-of-stream. Prompt tokens we can't estimate without the
        # original messages, so 0; completion tokens via char/4 of the
        # accumulated text.
        full_text = "".join(accumulated)
        final_usage = TokenUsage(
            prompt_tokens=0,
            completion_tokens=max(0, len(full_text) // 4),
        )
    cost = estimate_cost_cents(
        model=prepared.model,
        prompt_tokens=final_usage.prompt_tokens,
        completion_tokens=final_usage.completion_tokens,
    )
    ledger = await _write_ledger_row(
        db,
        org_id=org_id,
        credential_id=prepared.credential_id,
        feature_key=feature_key,
        model=prepared.model,
        prompt_tokens=final_usage.prompt_tokens,
        completion_tokens=final_usage.completion_tokens,
        est_cost_cents_value=cost,
        latency_ms=latency_ms,
        success=True,
        error_class=None,
    )
    await _post_write_soft_cap_crossing(
        db,
        org_id=org_id,
        feature_key=feature_key,
        resolved=prepared.resolved,
        cost_so_far=prepared.cost_so_far,
        cost_this_call=cost,
    )
    logger.info(
        "ai.dispatch.stream.success",
        org_id=org_id,
        feature_key=feature_key,
        model=prepared.model,
        ledger_id=ledger.id,
        prompt_tokens=final_usage.prompt_tokens,
        completion_tokens=final_usage.completion_tokens,
    )
    asyncio.create_task(_touch_last_used(prepared.credential_pk_id))  # noqa: RUF006
