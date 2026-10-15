"""Daily OpenRouter sync into ``platform_ai_models`` (TBD-631).

Platform-global like the PAT and OAuth sweeps: called once per tick from ``run_one_tick``
under the tick lease. Lands dark: nothing reads the table yet. The job only ever writes
``enabled=False``; an operator allowlist (a later ticket) is the only thing that sets it.

Prices are integer micro-USD per 1,000,000 tokens (USD/token x 1e12), computed in
``Decimal`` and rounded UP, because the ZDR figures are ceilings. Errors are logged,
never raised (the ticker must not die).
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx
from opentelemetry import trace
from sqlalchemy import func, select

from app import state_db, tracing
from app.config import settings
from app.database import async_session
from app.models.platform_ai_model import PlatformAIModel
from app.services import audit_service
from app.services.ai_providers.openai_compatible import OPENAI_COMPATIBLE_PRESETS

logger = logging.getLogger(__name__)

PRICE_CAP_MICROS = 10_000_000_000  # $10,000/Mtok; today's max is $180
EXPIRY_DISABLE_DAYS = 14
DUE_AFTER = datetime.timedelta(hours=23)  # 23 not 24, so tick jitter cannot slip a day
OFFERABLE_MAX_AGE = datetime.timedelta(hours=48)
ATTEMPT_LEASE = "platform_ai_catalog:attempt"
ATTEMPT_LEASE_TTL = 3600
FETCH_DEADLINE_SECONDS = 90
_ONE_MTOK = Decimal(10) ** 12
_DATA_COLUMNS = (
    "name", "context_length", "max_completion_tokens", "supports_structured_outputs",
    "headline_prompt_micros_per_mtok", "headline_completion_micros_per_mtok",
    "zdr_prompt_micros_per_mtok", "zdr_completion_micros_per_mtok", "expires_at",
)


class CatalogFeedError(Exception):
    """A feed that is not safe to apply (wrong status, empty, partial)."""


def parse_price(value) -> int | None:
    """USD per token (decimal string) -> micro-USD per Mtok, rounded up; None if unusable."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 0:  # is_finite before ceil: ceil(NaN/Inf) raises
        return None
    micros = math.ceil(d * _ONE_MTOK)
    return micros if micros <= PRICE_CAP_MICROS else None


def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _endpoint_ceiling(pricing: dict, field_name: str) -> int | None:
    """Max of the base and every override price, grossed up for the endpoint's discount."""
    discount = pricing.get("discount")
    if discount is None:
        discount = 0
    if isinstance(discount, bool) or not isinstance(discount, (int, float)):
        return None
    dd = Decimal(str(discount))
    if not dd.is_finite() or not (0 <= dd < 1):
        return None
    candidates = [pricing.get(field_name)]
    for o in pricing.get("overrides") or []:
        if isinstance(o, dict) and field_name in o:
            candidates.append(o[field_name])
    parsed = [parse_price(c) for c in candidates]
    if any(p is None for p in parsed):
        return None
    gross = math.ceil(Decimal(max(parsed)) / (1 - dd))
    return gross if gross <= PRICE_CAP_MICROS else None  # cap AFTER the gross-up


def _zdr_facts(endpoints: list[dict], fallback_ctx: int) -> dict:
    tools = [e for e in endpoints if "tools" in (e.get("supported_parameters") or [])]
    facts = {
        "max_completion_tokens": None, "context_length": fallback_ctx,
        "supports_structured_outputs": False,
        "zdr_prompt_micros_per_mtok": None, "zdr_completion_micros_per_mtok": None,
    }
    if not tools:
        return facts
    mcts = [e["max_completion_tokens"] for e in tools if _pos_int(e.get("max_completion_tokens"))]
    ctxs = [e["context_length"] for e in tools if _pos_int(e.get("context_length"))]
    facts["max_completion_tokens"] = min(mcts) if mcts else None
    facts["context_length"] = min(ctxs) if ctxs else fallback_ctx
    facts["supports_structured_outputs"] = any("structured_outputs" in e["supported_parameters"] for e in tools)
    for col, f in (("zdr_prompt_micros_per_mtok", "prompt"), ("zdr_completion_micros_per_mtok", "completion")):
        per_ep = [_endpoint_ceiling(e.get("pricing") or {}, f) for e in tools]
        facts[col] = None if any(c is None for c in per_ep) else max(per_ep)
    if facts["zdr_prompt_micros_per_mtok"] is None or facts["zdr_completion_micros_per_mtok"] is None:
        facts["zdr_prompt_micros_per_mtok"] = facts["zdr_completion_micros_per_mtok"] = None
    return facts


