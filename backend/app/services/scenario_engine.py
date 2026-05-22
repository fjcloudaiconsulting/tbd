"""Plans simulation engine (analytic baseline + AI stub).

Spec: ``specs/2026-05-22-plans-page-simulation-sandbox.md``.

Architect-locked invariants:

- The engine is the ONLY writer of ``Scenario.projection_json``. It
  has NO ``db.add`` / commit calls that touch ``transactions``,
  ``accounts``, ``budgets``, ``recurring_transactions``, or
  ``forecast_plans``. The sandboxing guard test in
  ``tests/services/test_scenario_engine.py`` pins this with a
  row-count delta assertion.
- ``ScenarioEngine`` is the ABC. ``AnalyticEngine`` is the
  deterministic baseline. ``AIEngine`` is the PR4 stub that raises
  ``NotImplementedError("PR4")`` on simulate; ``analytic`` is the
  default everywhere.
- Output shape is engine-agnostic so the UI doesn't care which engine
  ran. Mirrors ``schemas/scenario.py::ProjectionResult``.
- Math reuses ``app/services/date_utils.py::advance_date`` for
  recurring cadence and reads accounts + recurring through
  ``build_world_state``; the engine itself never touches the DB.
"""
from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app._time import utcnow_naive
from app.models.account import Account
from app.models.recurring import Frequency, RecurringTransaction
from app.models.scenario import Scenario, ScenarioType
from app.services.date_utils import advance_date


_TWOPLACES = Decimal("0.01")


def _q(value: Decimal) -> str:
    """Quantize a Decimal to 2 decimal places and return the JSON string."""
    return str(value.quantize(_TWOPLACES))


# ── Engine contract ─────────────────────────────────────────────────────


@dataclass
class AccountSnapshot:
    """Frozen view of one account at month 0."""

    account_id: int
    account_name: str
    currency: str
    starting_balance: Decimal


@dataclass
class RecurringSnapshot:
    """Frozen view of one active recurring template."""

    id: int
    account_id: int
    amount: Decimal
    type: str  # "income" | "expense"
    frequency: Frequency
    next_due_date: datetime.date


@dataclass
class WorldState:
    """Snapshot of the user's current finances, frozen at simulation time.

    Built BEFORE the engine runs; the engine never queries the DB itself.
    This is what makes the sandboxing guarantee structural rather than
    aspirational: there's no path inside the engine that could mutate
    real tables, because the engine doesn't have a session.
    """

    accounts: list[AccountSnapshot]
    recurring: list[RecurringSnapshot]


@dataclass
class SimulationRequest:
    scenario: Scenario
    state: WorldState
    horizon_months: int
    options: dict[str, Any] = field(default_factory=dict)


# ── Helpers shared by engines ───────────────────────────────────────────


def _month_label(d: datetime.date) -> str:
    """Format a date as "YYYY-MM" for the projection month axis."""
    return f"{d.year:04d}-{d.month:02d}"


def _first_of_next_month(d: datetime.date) -> datetime.date:
    """Return the first day of the month AFTER ``d``."""
    return (d.replace(day=1) + relativedelta(months=1))


def _start_of_horizon() -> datetime.date:
    """Month 0 = the calendar month the simulate call is made in.

    Anchored to "today's" first-of-month so every month-by-month
    projection lines up with calendar months (which is what the UI's
    Recharts axis expects).
    """
    today = utcnow_naive().date()
    return today.replace(day=1)


def _amortized_monthly_payment(
    principal: Decimal,
    annual_rate_pct: Decimal,
    term_months: int,
) -> Decimal:
    """Standard fixed-rate mortgage formula: P * r / (1 - (1+r)^-n).

    Returns 0 when principal is 0; returns ``principal / term_months``
    when the annual rate is 0 (interest-free amortization).
    """
    if principal == 0 or term_months == 0:
        return Decimal("0")
    if annual_rate_pct == 0:
        return (principal / Decimal(term_months)).quantize(_TWOPLACES)
    r = (annual_rate_pct / Decimal("100")) / Decimal("12")
    factor = (Decimal("1") + r) ** term_months
    if factor == 0:
        return Decimal("0")
    payment = (principal * r * factor) / (factor - Decimal("1"))
    return payment.quantize(_TWOPLACES)


