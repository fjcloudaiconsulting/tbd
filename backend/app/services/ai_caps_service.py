"""Per-org AI cap service (PR1 follow-up).

CRUD only — no cap-check enforcement here (that lives in PR2's
``call_llm`` chokepoint alongside the ``ai_usage`` ledger). Same
feature-name closed set as routing so caps and routing stay aligned.

Caps tables don't reference credentials, so no composite-FK pattern;
same-org integrity is structural via the per-table PK on ``org_id``.
"""
from __future__ import annotations

from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.org_ai_caps import OrgAIDefaultCaps, OrgAIFeatureCaps
from app.models.org_ai_routing import ROUTABLE_FEATURE_NAMES
from app.services import audit_service
from app.services.ai_routing_service import UnknownFeatureName


logger = structlog.stdlib.get_logger()


def assert_known_feature(feature_key: str) -> None:
    if feature_key not in ROUTABLE_FEATURE_NAMES:
        raise UnknownFeatureName(feature_key)


async def get_default_caps(
    db: AsyncSession, *, org_id: int
) -> Optional[OrgAIDefaultCaps]:
    res = await db.execute(
        select(OrgAIDefaultCaps).where(OrgAIDefaultCaps.org_id == org_id)
    )
    return res.scalar_one_or_none()


async def get_feature_caps(
    db: AsyncSession, *, org_id: int
) -> list[OrgAIFeatureCaps]:
    res = await db.execute(
        select(OrgAIFeatureCaps)
        .where(OrgAIFeatureCaps.org_id == org_id)
        .order_by(OrgAIFeatureCaps.feature_key)
    )
    return list(res.scalars().all())


async def set_default_caps(
    db: AsyncSession,
    *,
    org_id: int,
    soft_cap_cents: Optional[int],
    hard_cap_cents: Optional[int],
    period: str,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> OrgAIDefaultCaps:
    existing = await get_default_caps(db, org_id=org_id)
    if existing is None:
        row = OrgAIDefaultCaps(
            org_id=org_id,
            soft_cap_cents=soft_cap_cents,
            hard_cap_cents=hard_cap_cents,
            period=period,
        )
        db.add(row)
    else:
        existing.soft_cap_cents = soft_cap_cents
        existing.hard_cap_cents = hard_cap_cents
        existing.period = period
        row = existing
    await db.commit()
    await db.refresh(row)
    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.caps.default.set",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={
            "soft_cap_cents": soft_cap_cents,
            "hard_cap_cents": hard_cap_cents,
            "period": period,
        },
    )
    return row


async def set_feature_caps(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: str,
    soft_cap_cents: Optional[int],
    hard_cap_cents: Optional[int],
    period: str,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> OrgAIFeatureCaps:
    assert_known_feature(feature_key)
    res = await db.execute(
        select(OrgAIFeatureCaps).where(
            OrgAIFeatureCaps.org_id == org_id,
            OrgAIFeatureCaps.feature_key == feature_key,
        )
    )
    existing = res.scalar_one_or_none()
    if existing is None:
        row = OrgAIFeatureCaps(
            org_id=org_id,
            feature_key=feature_key,
            soft_cap_cents=soft_cap_cents,
            hard_cap_cents=hard_cap_cents,
            period=period,
        )
        db.add(row)
    else:
        existing.soft_cap_cents = soft_cap_cents
        existing.hard_cap_cents = hard_cap_cents
        existing.period = period
        row = existing
    await db.commit()
    await db.refresh(row)
    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.caps.feature.set",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={
            "feature_key": feature_key,
            "soft_cap_cents": soft_cap_cents,
            "hard_cap_cents": hard_cap_cents,
            "period": period,
        },
    )
    return row


async def delete_feature_caps(
    db: AsyncSession,
    *,
    org_id: int,
    feature_key: str,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> bool:
    res = await db.execute(
        select(OrgAIFeatureCaps).where(
            OrgAIFeatureCaps.org_id == org_id,
            OrgAIFeatureCaps.feature_key == feature_key,
        )
    )
    row = res.scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    await audit_service.record_audit_event(
        session_factory,
        event_type="ai.caps.feature.deleted",
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={"feature_key": feature_key},
    )
    return True