def _expiry(raw, today: datetime.date) -> datetime.date | None:
    if raw in (None, ""):
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        return today  # unparseable: fail closed (offerable() sees it as expiring)


def _parse_models(models_feed: dict, zdr_feed: dict, today: datetime.date) -> dict[str, dict]:
    zdr: dict[str, list[dict]] = {}
    for e in zdr_feed.get("data") or []:
        if isinstance(e, dict) and isinstance(e.get("model_id"), str):
            zdr.setdefault(e["model_id"], []).append(e)
    out: dict[str, dict] = {}
    for m in models_feed.get("data") or []:
        mid = m.get("id") if isinstance(m, dict) else None
        name = m.get("name") if isinstance(m, dict) else None
        if not (isinstance(mid, str) and mid.isascii() and 1 <= len(mid) <= 128
                and isinstance(name, str) and 1 <= len(name) <= 255
                and _pos_int(m.get("context_length"))):
            logger.warning("platform_ai_catalog.item_skipped", extra={"item_id": str(mid)[:128]})
            continue
        pricing = m.get("pricing") or {}
        out[mid] = {
            "name": name,
            "headline_prompt_micros_per_mtok": parse_price(pricing.get("prompt")),
            "headline_completion_micros_per_mtok": parse_price(pricing.get("completion")),
            "expires_at": _expiry(m.get("expiration_date"), today),
            **_zdr_facts(zdr.get(mid, []), m["context_length"]),
        }
    return out


@dataclass
class SyncPlan:
    upserts: dict[str, dict] = field(default_factory=dict)
    disables: list[tuple[str, str]] = field(default_factory=list)
    ceiling_drops: list[dict] = field(default_factory=list)
    refused: bool = False
    would_disable: list[str] = field(default_factory=list)
    enabled_count: int = 0


def _expiring(expires_at, today: datetime.date) -> bool:
    return expires_at is not None and expires_at <= today + datetime.timedelta(days=EXPIRY_DISABLE_DAYS)


def plan_sync(existing: dict[str, PlatformAIModel], models_feed: dict, zdr_feed: dict,
              today: datetime.date) -> SyncPlan:
    """Pure: what the feeds change. ``enabled`` is never set True, only disabled."""
    plan = SyncPlan(upserts=_parse_models(models_feed, zdr_feed, today))
    for mid, row in existing.items():
        if not row.enabled:
            continue
        plan.enabled_count += 1
        new = plan.upserts.get(mid)
        if new is None:
            plan.disables.append((mid, "vanished"))
        elif new["zdr_prompt_micros_per_mtok"] is None:
            plan.disables.append((mid, "no_zdr"))
        elif _expiring(new["expires_at"], today):
            plan.disables.append((mid, "expiring"))
        else:
            old = (row.zdr_prompt_micros_per_mtok, row.zdr_completion_micros_per_mtok)
            now_ = (new["zdr_prompt_micros_per_mtok"], new["zdr_completion_micros_per_mtok"])
            if None not in old and (now_[0] < old[0] or now_[1] < old[1]):
                plan.ceiling_drops.append({
                    "openrouter_id": mid, "old": old, "new": now_,
                    "context_length": (row.context_length, new["context_length"]),
                    "max_completion_tokens": (row.max_completion_tokens, new["max_completion_tokens"]),
                })
    plan.would_disable = [m for m, r in plan.disables if r in ("vanished", "no_zdr")]
    # Expiry is a positive fact from the feed; only absence signals count toward refusal.
    plan.refused = plan.enabled_count >= 2 and 2 * len(plan.would_disable) > plan.enabled_count
    return plan


def offerable(row: PlatformAIModel, now: datetime.datetime) -> bool:
    """No caller until the switch-over. ``now`` is naive UTC."""
    return bool(
        row.enabled
        and row.zdr_prompt_micros_per_mtok is not None
        and row.zdr_completion_micros_per_mtok is not None
        and row.max_completion_tokens is not None
        and not _expiring(row.expires_at, now.date())
        and row.last_seen_at >= now - OFFERABLE_MAX_AGE
    )


