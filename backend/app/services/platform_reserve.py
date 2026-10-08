"""Platform AI spend reservation (TBD-586 PR1 kernel).

``reserve`` admits one platform dispatch against the org's two platform meters
(``platform_ai.tokens``, ``platform_ai.cents``) and the global monthly ceiling
(``platform_ai_spend``) in ONE transaction, or refuses with nothing reserved.
``settle`` swaps the reservation for the actual usage on the SAME period keys,
once. There is no release path: a reservation that is not settled stays.

Lock order is fixed (org tokens, org cents, global) so two dispatches never
deadlock on each other's rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

import structlog
from sqlalchemy import update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.models.platform_ai_spend import PlatformAISpend
from app.models.usage_counter import UsageCounter
from app.services import feature_service
from app.services.usage_service import PlanLimitReached, period_start, resets_at

logger = structlog.stdlib.get_logger()

TOKENS = "platform_ai.tokens"
CENTS = "platform_ai.cents"


class PlatformAIUnavailable(Exception):
    """Global ceiling reached, or the reservation could not be written.
    Mapped to 402 ``platform_ai_unavailable``."""


@dataclass
class SettleHandle:
    """One-shot. Carries the reservation's period keys so a settle after a
    period boundary still moves the rows it reserved on."""

    org_id: int
    tokens_key: tuple[str, date]  # (period kind, period_start)
    cents_key: tuple[str, date]
    spend_start: date
    tokens: int
    cents: int
    done: bool = field(default=False)


@dataclass
class Reservation:
    adapter: Any
    handle: SettleHandle


def _upsert(db: AsyncSession, model, row: dict, noop_col: str):
    if db.get_bind().dialect.name == "mysql":
        ins = mysql_insert(model).values(**row)
        return ins.on_duplicate_key_update({noop_col: getattr(model, noop_col)})
    return sqlite_insert(model).values(**row).on_conflict_do_nothing()


def _counter(org_id: int, meter: str, key: tuple[str, date]):
    return update(UsageCounter).where(
        UsageCounter.org_id == org_id,
        UsageCounter.meter == meter,
        UsageCounter.period == key[0],
        UsageCounter.period_start == key[1],
    ).execution_options(synchronize_session=False)


def _spend(start: date):
    return update(PlatformAISpend).where(
        PlatformAISpend.period_start == start
    ).execution_options(synchronize_session=False)


async def reserve(
    db: AsyncSession,
    org_id: int,
    tokens: int,
    cents: int,
    ceiling_cents: int,
    make_adapter: Callable[[], Any],
    *,
    now: datetime | None = None,
) -> Reservation:
    """Reserve ``tokens``/``cents`` or raise. COMMITS the caller's session
    (like ``usage_service.admit``), so call it before any write of the unit
    of work. ``make_adapter`` runs only after the reservation committed."""
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("platform_reserve.reserve: the session holds uncommitted ORM changes")
    now = now or utcnow_naive()
    ent = await feature_service.get_entitlements(db, org_id, now=now)
    lt, lc = ent.limits[TOKENS], ent.limits[CENTS]
    tkey = (lt.period, period_start(lt.period, now))
    ckey = (lc.period, period_start(lc.period, now))
    month = period_start("month", now)

    try:
        for meter, key in ((TOKENS, tkey), (CENTS, ckey)):
            await db.execute(_upsert(db, UsageCounter, dict(
                org_id=org_id, meter=meter, period=key[0], period_start=key[1], value=0,
            ), "value"))
        await db.execute(_upsert(db, PlatformAISpend, dict(period_start=month, cents=0), "cents"))
        await db.commit()

        steps = (
            (TOKENS, _counter(org_id, TOKENS, tkey)
                .where(UsageCounter.value + tokens <= lt.limit)
                .values(value=UsageCounter.value + tokens)),
            (CENTS, _counter(org_id, CENTS, ckey)
                .where(UsageCounter.value + cents <= lc.limit)
                .values(value=UsageCounter.value + cents)),
            (None, _spend(month)
                .where(PlatformAISpend.cents + cents <= ceiling_cents)
                .values(cents=PlatformAISpend.cents + cents)),
        )
        for meter, stmt in steps:
            if (await db.execute(stmt)).rowcount != 1:
                await db.rollback()
                if meter is None:
                    raise PlatformAIUnavailable("global ceiling reached")
                lim = lt if meter == TOKENS else lc
                raise PlanLimitReached(
                    meter, lim.limit, lim.period,
                    None if lim.limit == 0 else resets_at(lim.period, now),
                )
        await db.commit()
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.warning("platform_reserve.reserve_failed", org_id=org_id, error=type(exc).__name__)
        raise PlatformAIUnavailable("reservation failed") from exc

    return Reservation(
        adapter=make_adapter(),
        handle=SettleHandle(org_id, tkey, ckey, month, tokens, cents),
    )


async def settle(db: AsyncSession, handle: SettleHandle, tokens: int, cents: int) -> None:
    """Replace the reservation with the actual usage, once. Unconditional:
    actual above the limit still lands (the call already happened). The
    caller decides usage completeness and passes actual tokens/cents only
    when it is complete; otherwise it does not call settle."""
    if handle.done:
        return
    handle.done = True
    dt, dc = tokens - handle.tokens, cents - handle.cents
    try:
        await db.execute(_counter(handle.org_id, TOKENS, handle.tokens_key)
                         .values(value=UsageCounter.value + dt))
        await db.execute(_counter(handle.org_id, CENTS, handle.cents_key)
                         .values(value=UsageCounter.value + dc))
        await db.execute(_spend(handle.spend_start).values(cents=PlatformAISpend.cents + dc))
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()  # reservation kept; caller logs and still returns
        raise
