"""TBD-578: the 5-live agent token cap under MySQL REPEATABLE READ.

Skipped unless ``PFV_RUN_MYSQL_TESTS=1`` (needs a migrated MySQL): SQLite has
no snapshot isolation, so it cannot show the race.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.api_token import ApiToken
from app.models.feature_override import OrgFeatureOverride
from app.models.user import Organization, Role, User
from app.security import token_cutoff
from app.services import api_token_service as svc
from app.services.api_token_service import hash_api_token

pytestmark = pytest.mark.skipif(
    os.environ.get("PFV_RUN_MYSQL_TESTS") != "1",
    reason="MySQL-only concurrency test; set PFV_RUN_MYSQL_TESTS=1 to run.",
)


async def test_parallel_mints_cannot_pass_the_cap_on_a_stale_snapshot():
    """FENCE (sign-off fold 1). Both sessions read the user first, which
    fixes their snapshot at 4 live tokens, then mint concurrently. Wrong
    implementation: a plain (non-locking) count after the owner-row lock;
    the second mint waits for the lock but still counts 4 and mints a 6th."""
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tag = secrets.token_hex(4)
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    async with factory() as s:
        org = Organization(name=f"race-{tag}", billing_cycle_day=1)
        s.add(org)
        await s.flush()
        s.add(OrgFeatureOverride(org_id=org.id, feature_key="ai.agent", value=True))
        u = User(org_id=org.id, username=f"race{tag}", email=f"race{tag}@acme.io",
                 password_hash="x", role=Role.MEMBER, is_active=True)
        s.add(u)
        await s.flush()
        for i in range(4):
            s.add(ApiToken(
                token_hash=hash_api_token(f"pat_{tag}{i}"), token_prefix=f"pat_{tag}{i}"[:14],
                name="h", scope="agent:read", created_by_user_id=u.id,
                created_by_email=u.email, created_at=now - timedelta(minutes=5),
                expires_at=now + timedelta(days=5),
            ))
        await s.commit()
        uid, org_id = u.id, org.id
    try:
        async with factory() as a, factory() as b:
            ua, ub = await a.get(User, uid), await b.get(User, uid)
            results = await asyncio.gather(
                *(svc.mint_agent(sess, user=usr, name="n", scope="agent:read",
                                 expires_in_days=1, cutoff_seen=token_cutoff(usr))
                  for sess, usr in ((a, ua), (b, ub))),
                return_exceptions=True,
            )
        refused = [r for r in results if isinstance(r, svc.AgentTokenCapReached)]
        assert len(refused) == 1, results
        async with factory() as s:
            live = (await s.execute(
                select(ApiToken).where(ApiToken.created_by_user_id == uid)
            )).scalars().all()
        assert len(live) == 5
    finally:
        async with factory() as s:
            await s.execute(delete(ApiToken).where(ApiToken.created_by_user_id == uid))
            await s.execute(delete(OrgFeatureOverride).where(OrgFeatureOverride.org_id == org_id))
            await s.execute(delete(User).where(User.id == uid))
            await s.execute(delete(Organization).where(Organization.id == org_id))
            await s.commit()
        await engine.dispose()
