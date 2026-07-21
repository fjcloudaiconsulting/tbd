"""Platform-level PAT expiry-reminder job (spec §10, ARC-R10).

This job is deliberately NOT part of the per-org ``REGISTRY`` runner. A PAT
belongs to a superadmin and carries no org dimension, so it cannot be gated on
a per-org ``scheduler.``-namespaced ``OrgSetting`` and the org-iterating runner
has no natural place for it. Instead it runs once per tick, invoked directly
from ``run_one_tick`` under the same ``scheduler:tick:lock`` Redis lock (so a
single replica runs it per tick), and is gated on a *global* ``SystemSetting``
flag (``api_token_expiry_reminders_enabled``, value ``"on"``).

Behavior: a daily scan of non-revoked, non-fully-reminded tokens with a
non-null owner. Each token advances through three reminder stages as it
approaches expiry:

    reminder_stage: 0 = none, 1 = 14d sent, 2 = 3d sent, 3 = on-expiry sent

Per tick, a token advances at most one stage: the *next* stage fires only when
the time remaining has crossed that stage's threshold (14d / 3d / on-or-after
expiry). Each fire sends one email + one in-app notification to the owner and
advances ``reminder_stage`` in a committed unit BEFORE the email goes out, so a
double-run tick at the same wall-clock re-reads the advanced stage and cannot
re-notify (idempotency, ARC-R10). Processing tokens in separate sessions keeps
one token's failure from poisoning the sweep (mirrors ``runner.run_all_due``).
"""
from __future__ import annotations

import datetime
from datetime import timezone

import structlog
from sqlalchemy import select

from app.database import async_session
from app.models.api_token import ApiToken
from app.models.notification import NotificationCategory
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.services.email_service import send_notification_email
from app.services.notification_service import dispatch_notification

logger = structlog.get_logger(__name__)

FLAG_KEY = "api_token_expiry_reminders_enabled"
EVENT_TYPE = "api_token.expiry_reminder"
LINK_URL = "/admin/api-tokens"
FULLY_REMINDED_STAGE = 3

# Days-remaining threshold that unlocks each *next* stage. A token at stage
# ``s`` fires stage ``s + 1`` once its time-to-expiry is <= this many days.
_STAGE_THRESHOLD_DAYS = {1: 14, 2: 3, 3: 0}


def _aware(dt: datetime.datetime) -> datetime.datetime:
    """Treat a tz-naive DB datetime as UTC (columns are naive-UTC, ARC-R7)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_flag(value: str | None) -> bool:
    """The global enable flag is on only for an explicit truthy value."""
    if value is None:
        return False
    return value.strip().lower() in {"on", "true", "1", "yes"}


def _reminder_copy(days_remaining: int, next_stage: int, token_name: str) -> tuple[str, str]:
    """Return (title, body) for a fired reminder. No em-dashes (copy rule)."""
    if next_stage >= FULLY_REMINDED_STAGE:
        title = "An API token has expired"
        body = (
            f'Your API token "{token_name}" has expired and can no longer be '
            "used. Create a new token if you still need programmatic access."
        )
    else:
        window = "14 days" if next_stage == 1 else "3 days"
        title = f"An API token expires in {window}"
        body = (
            f'Your API token "{token_name}" expires in about {window}. Rotate '
            "it before then to avoid an interruption to any automation that "
            "uses it."
        )
    return title, body


async def _flag_enabled(session_factory) -> bool:
    async with session_factory() as db:
        value = await db.scalar(
            select(SystemSetting.value).where(SystemSetting.key == FLAG_KEY)
        )
    return _parse_flag(value)


async def _due_token_ids(session_factory, now: datetime.datetime) -> list[int]:
    """Candidate tokens: not revoked, not fully reminded, non-null owner.

    Returns only ids; each token is then re-read and mutated in its own
    session so the stage-advance commits independently and idempotently.
    """
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(ApiToken.id)
                .where(
                    ApiToken.revoked_at.is_(None),
                    ApiToken.created_by_user_id.isnot(None),
                    ApiToken.reminder_stage < FULLY_REMINDED_STAGE,
                )
                .order_by(ApiToken.id)
            )
        ).scalars().all()
    return list(rows)


async def _process_token(session_factory, token_id: int, now: datetime.datetime) -> bool:
    """Advance one token by at most one stage if its threshold is crossed.

    The in-app row write and the ``reminder_stage`` bump commit together
    BEFORE the email send, so a concurrent/re-run tick re-reads the advanced
    stage and will not re-fire this threshold. Returns True if a reminder was
    sent. Failures are logged and swallowed so the sweep continues.
    """
    async with session_factory() as db:
        try:
            token = await db.get(ApiToken, token_id)
            if token is None:
                return False
            # Re-check invariants against the freshly-read row (guards against a
            # revoke / stage-advance landed by another actor since the scan).
            if (
                token.revoked_at is not None
                or token.created_by_user_id is None
                or token.reminder_stage >= FULLY_REMINDED_STAGE
            ):
                return False

            next_stage = token.reminder_stage + 1
            threshold_days = _STAGE_THRESHOLD_DAYS[next_stage]
            seconds_remaining = (_aware(token.expires_at) - now).total_seconds()
            days_remaining = seconds_remaining / 86400.0
            if days_remaining > threshold_days:
                return False  # not yet at the next threshold

            owner = await db.get(User, token.created_by_user_id)
            if owner is None:
                # FK says non-null but the row is gone — treat as null-owner.
                return False

            title, body = _reminder_copy(
                days_remaining=int(days_remaining),
                next_stage=next_stage,
                token_name=token.name,
            )
            owner_email = owner.email

            # In-app row (SECURITY category is force-on, never preference-skipped)
            # then advance the stage — both in this one committed transaction.
            await dispatch_notification(
                db,
                user_id=token.created_by_user_id,
                category=NotificationCategory.SECURITY,
                event_type=EVENT_TYPE,
                title=title,
                body=body,
                link_url=LINK_URL,
            )
            token.reminder_stage = next_stage
            await db.commit()
        except Exception as exc:  # noqa: BLE001 — isolate per-token failures
            await db.rollback()
            await logger.aerror(
                "scheduler.api_token_expiry.token_failed",
                token_id=token_id,
                error=str(exc),
            )
            return False

    # Email is best-effort and runs AFTER the durable stage-advance commit, so a
    # send failure can never roll the stage back into a re-notify loop.
    try:
        await send_notification_email(
            owner_email, title=title, body=body, link_url=LINK_URL
        )
    except Exception as exc:  # noqa: BLE001 — email never fails the sweep
        await logger.awarning(
            "scheduler.api_token_expiry.email_failed",
            token_id=token_id,
            error=str(exc),
        )
    return True


async def run_api_token_expiry_reminders(
    session_factory=async_session, *, now: datetime.datetime | None = None
) -> int:
    """Scan PAT tokens and fire any due expiry reminders. Returns fired count.

    Gated on the global ``SystemSetting`` flag ``api_token_expiry_reminders_enabled``
    (value ``"on"``). Flag off / absent → no-op. Invoked from ``run_one_tick``
    under the tick lock; NOT part of the per-org registry.
    """
    if now is None:
        now = datetime.datetime.now(timezone.utc)

    if not await _flag_enabled(session_factory):
        return 0

    token_ids = await _due_token_ids(session_factory, now)
    fired = 0
    for token_id in token_ids:
        if await _process_token(session_factory, token_id, now):
            fired += 1

    if fired:
        await logger.ainfo("scheduler.api_token_expiry.complete", fired=fired)
    return fired
