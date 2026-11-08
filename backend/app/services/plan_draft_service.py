"""Plan drafts: org-scoped plans and goals on the TBD-548 model (TBD-601).

A plan is a ``scenarios`` row and its goals are ``plan_goals`` rows. Who sees
and changes what follows the reports precedent (``routers/reports.py``):

- A legacy scenario (never given plan fields or goals) stays private to its
  creator, as before TBD-601.
- A plan is visible to every member of its org. Its creator, or an org OWNER
  or ADMIN, edits or deletes it and writes its goals; another member gets 403.
- Another org's row is a 404 either way, so a probe cannot tell it exists.

Lifecycle transitions are ``plan_lifecycle_service``'s (TBD-602). A draft has
no real-data side effects: nothing here writes outside ``scenarios`` and
``plan_goals``. A started plan is edited only with a confirmation, and only in
the fields ``assert_editable`` allows; a finished one is a read-only record.

The schema does not stop a plan or goal pointing at another org's rows, so
every link is checked here, and a goal's ``org_id`` is always its plan's.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.recurring import RecurringTransaction
from app.models.scenario import PlanGoal, Scenario, ScenarioStatus
from app.models.tag import Tag
from app.models.user import Role, User
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


class PlanEditDenied(Exception):
    """A member who can see a plan but may not change it (HTTP 403)."""


def is_plan(row: Scenario) -> bool:
    """A row with plan fields. ``currency`` is the marker: 098 left it NULL on
    every legacy row (no backfill), and every plan-field or goal write sets it
    to the org's currency (``plan_currency``), so it is NULL exactly on rows
    that never became a plan."""
    return row.currency is not None


def visible_filter(user: User):
    """``WHERE`` clause for the scenarios ``user`` may see."""
    return (Scenario.org_id == user.org_id) & or_(
        Scenario.user_id == user.id, Scenario.currency.is_not(None)
    )


def can_view(user: User, row: Scenario) -> bool:
    return row.org_id == user.org_id and (row.user_id == user.id or is_plan(row))


def can_edit(user: User, row: Scenario) -> bool:
    return can_view(user, row) and (
        row.user_id == user.id or user.role in (Role.OWNER, Role.ADMIN)
    )


async def get_plan(
    db: AsyncSession, *, user: User, plan_id: int, write: bool = False
) -> Scenario:
    """The scenario if ``user`` may see it (else "not found", 404), and, with
    ``write``, change it (else ``PlanEditDenied``, 403)."""
    row = await db.get(Scenario, plan_id)
    if row is None or not can_view(user, row):
        raise NotFoundError("Scenario")
    if write and not can_edit(user, row):
        raise PlanEditDenied("only the plan's creator or an org owner or admin can change it")
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
        accounts = []
        if model is Account:
            accounts = [row]
        elif model is RecurringTransaction:
            # Its money is in its account's currency (both legs' for a transfer).
            accounts = [await db.get(Account, a)
                        for a in (row.account_id, row.to_account_id) if a is not None]
        for acc in accounts:
            if normalise_currency(acc.currency) != currency:
                raise ValidationError(
                    f"{label} {ref} is in {acc.currency}, not the plan's currency {currency}"
                )


def assert_draft(plan: Scenario) -> None:
    if plan.status != ScenarioStatus.DRAFT:
        raise ConflictError("only a draft plan can be edited here")


# What a started plan may still change (TBD-602): nothing that a series, a
# category or a tag Start made depends on. ``monthly_budget`` reaches its
# Budgets; a new or newly enabled goal gets Start's steps (the routers).
# Not ``is_active``: hiding a running plan waits for Archive (TBD-606).
STARTED_PLAN_FIELDS = frozenset({
    "name", "params", "horizon_months", "monthly_budget", "birth_year",
    "return_pct", "inflation_pct",
})
STARTED_GOAL_FIELDS = frozenset({"name", "tier", "sort_order", "enabled"})
CONFIRM_TO_EDIT = (
    "this plan has started: changing it is discouraged, so resend with confirm=true to go ahead"
)


def assert_editable(
    plan: Scenario, *, confirm: bool, changes: dict[str, Any], allowed: frozenset[str] | None,
) -> None:
    """A draft takes any change. An active plan takes the ``allowed`` fields
    (``None``: any, a new goal) with ``confirm``, and never clears its budget
    or disables a goal: Start's rows would be left behind. A finished plan
    takes none."""
    if plan.status == ScenarioStatus.DRAFT:
        return
    if plan.status != ScenarioStatus.ACTIVE:
        raise ConflictError("a finished plan is a read-only record")
    if not confirm:
        raise ConflictError(CONFIRM_TO_EDIT)
    refused = sorted(k for k, v in changes.items() if allowed is not None and (
        k not in allowed or (k in ("monthly_budget", "enabled") and (v is None or v is False))))
    if refused:
        raise ConflictError(f"a started plan cannot change {', '.join(refused)} this way")


async def apply_plan_fields(db: AsyncSession, row: Scenario, changes: dict[str, Any]) -> None:
    """Validate and set the plan fields a request sent (``changes``), which
    makes the row a plan in the org's currency; with no currency scope that
    is refused. A request without plan fields leaves a legacy row as it is,
    so a plain scenario still works in a multi-currency org."""
    if not changes:
        return
    currency = await plan_currency(
        db, org_id=row.org_id, requested=changes.get("currency", row.currency)
    )
    await _check_links(db, org_id=row.org_id, currency=currency, changes=changes)
    for key, value in changes.items():
        setattr(row, key, value)
    row.currency = currency


async def create_goal(db: AsyncSession, *, plan: Scenario, fields: dict[str, Any]) -> PlanGoal:
    """The caller has run ``assert_editable``, as for ``update_goal``."""
    currency = await plan_currency(db, org_id=plan.org_id, requested=plan.currency)
    await _check_links(db, org_id=plan.org_id, currency=currency, changes=fields)
    plan.currency = currency  # a legacy row with a goal is a plan
    # org_id comes from the plan, never from the request.
    goal = PlanGoal(**fields, org_id=plan.org_id, scenario_id=plan.id)
    db.add(goal)
    return goal


async def update_goal(
    db: AsyncSession, *, plan: Scenario, goal: PlanGoal, changes: dict[str, Any]
) -> None:
    currency = await plan_currency(db, org_id=plan.org_id, requested=plan.currency)
    await _check_links(db, org_id=plan.org_id, currency=currency, changes=changes)
    for key, value in changes.items():
        setattr(goal, key, value)


async def delete_goal(db: AsyncSession, *, plan: Scenario, goal: PlanGoal) -> None:
    assert_draft(plan)
    await db.delete(goal)