# ── Engine base class ───────────────────────────────────────────────────


class ScenarioEngine(ABC):
    """Engine contract. Sync; no DB session.

    The sandboxing guarantee is structural: this class is the only
    type the router instantiates, and its concrete subclasses NEVER
    accept a ``db`` argument. The world state passed in via
    ``SimulationRequest`` is the engine's entire view of reality.
    """

    name: str

    @abstractmethod
    def simulate(self, req: SimulationRequest) -> dict[str, Any]:
        """Run the simulation. Return a JSON-serializable dict matching
        ``schemas/scenario.py::ProjectionResult``.
        """
        raise NotImplementedError


# ── Analytic engine (deterministic baseline) ────────────────────────────


class AnalyticEngine(ScenarioEngine):
    """Deterministic month-by-month projection.

    Algorithm per the spec §"Analytic baseline algorithm":

      For each month m in [1..horizon_months]:
        1. Seed: each account's current balance.
        2. Apply real recurring whose next_due_date falls in m,
           advancing by frequency.
        3. Apply scenario overlays (trip lump-sum, purchase
           amortization, retirement contribution+interest, custom
           events).
        4. Carry forward.
        5. Emit a series point per account per month plus alerts
           on dip-below-zero events.
    """

    name = "analytic_v1"

    def simulate(self, req: SimulationRequest) -> dict[str, Any]:
        scenario = req.scenario
        horizon = req.horizon_months
        state = req.state

        currency = _resolve_report_currency(state, scenario)

        balances: dict[int, Decimal] = {
            a.account_id: Decimal(a.starting_balance) for a in state.accounts
        }
        recurring_queue: list[tuple[RecurringSnapshot, datetime.date]] = [
            (r, r.next_due_date) for r in state.recurring
        ]

        overlays = _build_overlay_events(scenario)

        series_by_account: dict[int, list[dict[str, str]]] = {
            a.account_id: [] for a in state.accounts
        }
        alerts: list[dict[str, Any]] = []

        month_start = _start_of_horizon()
        for m_index in range(horizon):
            month_date = month_start + relativedelta(months=m_index)
            month_end = _first_of_next_month(month_date) - datetime.timedelta(days=1)
            month_label = _month_label(month_date)

            # (1) Apply recurring whose next_due_date falls in [month_date, month_end].
            new_queue: list[tuple[RecurringSnapshot, datetime.date]] = []
            for snap, due in recurring_queue:
                next_due = due
                while next_due <= month_end:
                    if next_due >= month_date and next_due <= month_end:
                        delta = Decimal(snap.amount)
                        if snap.type == "expense":
                            delta = -delta
                        if snap.account_id in balances:
                            balances[snap.account_id] = (
                                balances[snap.account_id] + delta
                            )
                    next_due = advance_date(next_due, snap.frequency)
                new_queue.append((snap, next_due))
            recurring_queue = new_queue

            # (2) Apply scenario overlay events for this month.
            for ev in overlays.get((month_date.year, month_date.month), []):
                account_id = ev["account_id"]
                if account_id not in balances:
                    # Account referenced by the scenario doesn't exist.
                    # Skip silently; the projection just won't reflect it.
                    continue
                delta = Decimal(ev["amount"])
                if ev["kind"] == "expense":
                    delta = -delta
                balances[account_id] = balances[account_id] + delta

                if balances[account_id] < 0:
                    alerts.append(
                        {
                            "account_id": account_id,
                            "month": month_label,
                            "projected_balance": _q(balances[account_id]),
                            "trigger": ev.get("trigger", "scenario_event"),
                            "severity": "warn",
                        }
                    )

            # (3) Emit per-account series points for this month.
            for account_id, balance in balances.items():
                series_by_account[account_id].append(
                    {
                        "month": month_label,
                        "projected_balance": _q(balance),
                    }
                )

        account_index = {a.account_id: a for a in state.accounts}
        per_account_series = [
            {
                "account_id": account_id,
                "account_name": account_index[account_id].account_name,
                "currency": account_index[account_id].currency,
                "points": points,
            }
            for account_id, points in series_by_account.items()
        ]

        verdict = _compute_verdict(state, balances, alerts)
        suggestions = _compute_suggestions(scenario, verdict)

        return {
            "engine_name": self.name,
            "computed_at": utcnow_naive().isoformat(),
            "horizon_months": horizon,
            "currency": currency,
            "per_account_series": per_account_series,
            "alerts": alerts,
            "verdict": verdict,
            "suggestions": suggestions,
        }


