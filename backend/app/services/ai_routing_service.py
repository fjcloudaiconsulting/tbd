"""Per-org AI routing service (PR1).

Both defense layers run here:

1. **Service-layer cross-org check** — pre-fetch the credential and
   verify ``credential.org_id == org_id`` before commit. Raises
   ``CrossOrgRoutingDenied`` with a user-friendly message.
2. **DB composite FK** — the migration's
   ``FOREIGN KEY (org_id, credential_id) REFERENCES org_ai_credentials
   (org_id, id)`` is the safety net for ORM bugs, direct DB writes, or
   any service path that bypasses (1).

PR1 ships routing WRITES even though no feature surface dispatches
through it yet — that lets the architect-locked rollout (spec §13)
land routing UI + CRUD in PR1, with the chat/embed dispatch arriving
in PR3.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.org_ai_credential import OrgAICredential, is_supported_provider
from app.models.org_ai_routing import (
    ROUTABLE_FEATURE_NAMES,
    OrgAIDefaultRouting,
    OrgAIFeatureRouting,
)
from app.schemas.org_ai_routing import MODEL_NAME_MAX_LENGTH
from app.services import audit_service, feature_service, platform_ai, platform_ai_settings
from app.services.ai_feature_map import CHAT_FEATURE, FEATURE_CAPABILITY
from app.services.ai_pricing import MICROS_PER_CENT, MODEL_PRICING, cost_tier


logger = logging.getLogger(__name__)


class CrossOrgRoutingDenied(Exception):
    """Service-layer refusal — credential belongs to a different org.

    The DB composite FK is the catch-all; this one fires earlier and
    surfaces a clear error message rather than a raw FK constraint
    string. See spec §4 (belt-and-suspenders).
    """


class UnknownFeatureName(Exception):
    """Refuse routing writes for feature_names outside the closed set."""


class ModelListUnavailable(Exception):
    """The plan/catalog read behind the model list failed (not the routing write)."""


class ModelNotListed(Exception):
    """Routing to a model ``list_models`` does not list for that credential and feature."""


# Substrings of ids that cannot serve chat or structured output (all routable features).
_NON_CHAT = ("embed", "tts", "whisper", "dall-e", "image", "moderation", "realtime", "audio", "transcribe",
             "davinci-002", "babbage-002", "-instruct", "sora-")


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str | None
    hint: str | None
    tier: str | None  # "budget" | "standard" | "frontier"; None = cost unknown
    prompt_micros_per_mtok: int | None
    completion_micros_per_mtok: int | None


@dataclass(frozen=True)
class ModelList:
    options: list[ModelOption]
    default: str | None


async def list_models(db: AsyncSession, cred: OrgAICredential, feature: str | None) -> ModelList:
    """The ONE definition of which models ``cred`` can be routed to for ``feature``
    (None = the default routing row). The caller has validated ``feature``."""
    if cred.platform_provider is None:
        # TBD-584: a retired provider (ollama, native) lists nothing, so no
        # routing can be written to it; dispatch refuses it too.
        if not is_supported_provider(cred.provider):
            return ModelList([], None)
        return _own_key_models(cred, feature)
    conf = await platform_ai_settings.load(db)
    if not platform_ai_settings.switched_on(conf, cred.platform_provider):
        return ModelList([], None)
    frontier = (await feature_service.get_entitlements(db, cred.org_id)).frontier_models
    cap = FEATURE_CAPABILITY[feature] if feature else "function_call"
    rows = await platform_ai.candidates(db, cap, frontier=frontier)
    default = platform_ai.default_row(rows)
    if feature not in (None, CHAT_FEATURE):
        rows = [default] if default else []  # dispatch always runs the default for background features
    options = []
    for r in rows:
        priced = MODEL_PRICING.get(r.openrouter_id)
        options.append(ModelOption(
            id=r.openrouter_id, label=priced.label if priced and priced.label else r.name,
            hint=priced.hint if priced else None, tier=platform_ai.tier(r),
            prompt_micros_per_mtok=r.zdr_prompt_micros_per_mtok,
            completion_micros_per_mtok=r.zdr_completion_micros_per_mtok))
    return ModelList(options, default.openrouter_id if default else None)


def _own_key_models(cred: OrgAICredential, feature: str | None) -> ModelList:
    if feature is not None and FEATURE_CAPABILITY[feature] not in (cred.discovered_capabilities or []):
        return ModelList([], None)  # mirrors the dispatch refusal
    ids = dict.fromkeys(i for i in (cred.discovered_models or []) if isinstance(i, str))
    priced: list[tuple[int, str, ModelOption]] = []
    unpriced: list[ModelOption] = []
    for i in ids:
        if (not i or len(i) > MODEL_NAME_MAX_LENGTH or i == "_default"
                or any(m in i.lower() for m in _NON_CHAT)):
            continue
        row = MODEL_PRICING.get(i)  # never get_pricing(): its `_default` row would tier every unknown id "frontier"
        if row is None:
            unpriced.append(ModelOption(i, None, None, None, None, None))
            continue
        p, c = row.prompt_per_1m_cents * MICROS_PER_CENT, row.completion_per_1m_cents * MICROS_PER_CENT
        priced.append((3 * p + c, i, ModelOption(i, row.label, row.hint, cost_tier(p, c), p, c)))
    priced.sort(key=lambda t: t[:2])
    default = next((o.id for b, _, o in priced if b > 0), None)
    return ModelList([o for _, _, o in priced] + sorted(unpriced, key=lambda o: o.id), default)


async def _assert_model_listed(
    db: AsyncSession, cred: OrgAICredential, feature: str | None, model: str
) -> None:
    try:
        listed = await list_models(db, cred, feature)
    except (SQLAlchemyError, PydanticValidationError):
        await db.rollback()
        raise ModelListUnavailable from None
    if model not in {o.id for o in listed.options}:
        raise ModelNotListed(model)


def assert_known_feature(feature_name: str) -> None:
    if feature_name not in ROUTABLE_FEATURE_NAMES:
        raise UnknownFeatureName(feature_name)


async def _assert_credential_in_org(
    db: AsyncSession, *, org_id: int, credential_id: int
) -> OrgAICredential:
    row = await db.execute(
        select(OrgAICredential).where(OrgAICredential.id == credential_id)
    )
    cred = row.scalar_one_or_none()
    if cred is None or cred.org_id != org_id:
        # Don't reveal whether the credential ID exists in a different
        # org — flatten to the same denial.
        raise CrossOrgRoutingDenied(
            "credential does not belong to this organization"
        )
    return cred


async def get_default_routing(
    db: AsyncSession, *, org_id: int
) -> Optional[OrgAIDefaultRouting]:
    res = await db.execute(
        select(OrgAIDefaultRouting).where(OrgAIDefaultRouting.org_id == org_id)
    )
    return res.scalar_one_or_none()


async def get_feature_routings(
    db: AsyncSession, *, org_id: int
) -> list[OrgAIFeatureRouting]:
    res = await db.execute(
        select(OrgAIFeatureRouting)
        .where(OrgAIFeatureRouting.org_id == org_id)
        .order_by(OrgAIFeatureRouting.feature_name)
    )
    return list(res.scalars().all())


async def set_default_routing(
    db: AsyncSession,
    *,
    org_id: int,
    credential_id: int,
    model: str,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> OrgAIDefaultRouting:
    cred = await _assert_credential_in_org(
        db, org_id=org_id, credential_id=credential_id
    )
    await _assert_model_listed(db, cred, None, model)

    existing = await get_default_routing(db, org_id=org_id)
    if existing is None:
        row = OrgAIDefaultRouting(
            org_id=org_id, credential_id=credential_id, model=model
        )
        db.add(row)
    else:
        existing.credential_id = credential_id
        existing.model = model
        row = existing
    await db.commit()
    await db.refresh(row)

    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.routing.default.set",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={"credential_id": credential_id, "model": model},
    )
    return row


async def set_feature_routing(
    db: AsyncSession,
    *,
    org_id: int,
    feature_name: str,
    credential_id: int,
    model: str,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> OrgAIFeatureRouting:
    assert_known_feature(feature_name)
    cred = await _assert_credential_in_org(
        db, org_id=org_id, credential_id=credential_id
    )
    await _assert_model_listed(db, cred, feature_name, model)

    res = await db.execute(
        select(OrgAIFeatureRouting).where(
            OrgAIFeatureRouting.org_id == org_id,
            OrgAIFeatureRouting.feature_name == feature_name,
        )
    )
    existing = res.scalar_one_or_none()
    if existing is None:
        row = OrgAIFeatureRouting(
            org_id=org_id,
            feature_name=feature_name,
            credential_id=credential_id,
            model=model,
        )
        db.add(row)
    else:
        existing.credential_id = credential_id
        existing.model = model
        row = existing
    await db.commit()
    await db.refresh(row)

    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.routing.feature.set",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={
            "feature_name": feature_name,
            "credential_id": credential_id,
            "model": model,
        },
    )
    return row


async def delete_feature_routing(
    db: AsyncSession,
    *,
    org_id: int,
    feature_name: str,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> bool:
    res = await db.execute(
        select(OrgAIFeatureRouting).where(
            OrgAIFeatureRouting.org_id == org_id,
            OrgAIFeatureRouting.feature_name == feature_name,
        )
    )
    row = res.scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.routing.feature.deleted",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={"feature_name": feature_name},
    )
    return True


async def get_routing_for_feature(
    db: AsyncSession, *, org_id: int, feature_name: str
) -> Optional[tuple[int, str]]:
    """Resolve dispatch routing per spec §4 step order.

    Returns ``(credential_id, model)`` or None. Feature override beats
    default; no row anywhere -> None (caller maps to ``NoProviderConfigured``).
    """
    feat = await db.execute(
        select(OrgAIFeatureRouting).where(
            OrgAIFeatureRouting.org_id == org_id,
            OrgAIFeatureRouting.feature_name == feature_name,
        )
    )
    feat_row = feat.scalar_one_or_none()
    if feat_row is not None:
        return (feat_row.credential_id, feat_row.model)
    default = await get_default_routing(db, org_id=org_id)
    if default is not None:
        return (default.credential_id, default.model)
    return None
