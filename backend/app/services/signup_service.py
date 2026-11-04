"""Email-first sign-up (TBD-654): the per-address mail caps and the start decision.

``POST /auth/signup/start`` answers the same 202 whatever the address's state,
so everything that could differ between states lives here, behind that answer:
which email goes out, if any. The caps are claimed first and depend on the
address only, so they hold identically on every path.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import state_db
from app.models.user import User
from app.security import create_email_verification_token, create_signup_token, signup_mail_key
from app.services.email_service import (
    send_existing_account_signup_email,
    send_legacy_verification_email,
    send_signup_link_email,
)
from app.services.user_service import normalize_email

SignupPath = Literal["new", "existing", "legacy_unverified", "inactive", "capped"]

# ``used_tokens.scope`` is ``ascii_id(32)``: a longer scope fails every request
# on MySQL strict mode while SQLite accepts it. The per-address key goes in the
# token column instead.
MAIL_CAP_10M_SCOPE = "signup_mail_10m"
MAIL_CAP_24H_SCOPE = "signup_mail_24h"
MAIL_CAP_DAILY = 5


async def claim_mail_slot(email: str) -> bool:
    """True when a step-1 email may go to ``email``: at most one per 10 minutes
    and ``MAIL_CAP_DAILY`` per 24 hours. Both windows slide, because each claim
    expires on its own TTL rather than at a bucket edge. A refusal by the
    10-minute cap uses no daily slot."""
    key = signup_mail_key(email)
    if not await state_db.claim_token(MAIL_CAP_10M_SCOPE, key, 10 * 60):
        return False
    for slot in range(MAIL_CAP_DAILY):
        if await state_db.claim_token(MAIL_CAP_24H_SCOPE, f"{key}:{slot}", 24 * 60 * 60):
            return True
    return False


async def plan_start(
    db: AsyncSession, email: str
) -> tuple[SignupPath, Callable[[], Awaitable[bool]] | None]:
    """Decide what step 1 mails to ``email``: the path (for the audit row) and
    the zero-argument send the route queues, or None when nothing goes out.

    The caps are claimed before the account is looked up, so a capped address
    reads no state and every path spends the same claims.
    """
    email = normalize_email(email)
    if not await claim_mail_slot(email):
        return "capped", None
    row = (
        await db.execute(
            select(User.id, User.email, User.is_active, User.email_verified).where(
                User.email == email
            )
        )
    ).first()
    if row is None:
        return "new", partial(send_signup_link_email, email, create_signup_token(email))
    if not row.is_active:
        return "inactive", None
    if row.email_verified:
        return "existing", partial(send_existing_account_signup_email, row.email)
    token = create_email_verification_token(row.id, row.email)
    return "legacy_unverified", partial(send_legacy_verification_email, row.email, token)
