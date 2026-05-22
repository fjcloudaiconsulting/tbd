"""Per-org AI consent service (PR1).

Append-only. The latest row by ``consented_at`` is the current state.
``get_current_consents`` returns the effective snapshot — all-false
when no row exists, or when the latest row is revoked.

PR1 ships create + read; PR4 wires the refusal logic into the native
adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.org_ai_consent import OrgAIConsent
from app.services import audit_service


logger = structlog.stdlib.get_logger()


@dataclass(frozen=True)
class ConsentSnapshot:
    allow_training: bool
    allow_rag: bool
    allow_telemetry: bool
    consent_version: Optional[str]
    consented_by_user_id: Optional[int]
    consented_at: Optional[datetime]
    has_consent: bool


_NO_CONSENT = ConsentSnapshot(
    allow_training=False,
    allow_rag=False,
    allow_telemetry=False,
    consent_version=None,
    consented_by_user_id=None,
    consented_at=None,
    has_consent=False,
)


async def get_latest_consent_row(
    db: AsyncSession, *, org_id: int
) -> Optional[OrgAIConsent]:
    res = await db.execute(
        select(OrgAIConsent)
        .where(OrgAIConsent.org_id == org_id)
        .order_by(desc(OrgAIConsent.consented_at), desc(OrgAIConsent.id))
        .limit(1)
    )
    return res.scalar_one_or_none()


async def get_current_consents(
    db: AsyncSession, *, org_id: int
) -> ConsentSnapshot:
    row = await get_latest_consent_row(db, org_id=org_id)
    if row is None:
        return _NO_CONSENT
    if row.revoked_at is not None:
        # Revoked => effective state is all-false even if the row's
        # allow_* booleans were True at the time it was written. The
        # revocation row is what counts now.
        return ConsentSnapshot(
            allow_training=False,
            allow_rag=False,
            allow_telemetry=False,
            consent_version=row.consent_version,
            consented_by_user_id=row.consented_by_user_id,
            consented_at=row.consented_at,
            has_consent=False,
        )
    return ConsentSnapshot(
        allow_training=row.allow_training,
        allow_rag=row.allow_rag,
        allow_telemetry=row.allow_telemetry,
        consent_version=row.consent_version,
        consented_by_user_id=row.consented_by_user_id,
        consented_at=row.consented_at,
        has_consent=True,
    )


async def write_consent_row(
    db: AsyncSession,
    *,
    org_id: int,
    consent_version: str,
    allow_training: bool,
    allow_rag: bool,
    allow_telemetry: bool,
    revoked: bool,
    consented_by_user_id: int,
    session_factory: async_sessionmaker[AsyncSession],
    actor_user_id: int,
    actor_email: str,
    request_id: Optional[str],
    ip_address: Optional[str],
) -> OrgAIConsent:
    # Pin the ToS version. POSTs MUST carry the current pinned
    # consent_version; any mismatch (older or newer) is rejected so a
    # stale browser tab can't replay an old consent payload after a
    # ToS bump, and a forged-future-version payload can't pre-accept
    # a ToS that hasn't shipped yet. Spec §3.5 (T-ToS).
    current = settings.ai_native_current_consent_version
    if consent_version != current:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "consent_version_outdated",
                "message": (
                    f"Consent version is outdated. Current is {current}."
                ),
                "current_consent_version": current,
            },
        )
    now = datetime.now(timezone.utc)
    row = OrgAIConsent(
        org_id=org_id,
        consent_version=consent_version,
        allow_training=False if revoked else allow_training,
        allow_rag=False if revoked else allow_rag,
        allow_telemetry=False if revoked else allow_telemetry,
        consented_by_user_id=consented_by_user_id,
        revoked_at=now if revoked else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)

    await audit_service.record_audit_event(
        session_factory,
        event_type=(
            "ai.consent.revoked" if revoked else "ai.consent.granted"
        ),
        actor_user_id=actor_user_id,
        actor_email=actor_email,
        target_org_id=org_id,
        target_org_name=None,
        request_id=request_id,
        ip_address=ip_address,
        outcome="success",
        detail={
            "consent_version": consent_version,
            "allow_training": row.allow_training,
            "allow_rag": row.allow_rag,
            "allow_telemetry": row.allow_telemetry,
            "revoked": revoked,
        },
    )
    return row
