"""SSO step-up proof, scoped to one action (TBD-390).

The ONLY reader/writer of ``users.stepup_token`` (besides the model, the
schemas and the export registry). The stored value is ``f"{action}:{token}"``;
the bare ``token`` travels in the callback's URL fragment. A consumer only
accepts a token issued for its own action, so a proof minted for an email
change can never mint a PAT (fenced in
``tests/auth/test_stepup_action_scoping.py``).
"""
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.user import User

STEPUP_TOKEN_TTL_SECONDS = 5 * 60

# action -> the one page the callback returns to. Single source of truth for
# both the initiate allowlist and the redirect target. New entries must stay
# same-origin first-party paths (the callback redirects to them verbatim).
STEPUP_ACTIONS: dict[str, str] = {
    "email_change": "/settings",
    "password_set": "/settings/security",
    "pat_mint": "/system/api-tokens",
    # TBD-578. Own action, never ``pat_mint``: a proof issued for the agent
    # token must not mint the superadmin credential, nor the reverse.
    "agent_token_mint": "/settings/agent-tokens",
}


def _aware(dt: datetime) -> datetime:
    """The expiry column is naive ``DateTime``; every write is UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _encode(value: str) -> bytes:
    # surrogatepass: a JSON body can carry a lone surrogate, which plain
    # ``.encode()`` rejects. Bytes compare never raises on non-ASCII.
    return value.encode("utf-8", "surrogatepass")


def issue_stepup(user: User, action: str) -> str:
    """Write a fresh proof for ``action`` onto ``user``; return the BARE token.

    The caller commits. An unknown action is a programmer error.
    """
    if action not in STEPUP_ACTIONS:
        raise ValueError(f"unknown step-up action: {action!r}")
    token = secrets.token_urlsafe(32)
    user.stepup_token = f"{action}:{token}"
    user.stepup_token_expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=STEPUP_TOKEN_TTL_SECONDS
    )
    return token


def stepup_valid(user: User, presented: str | None, action: str) -> bool:
    """True iff ``presented`` is a live proof issued for ``action``. Pure."""
    stored = user.stepup_token
    expires_at = user.stepup_token_expires_at
    return bool(
        presented
        and stored is not None
        and expires_at is not None
        and _aware(expires_at) > datetime.now(timezone.utc)
        and secrets.compare_digest(_encode(f"{action}:{presented}"), _encode(stored))
    )


async def consume_stepup(db: AsyncSession, user: User) -> bool:
    """Atomically spend the proof ``user`` currently holds. Single-use.

    Runs inside the caller's transaction: the caller's commit makes it
    durable, a caller rollback restores the token. Two requests racing on one
    proof: only the one whose UPDATE still matches the stored value wins.

    On ``False`` the session has been ROLLED BACK (a 0-row UPDATE under MySQL
    RR keeps the row lock; the caller's out-of-band failure audit would wait
    on it). Caller contract: after ``False`` do not read ORM attributes of
    ``user`` (they are expired); raise the consumer's rejection.
    """
    user_id = user.id
    stored = user.stepup_token
    rowcount = 0
    if stored is not None:
        result = await db.execute(
            update(User)
            .where(User.id == user_id, User.stepup_token == stored)
            .values(stepup_token=None, stepup_token_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        rowcount = result.rowcount
    if rowcount != 1:
        await db.rollback()
        return False
    # Mirror the DB without marking the attributes dirty: no write-back.
    set_committed_value(user, "stepup_token", None)
    set_committed_value(user, "stepup_token_expires_at", None)
    return True