async def fetch_feeds(*, transport: httpx.AsyncBaseTransport | None = None) -> tuple[dict, dict]:
    """Both feeds, public (no Authorization: the house key never leaves for a catalog read)."""
    root = next(p["base_url"] for p in OPENAI_COMPATIBLE_PRESETS if p["key"] == "openrouter")

    async def get(client: httpx.AsyncClient, path: str) -> dict:
        r = await client.get(f"{root}{path}")
        if r.status_code != 200:
            raise CatalogFeedError(f"status {r.status_code}")
        body = r.json()
        if not isinstance(body, dict) or not isinstance(body.get("data"), list) or not body["data"]:
            raise CatalogFeedError("empty or malformed data")
        return body

    async with asyncio.timeout(FETCH_DEADLINE_SECONDS):  # httpx's timeout is per phase
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            models = await get(client, "/models?supported_parameters=tools")
            # shortcut: refuses a paginated feed, follow links.next once OpenRouter paginates
            if models.get("total_count") != len(models["data"]) or (models.get("links") or {}).get("next"):
                raise CatalogFeedError("partial models page")
            zdr = await get(client, "/endpoints/zdr")
    return models, zdr


async def _audit(session_factory, event_type: str, outcome: str, detail: dict) -> None:
    await audit_service.record_audit_event(
        session_factory, event_type=event_type, actor_user_id=None, actor_email="system",
        target_org_id=None, target_org_name=None, request_id=None, ip_address=None,
        outcome=outcome, detail=detail,
    )


async def run_platform_ai_catalog_sync(
    session_factory=async_session, *, fetch=fetch_feeds, now: datetime.datetime | None = None
) -> str:
    """One sync attempt; returns an outcome word and never raises."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is not None:  # columns are naive UTC
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    try:
        if not settings.ai_native_enabled:
            return "off"
        async with session_factory() as db:
            last = (await db.execute(select(func.max(PlatformAIModel.last_seen_at)))).scalar()
        if last is not None and now - last < DUE_AFTER:
            return "fresh"
        if await state_db.acquire_lease(ATTEMPT_LEASE, ATTEMPT_LEASE_TTL) is None:
            return "backoff"
        try:
            models_feed, zdr_feed = await fetch()
        except Exception as exc:  # noqa: BLE001
            logger.error("platform_ai_catalog.fetch_failed", extra={
                "error": type(exc).__name__,
                "hours_since_success": None if last is None else round((now - last).total_seconds() / 3600, 1),
            })
            return "fetch_failed"
        async with session_factory() as db:
            rows = {r.openrouter_id: r for r in (await db.execute(select(PlatformAIModel))).scalars()}
            plan = plan_sync(rows, models_feed, zdr_feed, now.date())
            if plan.refused:
                refused = True
            else:
                refused = False
                # Attribute updates on loaded rows, never merge(): that would overwrite ``enabled``.
                for mid, data in plan.upserts.items():
                    if mid in rows:
                        for k, v in data.items():
                            setattr(rows[mid], k, v)
                        rows[mid].last_seen_at = now
                    else:
                        db.add(PlatformAIModel(openrouter_id=mid, enabled=False, last_seen_at=now, **data))
                for mid, _ in plan.disables:
                    rows[mid].enabled = False
                await db.commit()
        if refused:
            logger.error("platform_ai_catalog.sync_refused", extra={
                "would_disable": len(plan.would_disable), "enabled_count": plan.enabled_count})
            await _audit(session_factory, "platform_ai.catalog.sync_refused", "failure",
                         {"would_disable": plan.would_disable, "enabled_count": plan.enabled_count})
            return "refused"
        for mid, reason in plan.disables:
            logger.warning("platform_ai_catalog.model_disabled", extra={"openrouter_id": mid, "reason": reason})
            await _audit(session_factory, "platform_ai.catalog.model_disabled", "success",
                         {"openrouter_id": mid, "reason": reason})
        for d in plan.ceiling_drops:
            logger.warning("platform_ai_catalog.ceiling_dropped", extra={"openrouter_id": d["openrouter_id"]})
            await _audit(session_factory, "platform_ai.catalog.ceiling_dropped", "success",
                         {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()})
        return "synced"
    except Exception as exc:  # noqa: BLE001 -- never let the ticker die
        # Class only: str(exc) can quote statements and bound values.
        tracing.mark_error(trace.get_current_span(), exc)
        logger.error("platform_ai_catalog.failed", extra={"error": type(exc).__name__})
        return "error"
