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

from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.org_ai_credential import OrgAICredential
from app.models.org_ai_routing import (
    ROUTABLE_FEATURE_NAMES,
    OrgAIDefaultRouting,
    OrgAIFeatureRouting,
)
from app.services import audit_service


logger = structlog.stdlib.get_logger()


class CrossOrgRoutingDenied(Exception):
    """Service-layer refusal — credential belongs to a different org.

    The DB composite FK is the catch-all; this one fires earlier and
    surfaces a clear error message rather than a raw FK constraint
    string. See spec §4 (belt-and-suspenders).
    """


class UnknownFeatureName(Exception):
    """Refuse routing writes for feature_names outside the closed set."""


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
    await _assert_credential_in_org(
        db, org_id=org_id, credential_id=credential_id
    )

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
    await _assert_credential_in_org(
        db, org_id=org_id, credential_id=credential_id
    )

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
