"""Second-factor checks shared by the MFA routes.

Recovery codes are spent with a compare-and-swap on the stored list, the same
shape as ``app.auth.stepup.consume_stepup``: two requests carrying one code
read the same list, and only the one whose UPDATE still matches it wins
(BE-21).

A TOTP code is accepted once per user (BE-26, RFC 6238 section 5.2): the
accepted time step is claimed in ``used_tokens``, and so are the two steps
before it, so neither the same code nor an older one still inside the +/- 1
window works after it.
"""
from fastapi import HTTPException, Request, status
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from app import state_db
from app.auth import attempts
from app.models.user import User
from app.security import verify_password
from app.services.mfa_service import (
    MfaConfigError,
    decrypt_secret,
    match_totp_step,
    verify_recovery_code,
)

TOTP_STEP_SCOPE = "totp_step"
# A step is accepted at most until the end of the step after it (90 s from
# its start); the claim outlives that.
_TOTP_STEP_CLAIM_TTL_SECONDS = 120


async def verify_totp_once(user: User, code: str) -> bool:
    """True when ``code`` is a current TOTP for ``user`` whose time step was not
    accepted for this user before. Raises ``ValueError`` / ``MfaConfigError``
    for an undecryptable secret (callers answer 503) and ``SQLAlchemyError``
    when the store is down."""
    step = match_totp_step(decrypt_secret(user.totp_secret), code)
    if step is None:
        return False
    if not await state_db.claim_token(TOTP_STEP_SCOPE, f"{user.id}:{step}", _TOTP_STEP_CLAIM_TTL_SECONDS):
        return False
    for older in (step - 1, step - 2):
        await state_db.claim_token(TOTP_STEP_SCOPE, f"{user.id}:{older}", _TOTP_STEP_CLAIM_TTL_SECONDS)
    return True


async def consume_recovery_code(db: AsyncSession, user: User, code: str) -> bool:
    """Spend ``code`` from ``user``'s recovery codes. Single-use under races.

    Runs inside the caller's transaction: the caller's commit makes it
    durable, a caller rollback restores the code. ``False`` when no code
    matches. When one matched but another request spent the list first, the
    session has also been ROLLED BACK, as in ``consume_stepup``: after a
    ``False``, do not read ORM attributes of ``user``.
    """
    user_id = user.id
    stored = user.recovery_codes
    if not stored:
        return False
    hashed = stored.split(",")
    idx = verify_recovery_code(code, hashed)
    if idx is None:
        return False
    remaining = hashed[:idx] + hashed[idx + 1:]
    new_value = ",".join(remaining) if remaining else None
    result = await db.execute(
        update(User)
        .where(User.id == user_id, User.recovery_codes == stored)
        .values(recovery_codes=new_value)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        return False
    # Mirror the DB without marking the attribute dirty: no write-back.
    set_committed_value(user, "recovery_codes", new_value)
    return True


async def verify_second_factor(db: AsyncSession, user: User, code: str) -> bool:
    """A current TOTP code or an unused recovery code for ``user``. Six digits
    is a TOTP code (accepted once, ``verify_totp_once``); anything else is a
    recovery code, SPENT on success inside the caller's transaction (after a
    ``False`` do not read ``user``, see ``consume_recovery_code``)."""
    candidate = code.strip()
    if len(candidate) == 6 and candidate.isdigit():
        return bool(user.totp_secret) and await verify_totp_once(user, candidate)
    return await consume_recovery_code(db, user, candidate)


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication temporarily unavailable",
    )


async def reauth_password(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    request: Request,
    user: User,
    password: str | None,
    status_code: int = status.HTTP_403_FORBIDDEN,
    detail: str = "Invalid password",
) -> None:
    """Re-authenticate a signed-in user by password before an MFA change
    (BE-32). Wrong passwords draw from the account's ``/login`` failure budget
    (keyed on the username), so a stolen session cannot guess the password
    here without limit. ``status_code`` / ``detail`` on a wrong password, 429
    when the budget is full.
    """
    login = user.username
    try:
        retry_after = await attempts.login_retry_after(login)
    except SQLAlchemyError as exc:
        raise _unavailable() from exc
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=attempts.TOO_MANY_LOGIN_ATTEMPTS_DETAIL,
            headers={"Retry-After": str(retry_after)},
        )
    if password and verify_password(password, user.password_hash):
        return
    try:
        filled = await attempts.record_login_failure(login)
    except SQLAlchemyError as exc:
        raise _unavailable() from exc
    if filled:
        await attempts.audit_window_filled(
            session_factory,
            request=request,
            event_type="auth.login.locked",
            user_id=user.id,
            user_email=user.email,
            user_org_id=user.org_id,
            failures=attempts.LOGIN_FAILURES_PER_ACCOUNT,
        )
    raise HTTPException(status_code=status_code, detail=detail)


async def require_second_factor(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    request: Request,
    user: User,
    code: str | None,
    status_code: int = status.HTTP_403_FORBIDDEN,
    detail: str = "Invalid authentication code",
) -> None:
    """Require a current TOTP code or an unused recovery code from a signed-in
    MFA user before a change that could remove or bypass MFA (BE-32): turning
    it off, minting new recovery codes, moving the account email.

    Wrong codes draw from the user's second-factor budget, the same one the
    sign-in challenge uses (BE-02). A recovery code is spent in the caller's
    transaction. After a refusal the session may have been rolled back: raise,
    do not read ``user``.
    """
    user_id, user_email, user_org_id = user.id, user.email, user.org_id
    try:
        retry_after = await attempts.mfa_user_retry_after(user_id)
    except SQLAlchemyError as exc:
        raise _unavailable() from exc
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=attempts.TOO_MANY_MFA_ATTEMPTS_DETAIL,
            headers={"Retry-After": str(retry_after)},
        )
    try:
        accepted = bool(code) and await verify_second_factor(db, user, code)
        if accepted:
            await attempts.clear_mfa_failures(user_id)
        else:
            filled = await attempts.record_mfa_failure(user_id, None)
    except (ValueError, MfaConfigError):
        raise HTTPException(status_code=503, detail="MFA configuration error — contact support")
    except SQLAlchemyError as exc:
        raise _unavailable() from exc
    if accepted:
        return
    if filled:
        await attempts.audit_window_filled(
            session_factory,
            request=request,
            event_type="auth.mfa.locked",
            user_id=user_id,
            user_email=user_email,
            user_org_id=user_org_id,
            failures=attempts.MFA_FAILURES_PER_USER,
        )
    raise HTTPException(status_code=status_code, detail=detail)
