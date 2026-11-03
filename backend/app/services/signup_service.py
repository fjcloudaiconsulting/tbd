"""Email-first sign-up (TBD-654): the per-address mail caps and the start decision.

``POST /auth/signup/start`` answers the same 202 whatever the address's state,
so everything that could differ between states lives here, behind that answer:
which email goes out, if any. The caps are claimed first and depend on the
address only, so they hold identically on every path.
"""
from __future__ import annotations

from app import state_db
from app.security import signup_mail_key

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