# ── AI engine (stub for PR4) ────────────────────────────────────────────


class AIEngine(ScenarioEngine):
    """AI-enhanced engine — STUB for PR1.

    Real implementation lands in PR4 when Team E's AI Tier SDK is
    available. Until then, calling ``simulate`` raises
    ``NotImplementedError("PR4")`` so the engine selector in the
    router stays honest (a misconfigured request gets a hard error,
    not a silent analytic fallback).

    The signature is identical to ``AnalyticEngine.simulate`` so the
    router can swap engines without any other change.
    """

    name = "ai_enhanced"

    def simulate(self, req: SimulationRequest) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError("PR4")


# ── World-state assembler (DB → engine boundary) ────────────────────────


async def build_world_state(
    db: AsyncSession,
    *,
    org_id: int,
    user_id: int,
) -> WorldState:
    """Read accounts + active recurring for the given org and return a
    frozen ``WorldState`` for the engine.

    Pre-launch architect lock: per-user. Plans are private to the
    creator. World state for engine simulation uses the org's accounts
    (every member of an org shares the books) but the per-user
    visibility lives at the router scope, not here.

    No writes; READ ONLY.
    """
    account_rows = (
        await db.execute(
            select(Account).where(
                Account.org_id == org_id,
                Account.is_active.is_(True),
            )
        )
    ).scalars().all()
    accounts = [
        AccountSnapshot(
            account_id=a.id,
            account_name=a.name,
            currency=a.currency,
            starting_balance=Decimal(str(a.balance)),
        )
        for a in account_rows
    ]

    recurring_rows = (
        await db.execute(
            select(RecurringTransaction).where(
                RecurringTransaction.org_id == org_id,
                RecurringTransaction.is_active.is_(True),
            )
        )
    ).scalars().all()
    recurring = [
        RecurringSnapshot(
            id=r.id,
            account_id=r.account_id,
            amount=Decimal(str(r.amount)),
            type=str(r.type),
            frequency=r.frequency,
            next_due_date=r.next_due_date,
        )
        for r in recurring_rows
    ]

    return WorldState(accounts=accounts, recurring=recurring)


# ── Internal helpers ────────────────────────────────────────────────────


def _resolve_report_currency(state: WorldState, scenario: Scenario) -> str:
    """Pick the currency to label the projection with.

    Prefers the scenario's source-account currency where the source
    account exists in the world state; falls back to the first
    account; finally falls back to the scenario's own ``currency``
    field (trip / purchase / retirement) or "EUR".
    """
    params = scenario.params_json or {}
    source_account_id = (
        params.get("source_account_id")
        or params.get("down_payment_account_id")
        or params.get("contribution_account_id")
    )
    if source_account_id is not None:
        for acc in state.accounts:
            if acc.account_id == source_account_id:
                return acc.currency
    if state.accounts:
        return state.accounts[0].currency
    return str(params.get("currency") or "EUR")


