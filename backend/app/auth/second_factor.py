"""Second-factor checks shared by the MFA routes.

Recovery codes are spent with a compare-and-swap on the stored list, the same
shape as ``app.auth.stepup.consume_stepup``: two requests carrying one code
read the same list, and only the one whose UPDATE still matches it wins
(BE-21).
"""
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.user import User
from app.services.mfa_service import verify_recovery_code


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
