"""Daily OpenRouter sync into ``platform_ai_models`` (TBD-631).

Platform-global like the PAT and OAuth sweeps: called once per tick from ``run_one_tick``
under the tick lease. The job only ever writes ``enabled=False``; the superadmin PUT
``/api/v1/admin/platform-ai`` is the only writer of ``True`` (TBD-586).

Prices are integer micro-USD per 1,000,000 tokens (USD/token x 1e12), computed in
``Decimal`` and rounded UP, because the ZDR figures are ceilings. Errors are logged,
never raised (the ticker must not die).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import httpx
from opentelemetry import metrics, trace
from sqlalchemy import func, select

from app import state_db, tracing
from app.config import settings
from app.database import async_session
from app.models.platform_ai_model import PlatformAIModel
from app.services import audit_service
from app.services.ai_providers.openai_compatible import OPENAI_COMPATIBLE_PRESETS

logger = logging.getLogger(__name__)
# TBD-633: the platform AI alarms, as a metric because TBD logs do not reach Grafana. Attribute
# ``event`` is the log event's last segment (spend watch shares this counter); never org_id.
PLATFORM_AI_EVENTS = metrics.get_meter(__name__).create_counter("tbd.platform_ai.events")

PRICE_CAP_MICROS = 10_000_000_000  # $10,000/Mtok; today's max is $180
EXPIRY_DISABLE_DAYS = 14
DUE_AFTER = datetime.timedelta(hours=23)  # 23 not 24, so tick jitter cannot slip a day
OFFERABLE_MAX_AGE = datetime.timedelta(hours=48)
ATTEMPT_LEASE = "platform_ai_catalog:attempt"
ATTEMPT_LEASE_TTL = 3600
FETCH_DEADLINE_SECONDS = 90
_ONE_MTOK = Decimal(10) ** 12
MAX_BODY_BYTES = 5_000_000
INT_MAX = 2**31 - 1  # MySQL INT
OPENROUTER_ROOT = next(p["base_url"] for p in OPENAI_COMPATIBLE_PRESETS if p["key"] == "openrouter")
# Never released: the lease TTL is the 1h retry backoff after a failed or refused run.


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
    # Bound BEFORE scaling: "1e999999" overflows Decimal and huge exponents build giant ints.
    if not d.is_finite() or d < 0 or d > Decimal(PRICE_CAP_MICROS) / _ONE_MTOK:
        return None
    micros = math.ceil(d * _ONE_MTOK)
    return micros if micros <= PRICE_CAP_MICROS else None


def _encodable(s: str) -> bool:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogate
        return False
    return True


def _pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 < v <= INT_MAX


def _endpoint_ceiling(pricing: dict, field_name: str) -> int | None:
    """Max of the base and every override price, grossed up for the endpoint's discount.
    The completion ceiling also folds ``internal_reasoning`` (reasoning tokens bill there)."""
    if not isinstance(pricing, dict):
        return None
    discount = pricing.get("discount")
    if discount is None:
        discount = 0
    if isinstance(discount, bool) or not isinstance(discount, (int, float)):
        return None
    dd = Decimal(str(discount))
    if not dd.is_finite() or not (0 <= dd < 1):
        return None
    folded = ("internal_reasoning",) if field_name == "completion" else ()
    candidates = [pricing.get(field_name)] + [pricing[f] for f in folded if f in pricing]
    overrides = pricing.get("overrides") or []
    if not isinstance(overrides, list) or not all(isinstance(o, dict) for o in overrides):
        return None  # a shape we cannot read must not fall back to the (cheaper) base price
    candidates += [o[f] for o in overrides for f in (field_name, *folded) if f in o]
    parsed = [parse_price(c) for c in candidates]
    if any(p is None for p in parsed):
        return None
    gross = math.ceil(Decimal(max(parsed)) / (1 - dd))
    return gross if gross <= PRICE_CAP_MICROS else None  # cap AFTER the gross-up


def _request_fee(pricing) -> bool:
    return isinstance(pricing, dict) and "request" in pricing and parse_price(pricing["request"]) != 0


def _zdr_facts(endpoints: list[dict], fallback_ctx: int) -> dict:
    # An endpoint with a per-request fee cannot be routed under ``max_price.request = 0``.
    tools = [e for e in endpoints if isinstance(e.get("supported_parameters"), list)
             and "tools" in e["supported_parameters"] and not _request_fee(e.get("pricing"))]
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
        per_ep = [_endpoint_ceiling(e.get("pricing"), f) for e in tools]
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
        pricing = m.get("pricing") if isinstance(m, dict) else None
        if not (isinstance(mid, str) and 1 <= len(mid) <= 128 and all(" " <= c <= "~" for c in mid)
                and isinstance(name, str) and 1 <= len(name) <= 255 and _encodable(name)
                and isinstance(pricing, dict) and _pos_int(m.get("context_length"))):
            logger.warning("platform_ai_catalog.item_skipped", extra={"item_id": str(mid)[:128]})
            continue
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


def eligible(row: PlatformAIModel, now: datetime.datetime) -> bool:
    """Every ``offerable`` condition except ``enabled``. ``now`` is naive UTC."""
    return bool(
        row.zdr_prompt_micros_per_mtok is not None
        and row.zdr_completion_micros_per_mtok is not None
        and row.max_completion_tokens is not None
        and not _expiring(row.expires_at, now.date())
        and row.last_seen_at >= now - OFFERABLE_MAX_AGE
    )


def offerable(row: PlatformAIModel, now: datetime.datetime) -> bool:
    """``enabled`` (written True only by the superadmin PUT) and ``eligible``."""
    return bool(row.enabled and eligible(row, now))


async def fetch_feeds(*, transport: httpx.AsyncBaseTransport | None = None) -> tuple[dict, dict]:
    """Both feeds, public (no Authorization: the house key never leaves for a catalog read)."""
    async def get(client: httpx.AsyncClient, path: str) -> dict:
        async with client.stream("GET", f"{OPENROUTER_ROOT}{path}") as r:
            if r.status_code != 200:
                raise CatalogFeedError(f"status {r.status_code}")
            raw = b""
            async for chunk in r.aiter_bytes():
                raw += chunk
                if len(raw) > MAX_BODY_BYTES:
                    raise CatalogFeedError("body too large")
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list) or not body["data"]:
            raise CatalogFeedError("empty or malformed data")
        return body

    async with asyncio.timeout(FETCH_DEADLINE_SECONDS):  # httpx's timeout is per phase
        async with httpx.AsyncClient(timeout=30.0, transport=transport, trust_env=False) as client:
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
            refused = plan.refused
            if not refused:
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
            PLATFORM_AI_EVENTS.add(1, {"event": "model_disabled"})
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
