"""Purge of unredeemed OAuth codes and idle OAuth clients (TBD-587).

Runs once per tick from ``run_one_tick``, after the expiry reminders, under
the same tick lock. Platform-global like the PAT sweep: no org dimension.

1. Placeholders: grant rows whose code was never redeemed
   (``oauth_client_id`` set, ``refresh_hash`` NULL) once the code expired.
2. Clients with NO ``api_tokens`` row whose last approved consent (or, if
   none, registration) is older than 30 days. Placeholders go first, so a
   client whose only row was an expired code is freed in the same run.

Grant rows are only ever deleted, never SET NULL: a row with a nulled client
id would look like a manual token to the expiry sweep. Errors are logged,
never raised (the ticker must not die).
"""
from __future__ import annotations

import datetime

import structlog
from sqlalchemy import delete, func, select

from app.database import async_session
from app.models.api_token import ApiToken
from app.models.oauth_client import OAuthClient

logger = structlog.get_logger(__name__)

IDLE_CLIENT_DAYS = 30


async def run_oauth_client_purge(
    session_factory=async_session, *, now: datetime.datetime | None = None
) -> int:
    """Delete expired placeholders, then idle clients. Returns clients deleted."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is not None:  # columns are naive UTC
        now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    try:
        async with session_factory() as db:
            codes = await db.execute(delete(ApiToken).where(
                ApiToken.oauth_client_id.isnot(None),
                ApiToken.refresh_hash.is_(None),
                ApiToken.expires_at < now,
            ))
            clients = await db.execute(delete(OAuthClient).where(
                ~select(ApiToken.id).where(ApiToken.oauth_client_id == OAuthClient.id).exists(),
                func.coalesce(OAuthClient.last_used_at, OAuthClient.created_at)
                < now - datetime.timedelta(days=IDLE_CLIENT_DAYS),
            ))
            await db.commit()
    except Exception as exc:  # noqa: BLE001 -- never let the ticker die
        await logger.aerror("scheduler.oauth_client_purge.failed", error=str(exc))
        return 0
    if codes.rowcount or clients.rowcount:
        await logger.ainfo("scheduler.oauth_client_purge.complete",
                           codes=codes.rowcount, clients=clients.rowcount)
    return clients.rowcount