def _build_overlay_events(
    scenario: Scenario,
) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """Compile the scenario's params blob into a per-month event map.

    Returned dict shape:
        { (year, month): [ {kind, amount, account_id, trigger} ] }

    Kinds: "expense" subtracts from the account, "income" adds.
    Trigger is a human-readable string used in alerts.
    """
    params = scenario.params_json or {}
    out: dict[tuple[int, int], list[dict[str, Any]]] = {}

    def add(year: int, month: int, ev: dict[str, Any]) -> None:
        out.setdefault((year, month), []).append(ev)

    stype = scenario.scenario_type.value if hasattr(scenario.scenario_type, "value") else str(scenario.scenario_type)

    if stype == ScenarioType.TRIP.value:
        start_str = params.get("start_date")
        if not start_str:
            return out
        start = _parse_date(start_str)
        if start is None:
            return out
        duration = int(params.get("duration_days", 0) or 0)
        transport = Decimal(str(params.get("transport_cost", "0") or "0"))
        accom_per_night = Decimal(str(params.get("accommodation_per_night", "0") or "0"))
        daily_budget = Decimal(str(params.get("daily_budget", "0") or "0"))
        extras = params.get("one_off_extras") or []
        extras_total = sum(
            (Decimal(str(e.get("amount", "0") or "0")) for e in extras),
            Decimal("0"),
        )
        lump = (
            transport
            + accom_per_night * Decimal(duration)
            + daily_budget * Decimal(duration)
            + extras_total
        )
        source_id = params.get("source_account_id")
        if source_id is not None and lump > 0:
            add(start.year, start.month, {
                "kind": "expense",
                "amount": lump,
                "account_id": int(source_id),
                "trigger": "trip_lump_sum",
            })

    elif stype == ScenarioType.PURCHASE.value:
        target_str = params.get("target_date")
        if not target_str:
            return out
        target = _parse_date(target_str)
        if target is None:
            return out
        down_payment = Decimal(str(params.get("down_payment", "0") or "0"))
        total_price = Decimal(str(params.get("total_price", "0") or "0"))
        dp_account = params.get("down_payment_account_id")
        financing = params.get("financing")
        if financing is None:
            if dp_account is not None and total_price > 0:
                add(target.year, target.month, {
                    "kind": "expense",
                    "amount": total_price,
                    "account_id": int(dp_account),
                    "trigger": "purchase_cash",
                })
        else:
            if dp_account is not None and down_payment > 0:
                add(target.year, target.month, {
                    "kind": "expense",
                    "amount": down_payment,
                    "account_id": int(dp_account),
                    "trigger": "purchase_down_payment",
                })
            principal = Decimal(str(financing.get("principal", "0") or "0"))
            rate = Decimal(str(financing.get("annual_rate_pct", "0") or "0"))
            term = int(financing.get("term_months", 0) or 0)
            first_str = financing.get("first_payment_date")
            payment_account = financing.get("payment_account_id")
            first = _parse_date(first_str) if first_str else None
            if (
                first is not None
                and payment_account is not None
                and principal > 0
                and term > 0
            ):
                monthly = _amortized_monthly_payment(principal, rate, term)
                for i in range(term):
                    when = first + relativedelta(months=i)
                    add(when.year, when.month, {
                        "kind": "expense",
                        "amount": monthly,
                        "account_id": int(payment_account),
                        "trigger": "purchase_amortized",
                    })

    elif stype == ScenarioType.RETIREMENT.value:
        # PR1 minimal: post the monthly contribution into the
        # contribution account starting "this month" through the
        # horizon. Full retirement engine (compound interest +
        # contribution curve) lands in PR2.
        contribution = Decimal(str(params.get("monthly_contribution", "0") or "0"))
        contrib_account = params.get("contribution_account_id")
        target_str = params.get("target_retirement_date")
        target = _parse_date(target_str) if target_str else None
        if contrib_account is not None and contribution > 0:
            start = _start_of_horizon()
            stop = target if target else (start + relativedelta(years=40))
            cursor = start
            while cursor <= stop:
                add(cursor.year, cursor.month, {
                    "kind": "income",
                    "amount": contribution,
                    "account_id": int(contrib_account),
                    "trigger": "retirement_contribution",
                })
                cursor = cursor + relativedelta(months=1)

    elif stype == ScenarioType.CUSTOM.value:
        # PR1 minimal: custom events not replayed yet. Full event
        # replay (income_off, expense_off, recurring_on, etc.) is PR2.
        pass

    return out


