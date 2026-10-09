"""TBD-587: OAuth single-use under MySQL REPEATABLE READ (F-O1, F-O5).

Skipped unless ``PFV_RUN_MYSQL_TESTS=1`` (needs a migrated MySQL): SQLite
serialises writers, so it cannot show two exchanges or two refreshes racing.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.api_token import ApiToken
from app.models.feature_override import OrgFeatureOverride
from app.models.oauth_client import OAuthClient
from app.models.user import Organization, Role, User
from app.security import token_cutoff
from app.services import api_token_service as svc
from app.services import oauth_service

pytestmark = pytest.mark.skipif(
    os.environ.get("PFV_RUN_MYSQL_TESTS") != "1",
    reason="MySQL-only concurrency test; set PFV_RUN_MYSQL_TESTS=1 to run.",
)

CB = "https://claude.ai/api/mcp/auth_callback"


async def _setup(factory, tag):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    async with factory() as s:
        org = Organization(name=f"oauth-{tag}", billing_cycle_day=1)
        s.add(org)
        await s.flush()
        s.add(OrgFeatureOverride(org_id=org.id, feature_key="ai.agent", value=True))
        u = User(org_id=org.id, username=f"oa{tag}", email=f"oa{tag}@acme.io",
                 password_hash="x", role=Role.MEMBER, is_active=True)
        s.add(u)
        s.add(OAuthClient(id=tag * 4, client_name="Claude", redirect_uris=[CB],
                          metadata_key=tag * 8))
        await s.commit()
        code, _ = await svc.create_oauth_grant(
            s, user=u, cutoff_seen=token_cutoff(u), client_id=tag * 4, client_name="Claude",
            scope="agent:write", code_challenge=challenge, redirect_uri=CB,
        )
        return u.id, org.id, code, verifier


async def _cleanup(factory, uid, org_id, cid):
    async with factory() as s:
        await s.execute(delete(ApiToken).where(ApiToken.created_by_user_id == uid))
        await s.execute(delete(OAuthClient).where(OAuthClient.id == cid))
        await s.execute(delete(OrgFeatureOverride).where(OrgFeatureOverride.org_id == org_id))
        await s.execute(delete(User).where(User.id == uid))
        await s.execute(delete(Organization).where(Organization.id == org_id))
        await s.commit()


async def _race(factory, form):
    async with factory() as a, factory() as b:
        return await asyncio.gather(
            *(oauth_service.token_request(sess, factory, form, "203.0.113.1") for sess in (a, b)),
            return_exceptions=True,
        )


def _split(results):
    ok = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, oauth_service.OAuthError)]
    assert len(ok) + len(refused) == 2, results
    return ok, refused


async def test_two_concurrent_exchanges_of_one_code_yield_one_grant():
    """FENCE F-O1. Wrong implementation: SELECT-then-unconditional UPDATE
    (both exchanges mint tokens from one code)."""
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tag = secrets.token_hex(4)
    uid, org_id, code, verifier = await _setup(factory, tag)
    try:
        ok, refused = _split(await _race(factory, {
            "grant_type": "authorization_code", "code": code, "redirect_uri": CB,
            "client_id": tag * 4, "code_verifier": verifier,
        }))
        assert len(ok) == 1 and [e.error for e in refused] == ["invalid_grant"]
    finally:
        await _cleanup(factory, uid, org_id, tag * 4)
        await engine.dispose()


async def test_two_concurrent_refreshes_rotate_once_and_revoke():
    """FENCE F-O5. Wrong implementation: SELECT-then-UPDATE without the row
    lock or the ``refresh_hash`` predicate (both rotate, two live pairs)."""
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tag = secrets.token_hex(4)
    uid, org_id, code, verifier = await _setup(factory, tag)
    try:
        async with factory() as s:
            tokens = await oauth_service.token_request(s, factory, {
                "grant_type": "authorization_code", "code": code, "redirect_uri": CB,
                "client_id": tag * 4, "code_verifier": verifier,
            }, "203.0.113.1")
        ok, refused = _split(await _race(factory, {
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        }))
        assert len(ok) == 1 and [e.error for e in refused] == ["invalid_grant"]
        async with factory() as s:
            row = (await s.execute(
                select(ApiToken).where(ApiToken.created_by_user_id == uid))).scalar_one()
        assert row.revoked_at is not None
    finally:
        await _cleanup(factory, uid, org_id, tag * 4)
        await engine.dispose()
