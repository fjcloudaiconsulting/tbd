"""Plan drafts: org-scoped plans and goals on the TBD-548 model (TBD-601).

A plan is a ``scenarios`` row and its goals are ``plan_goals`` rows. Every
member of the plan's org reads and edits a draft; lifecycle transitions are
gated in the lifecycle ticket. A draft has no real-data side effects: nothing
here writes outside ``scenarios`` and ``plan_goals``.

The schema does not stop a plan or goal pointing at another org's rows, so
every link is checked here, and a goal's ``org_id`` is always its plan's.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, Scenario, ScenarioStatus
from app.models.tag import Tag
from app.services.currency_service import normalise_currency, resolve_currency_scope
from app.services.exceptions import ConflictError, NotFoundError, ValidationError

NO_CURRENCY_SCOPE = "set a currency scope to start a plan"

# column -> (model, label) for every link a plan or goal can hold.
_LINKS = {
    "funding_account_id": (Account, "account"),
    "account_id": (Account, "account"),
    "category_id": (Category, "category"),
    "tag_id": (Tag, "tag"),
    "funding_recurring_id": (RecurringTransaction, "recurring item"),
}


async def get_plan(db: AsyncSession, *, org_id: int, plan_id: int) -> Scenario:
    """The plan if it is in ``org_id``. Another org's plan is "not found"
    (404, not 403), so a probe cannot tell it exists."""
    row = await db.get(Scenario, plan_id)
    if row is None or row.org_id != org_id:
        raise NotFoundError("Scenario")
    return row


async def get_goal(db: AsyncSession, *, plan: Scenario, goal_id: int) -> PlanGoal:
    goal = await db.get(PlanGoal, goal_id)
    if goal is None or goal.scenario_id != plan.id:
        raise NotFoundError("Goal")
    return goal


async def list_goals(db: AsyncSession, *, plan: Scenario) -> list[PlanGoal]:
    return list((await db.scalars(
        select(PlanGoal).where(PlanGoal.scenario_id == plan.id)
        .order_by(PlanGoal.sort_order, PlanGoal.id)
    )).all())


async def plan_currency(db: AsyncSession, *, org_id: int, requested: Optional[str]) -> str:
    """The plan's currency: the org's currency scope. A plan never mixes
    currencies, so another currency, or an org with no scope (a legacy
    multi-currency org), is refused."""
    scope = (await resolve_currency_scope(db, org_id=org_id))["currency"]
    if scope is None:
        raise ValidationError(NO_CURRENCY_SCOPE)
    if requested is not None and normalise_currency(requested) != scope:
        raise ValidationError(
            f"a plan's currency must be the organization's currency, {scope}"
        )
    return scope


async def _check_links(
    db: AsyncSession, *, org_id: int, currency: str, changes: dict[str, Any]
) -> None:
    for column, (model, label) in _LINKS.items():
        ref = changes.get(column)
        if ref is None:
            continue
        row = await db.get(model, ref)
        if row is None or row.org_id != org_id:
            raise ValidationError(f"{label} {ref} not found in this organization")
        if model is Account and normalise_currency(row.currency) != currency:
            raise ValidationError(
                f"account {ref} is in {row.currency}, not the plan's currency {currency}"
            )


def _assert_draft(plan: Scenario) -> None:
    if plan.status != ScenarioStatus.DRAFT:
        raise ConflictError("only a draft plan can be edited here")


async def apply_plan_fields(
    db: AsyncSession, row: Scenario, changes: dict[str, Any], *, creating: bool
) -> None:
    """Validate and set the plan fields a request sent (``changes``).

    A new plan always takes the org's currency, so creating one in an org
    with no currency scope is refused. An edit that sends no plan field
    leaves a legacy row as it is."""
    if not creating:
        if not changes:
            return
        _assert_draft(row)
    currency = await plan_currency(
        db, org_id=row.org_id, requested=changes.get("currency", row.currency)
    )
    await _check_links(db, org_id=row.org_id, currency=currency, changes=changes)
    for key, value in changes.items():
        setattr(row, key, value)
    row.currency = currency


async def create_goal(db: AsyncSession, *, plan: Scenario, fields: dict[str, Any]) -> PlanGoal:
    _assert_draft(plan)
    currency = await plan_currency(db, org_id=plan.org_id, requested=plan.currency)
    await _check_links(db, org_id=plan.org_id, currency=currency, changes=fields)
    # org_id comes from the plan, never from the request.
    goal = PlanGoal(**fields, org_id=plan.org_id, scenario_id=plan.id)
    db.add(goal)
    return goal


async def update_goal(
    db: AsyncSession, *, plan: Scenario, goal: PlanGoal, changes: dict[str, Any]
) -> None:
    _assert_draft(plan)
    currency = await plan_currency(db, org_id=plan.org_id, requested=plan.currency)
    await _check_links(db, org_id=plan.org_id, currency=currency, changes=changes)
    for key, value in changes.items():
        setattr(goal, key, value)


async def delete_goal(db: AsyncSession, *, plan: Scenario, goal: PlanGoal) -> None:
    _assert_draft(plan)
    await db.delete(goal)