def _parse_date(value: Any) -> Optional[datetime.date]:
    """Parse an ISO date string (or pass through a date) defensively."""
    if value is None:
        return None
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        return None


def _compute_verdict(
    state: WorldState,
    final_balances: dict[int, Decimal],
    alerts: list[dict[str, Any]],
) -> dict[str, str]:
    """Architect-locked verdict thresholds:

    - Green: no account dipped below zero.
    - Yellow: any account dipped below zero but recoverably.
    - Red: an account dipped below zero by 10%+ of its starting balance.
    """
    if not alerts:
        return {
            "color": "green",
            "headline": "All accounts stay above zero across the horizon.",
            "reason": "No projected dips below zero.",
        }

    starting_by_id = {a.account_id: a.starting_balance for a in state.accounts}
    severe = False
    for alert in alerts:
        starting = starting_by_id.get(alert["account_id"], Decimal("0"))
        if starting <= 0:
            severe = True
            break
        dip = Decimal(alert["projected_balance"])
        if dip < -(starting / Decimal("10")):
            severe = True
            break

    if severe:
        return {
            "color": "red",
            "headline": "Projection dips significantly below zero.",
            "reason": (
                "At least one account drops more than 10% of its "
                "starting balance below zero in this scenario."
            ),
        }

    return {
        "color": "yellow",
        "headline": "Plan is feasible but cuts close.",
        "reason": (
            "At least one account briefly dips below zero before "
            "recovering. Consider shifting dates or reducing amounts."
        ),
    }


def _compute_suggestions(
    scenario: Scenario,
    verdict: dict[str, str],
) -> list[dict[str, Any]]:
    """Tiny suggestion set for PR1.

    Full suggestion engine (model-judged or rule-driven by overlay
    type) lands in PR2 + PR4. PR1 ships two canned hints for any
    yellow / red trip or purchase scenario so the UI has something
    to render under the chart.
    """
    if verdict["color"] == "green":
        return []
    stype = (
        scenario.scenario_type.value
        if hasattr(scenario.scenario_type, "value")
        else str(scenario.scenario_type)
    )
    if stype == ScenarioType.TRIP.value:
        return [
            {
                "action": "shift_start_date",
                "by_days": 30,
                "expected_outcome": (
                    "Move the trip a month later so the lump-sum lands "
                    "after one more pay cycle."
                ),
            },
            {
                "action": "reduce_daily_budget",
                "by_amount": "10.00",
                "expected_outcome": (
                    "Trim the daily budget to bring the trip total under "
                    "the dip threshold."
                ),
            },
        ]
    if stype == ScenarioType.PURCHASE.value:
        return [
            {
                "action": "increase_down_payment",
                "by_amount": "1000.00",
                "expected_outcome": (
                    "A larger down payment shrinks the financed principal "
                    "and the monthly payment."
                ),
            },
            {
                "action": "extend_term_months",
                "by_amount": "12",
                "expected_outcome": (
                    "Longer term lowers the monthly payment at the cost of "
                    "more total interest."
                ),
            },
        ]
    return []


# ── Engine selector ─────────────────────────────────────────────────────


_ENGINE_REGISTRY: dict[str, type[ScenarioEngine]] = {
    "analytic": AnalyticEngine,
    "ai_enhanced": AIEngine,
}


def get_engine(engine_name: str) -> ScenarioEngine:
    """Return a ScenarioEngine instance for the given engine name.

    Raises ``KeyError`` for unknown engine names — the request schema's
    Literal["analytic", "ai_enhanced"] gate makes that unreachable in
    practice but the registry remains the source of truth.
    """
    cls = _ENGINE_REGISTRY[engine_name]
    return cls()
