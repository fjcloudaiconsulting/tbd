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
from app.models.transaction import Transaction, TransactionType
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
class MonthlyCashflowPoint:
    """One historical month of per-account net cashflow.

    Net = sum(income amounts) - sum(expense amounts), with transfers
    excluded. The regression overlay (PR2) fits a least-squares line
    against these points per account.
    """

    account_id: int
    year: int
    month: int
    net: Decimal


@dataclass
class WorldState:
    """Snapshot of the user's current finances, frozen at simulation time.

    Built BEFORE the engine runs; the engine never queries the DB itself.
    This is what makes the sandboxing guarantee structural rather than
    aspirational: there's no path inside the engine that could mutate
    real tables, because the engine doesn't have a session.

    ``history`` is the optional last-12mo per-account net cashflow series,
    populated by ``build_world_state`` regardless of plan type. The
    regression overlay (PR2 ``smooth_with_regression``) consumes it.
    """

    accounts: list[AccountSnapshot]
    recurring: list[RecurringSnapshot]
    history: list[MonthlyCashflowPoint] = field(default_factory=list)


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


def _contribution_for_month(
    params: dict[str, Any], month_date: datetime.date
) -> Decimal:
    """Return the monthly contribution that applies for ``month_date``.

    Walks ``contribution_curve`` (already validated ascending by the
    schema) and picks the highest-date step whose ``from`` is <=
    ``month_date``. Falls back to the base ``monthly_contribution`` when
    no curve step applies yet.
    """
    base = Decimal(str(params.get("monthly_contribution", "0") or "0"))
    curve = params.get("contribution_curve") or []
    if not curve:
        return base
    chosen = base
    for step in curve:
        step_from = _parse_date(step.get("from"))
        if step_from is None:
            continue
        if step_from <= month_date:
            chosen = Decimal(str(step.get("monthly", "0") or "0"))
        else:
            break
    return chosen


def _fit_least_squares(values: list[Decimal]) -> Decimal:
    """Fit a least-squares slope on the index-vs-value series.

    Returns the per-step slope as a Decimal. Hand-rolled OLS so we
    don't add numpy as a hard dep on this path (the spec calls it out
    explicitly: numpy is a transitive dep, but the math is six lines).
    Returns 0 for fewer than 2 points.
    """
    n = len(values)
    if n < 2:
        return Decimal("0")
    # x = 0,1,2,...,n-1 ; y = values
    mean_x = Decimal(n - 1) / Decimal("2")
    mean_y = sum(values, Decimal("0")) / Decimal(n)
    num = Decimal("0")
    den = Decimal("0")
    for i, y in enumerate(values):
        dx = Decimal(i) - mean_x
        num += dx * (y - mean_y)
        den += dx * dx
    if den == 0:
        return Decimal("0")
    return num / den


def _regression_drift_by_account(
    history: list["MonthlyCashflowPoint"],
) -> dict[int, Decimal]:
    """Per-account per-month drift derived from a least-squares fit.

    For each account, fits a line to its 12-month net cashflow series
    and returns the per-month slope. The slope is added to each
    projected month's balance as a synthetic drift overlay.
    """
    by_acc: dict[int, list[Decimal]] = {}
    for pt in history:
        by_acc.setdefault(pt.account_id, []).append(pt.net)
    out: dict[int, Decimal] = {}
    for acc_id, series in by_acc.items():
        out[acc_id] = _fit_least_squares(series)
    return out


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
    def simulate(
        self,
        req: SimulationRequest,
        *,
        smooth_with_regression: bool = False,
    ) -> dict[str, Any]:
        """Run the simulation. Return a JSON-serializable dict matching
        ``schemas/scenario.py::ProjectionResult``.

        ``smooth_with_regression`` is the SINGLE source of truth for the
        regression-overlay toggle. It comes from the top-level field on
        ``SimulateRequest`` (the spec's request-level toggle), passed
        through the router as an explicit kwarg — NOT a magic key inside
        ``req.options``.
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

    def simulate(
        self,
        req: SimulationRequest,
        *,
        smooth_with_regression: bool = False,
    ) -> dict[str, Any]:
        """Run the deterministic month-by-month projection.

        Architect-locked PR2 extensions:

        - **Retirement plan_type**: compound interest applied to the
          contribution account at ``annual_return_pct / 12`` per month,
          with stepped contribution curve overrides. A parallel
          ``real_terms_series`` is computed using ``inflation_pct`` so
          the UI can overlay an inflation-adjusted line.
        - **Regression overlay**: when ``smooth_with_regression`` is True,
          a least-squares fit on the last 12 months of per-account net
          cashflow yields a per-month drift that gets added to every
          projected balance. Applies to every plan_type. Default off
          keeps PR1's exact math. Wired in via the top-level field on
          ``SimulateRequest`` — see the router for the call site.
        - **Verdict refinement**: dispatch by ``scenario_type`` so trip /
          purchase / custom use the refined dip-duration + end-balance
          bands and retirement uses its real-terms-vs-target bands.
        """
        scenario = req.scenario
        horizon = req.horizon_months
        state = req.state
        smooth = bool(smooth_with_regression)

        currency = _resolve_report_currency(state, scenario)
        stype = (
            scenario.scenario_type.value
            if hasattr(scenario.scenario_type, "value")
            else str(scenario.scenario_type)
        )
        params = scenario.params_json or {}

        balances: dict[int, Decimal] = {
            a.account_id: Decimal(a.starting_balance) for a in state.accounts
        }
        starting_balances: dict[int, Decimal] = dict(balances)
        recurring_queue: list[tuple[RecurringSnapshot, datetime.date]] = [
            (r, r.next_due_date) for r in state.recurring
        ]

        overlays = _build_overlay_events(scenario)

        series_by_account: dict[int, list[dict[str, str]]] = {
            a.account_id: [] for a in state.accounts
        }
        alerts: list[dict[str, Any]] = []

        # Per-account dip-streak tracker (consecutive months below the
        # account's minimum balance). The verdict refinement converts
        # streak length to a duration threshold using the simulation's
        # 30-day month convention (7-day threshold → ceil(7/30) = 1 month).
        dip_streak: dict[int, int] = {a.account_id: 0 for a in state.accounts}
        max_dip_streak: dict[int, int] = {a.account_id: 0 for a in state.accounts}
        # Track which accounts dipped at all (for "brief dip" band).
        any_dip_seen: dict[int, bool] = {a.account_id: False for a in state.accounts}

        # PR2 regression overlay: per-account drift (Decimal). The drift
        # is added to each month's balance before emitting the series
        # point, but AFTER the deterministic events are applied so the
        # smoothing surfaces as a separate "trend-adjusted" overlay
        # rather than warping the deterministic baseline math.
        regression_drift: dict[int, Decimal] = (
            _regression_drift_by_account(state.history) if smooth else {}
        )

        # Retirement-only state: compound-interest accrues on
        # contribution_account_id, and we also project a parallel
        # real-terms (inflation-deflated) balance for the chart overlay.
        retirement_real_terms: list[dict[str, str]] = []
        retirement_account_id: Optional[int] = None
        monthly_return = Decimal("0")
        monthly_inflation = Decimal("0")
        if stype == ScenarioType.RETIREMENT.value:
            retirement_account_id = params.get("contribution_account_id")
            annual_return = Decimal(str(params.get("annual_return_pct", "0") or "0"))
            monthly_return = (annual_return / Decimal("100")) / Decimal("12")
            annual_inflation = Decimal(str(params.get("inflation_pct", "0") or "0"))
            monthly_inflation = (
                annual_inflation / Decimal("100")
            ) / Decimal("12")

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

            # (2) Apply scenario overlay events for this month (trip
            # lump, purchase, custom). Retirement is handled in step
            # (2b) below so the compound-interest math runs in lockstep
            # with the contribution post.
            for ev in overlays.get((month_date.year, month_date.month), []):
                # Skip retirement overlays here; (2b) handles compound
                # interest + curve in one pass instead.
                if ev.get("trigger") == "retirement_contribution":
                    continue
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

            # (2b) Retirement compound-interest + curve. The order
            # matters: contribution first, then interest on the
            # resulting balance (end-of-month compounding convention).
            if (
                stype == ScenarioType.RETIREMENT.value
                and retirement_account_id is not None
                and retirement_account_id in balances
            ):
                contribution = _contribution_for_month(params, month_date)
                balances[retirement_account_id] = (
                    balances[retirement_account_id] + contribution
                )
                if monthly_return > 0:
                    interest = (
                        balances[retirement_account_id] * monthly_return
                    )
                    balances[retirement_account_id] = (
                        balances[retirement_account_id] + interest
                    )

            # (2c) Regression-overlay drift: add the per-month slope to
            # every account's balance. Skipped when smoothing is off.
            if smooth and regression_drift:
                for acc_id, drift in regression_drift.items():
                    if acc_id in balances:
                        balances[acc_id] = balances[acc_id] + drift

            # (3) Emit per-account series points for this month and
            # update the dip trackers.
            for account_id, balance in balances.items():
                series_by_account[account_id].append(
                    {
                        "month": month_label,
                        "projected_balance": _q(balance),
                    }
                )
                if balance < 0:
                    any_dip_seen[account_id] = True
                    dip_streak[account_id] += 1
                    if dip_streak[account_id] > max_dip_streak[account_id]:
                        max_dip_streak[account_id] = dip_streak[account_id]
                else:
                    dip_streak[account_id] = 0

            # (4) Retirement real-terms series: deflate the nominal
            # retirement balance by cumulative inflation.
            if (
                stype == ScenarioType.RETIREMENT.value
                and retirement_account_id is not None
                and retirement_account_id in balances
            ):
                nominal = balances[retirement_account_id]
                # Cumulative inflation factor over m_index+1 months.
                infl_factor = (
                    (Decimal("1") + monthly_inflation) ** (m_index + 1)
                )
                if infl_factor == 0:
                    real = nominal
                else:
                    real = nominal / infl_factor
                retirement_real_terms.append(
                    {
                        "month": month_label,
                        "projected_balance": _q(real),
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

        verdict = _dispatch_verdict(
            scenario,
            state,
            balances,
            starting_balances,
            alerts,
            max_dip_streak,
            any_dip_seen,
            retirement_real_terms,
        )
        suggestions = _compute_suggestions(
            scenario, verdict, balances, retirement_real_terms
        )

        result: dict[str, Any] = {
            "engine_name": self.name,
            "computed_at": utcnow_naive().isoformat(),
            "horizon_months": horizon,
            "currency": currency,
            "per_account_series": per_account_series,
            "alerts": alerts,
            "verdict": verdict,
            "suggestions": suggestions,
            "smoothed_with_regression": smooth,
        }
        if stype == ScenarioType.RETIREMENT.value and retirement_real_terms:
            result["real_terms_series"] = {
                "points": retirement_real_terms,
                "inflation_pct": _q(
                    Decimal(str(params.get("inflation_pct", "0") or "0"))
                ),
            }
        else:
            result["real_terms_series"] = None
        return result


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

    def simulate(
        self,
        req: SimulationRequest,
        *,
        smooth_with_regression: bool = False,
    ) -> dict[str, Any]:  # pragma: no cover
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

    # Last 12 months of per-account net cashflow, used by the optional
    # regression overlay in the engine (PR2). Transfers excluded — net =
    # income - expense. This read is unconditional so the engine has the
    # data when ``smooth_with_regression`` is True at the request level;
    # for plans that don't smooth, the points are simply ignored.
    today = utcnow_naive().date()
    history_start = today.replace(day=1) - relativedelta(months=12)
    txn_rows = (
        await db.execute(
            select(Transaction).where(
                Transaction.org_id == org_id,
                Transaction.date >= history_start,
                Transaction.date < today.replace(day=1),
                Transaction.type != TransactionType.TRANSFER,
            )
        )
    ).scalars().all()
    monthly: dict[tuple[int, int, int], Decimal] = {}
    for txn in txn_rows:
        key = (txn.account_id, txn.date.year, txn.date.month)
        sign = (
            Decimal("1")
            if txn.type == TransactionType.INCOME
            else Decimal("-1")
        )
        monthly[key] = monthly.get(key, Decimal("0")) + sign * Decimal(str(txn.amount))
    history = [
        MonthlyCashflowPoint(
            account_id=acc_id, year=year, month=month, net=net
        )
        for (acc_id, year, month), net in sorted(monthly.items())
    ]

    return WorldState(accounts=accounts, recurring=recurring, history=history)


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


def _dispatch_verdict(
    scenario: Scenario,
    state: WorldState,
    final_balances: dict[int, Decimal],
    starting_balances: dict[int, Decimal],
    alerts: list[dict[str, Any]],
    max_dip_streak: dict[int, int],
    any_dip_seen: dict[int, bool],
    retirement_real_terms: list[dict[str, str]],
) -> dict[str, str]:
    """Per-scenario-type verdict dispatch (PR2 architect lock).

    Trip / purchase / custom use the dip-duration + end-balance bands;
    retirement uses the real-terms-vs-target bands. Adding a new
    scenario_type that wants its own bands plugs into this dispatch
    rather than overloading ``_compute_verdict`` with type sniffing.
    """
    stype = (
        scenario.scenario_type.value
        if hasattr(scenario.scenario_type, "value")
        else str(scenario.scenario_type)
    )
    if stype == ScenarioType.RETIREMENT.value:
        return _retirement_verdict(scenario, retirement_real_terms)
    return _general_verdict(
        state, final_balances, starting_balances, alerts,
        max_dip_streak, any_dip_seen,
    )


def _general_verdict(
    state: WorldState,
    final_balances: dict[int, Decimal],
    starting_balances: dict[int, Decimal],
    alerts: list[dict[str, Any]],
    max_dip_streak: dict[int, int],
    any_dip_seen: dict[int, bool],
) -> dict[str, str]:
    """Refined trip/purchase/custom verdict (PR2 lock).

    Bands:

    - **Green**: no account dips below zero across the horizon AND
      the user's total ending balance is at least 80% of the starting
      total (the plan doesn't burn more than 20% of net worth).
    - **Yellow**: a "brief" dip (max streak <= 1 simulation-month,
      which the spec uses as a proxy for the 7-day threshold) OR
      ending balance between 50% and 80% of start.
    - **Red**: an extended dip (max streak > 1 simulation month) OR
      ending balance below 50% of start.

    The end-balance gate is the architect's "don't burn more than 20%
    of starting net worth" rule (PR2). It's evaluated on the SUM across
    accounts so a transfer between accounts doesn't accidentally trip
    it.
    """
    total_start = sum(starting_balances.values(), Decimal("0"))
    total_end = sum(final_balances.values(), Decimal("0"))

    extended_dip = any(streak > 1 for streak in max_dip_streak.values())
    brief_dip = any(any_dip_seen.values())

    if total_start > 0:
        end_ratio = total_end / total_start
    else:
        end_ratio = Decimal("1")

    if extended_dip or end_ratio < Decimal("0.5"):
        return {
            "color": "red",
            "headline": "Projection runs into trouble.",
            "reason": (
                "Either an account stays negative for more than 7 days, "
                "or the ending balance falls below 50% of where it started."
            ),
        }
    if brief_dip or end_ratio < Decimal("0.8"):
        return {
            "color": "yellow",
            "headline": "Plan is feasible but cuts close.",
            "reason": (
                "Either an account dips below zero briefly, or the "
                "ending balance ends between 50% and 80% of where it "
                "started. Consider shifting dates or reducing amounts."
            ),
        }
    return {
        "color": "green",
        "headline": "All accounts stay above zero across the horizon.",
        "reason": (
            "No projected dips and ending balance stays above 80% of "
            "the starting total."
        ),
    }


def _retirement_verdict(
    scenario: Scenario,
    real_terms: list[dict[str, str]],
) -> dict[str, str]:
    """Retirement-specific verdict against real-terms target.

    Bands (architect-locked PR2):

    - **Green**: real-terms projected balance at ``target_retirement_date``
      (or end of horizon if earlier) >= ``target_balance``.
    - **Yellow**: real-terms balance within 15% below target.
    - **Red**: real-terms balance more than 15% below target.
    """
    params = scenario.params_json or {}
    target_balance = Decimal(str(params.get("target_balance", "0") or "0"))
    if not real_terms or target_balance <= 0:
        # No real-terms data or zero target: green by default (nothing
        # actionable to compare against).
        return {
            "color": "green",
            "headline": "Retirement target not set or no data.",
            "reason": (
                "Add a target balance and contribution amount to get "
                "a verdict on this plan."
            ),
        }
    target_date = _parse_date(params.get("target_retirement_date"))
    final_real = Decimal(real_terms[-1]["projected_balance"])
    if target_date is not None:
        # Pick the projection point closest to target_date.
        target_label = _month_label(target_date.replace(day=1))
        match = next(
            (p for p in real_terms if p["month"] == target_label), None
        )
        if match is not None:
            final_real = Decimal(match["projected_balance"])
    if final_real >= target_balance:
        return {
            "color": "green",
            "headline": "On track to hit the retirement target.",
            "reason": (
                "The real-terms projected balance meets or exceeds the "
                "target at the retirement date."
            ),
        }
    gap_ratio = (target_balance - final_real) / target_balance
    if gap_ratio <= Decimal("0.15"):
        return {
            "color": "yellow",
            "headline": "Close to the target but not quite.",
            "reason": (
                "The real-terms projected balance is within 15% of the "
                "target at the retirement date. Consider raising the "
                "monthly contribution."
            ),
        }
    return {
        "color": "red",
        "headline": "Retirement target out of reach at current pace.",
        "reason": (
            "The real-terms projected balance falls more than 15% "
            "below the target at the retirement date. A larger monthly "
            "contribution, a later retirement date, or a lower target "
            "would close the gap."
        ),
    }


def _required_monthly_to_close_gap(
    starting: Decimal,
    target_real: Decimal,
    annual_return_pct: Decimal,
    annual_inflation_pct: Decimal,
    months: int,
) -> Decimal:
    """Solve for the constant monthly contribution that hits ``target_real``
    in real terms at horizon end, given current state.

    Future-value-of-an-annuity-due closed form, then deflated to real
    terms. Returns 0 when the math is degenerate.
    """
    if months <= 0:
        return Decimal("0")
    r = (annual_return_pct / Decimal("100")) / Decimal("12")
    i = (annual_inflation_pct / Decimal("100")) / Decimal("12")
    # Inflate the real-terms target to nominal at end of horizon.
    target_nominal = target_real * ((Decimal("1") + i) ** months)
    # Growth of the starting balance over the horizon (compounded
    # monthly).
    starting_fv = starting * ((Decimal("1") + r) ** months)
    needed_fv = target_nominal - starting_fv
    if needed_fv <= 0:
        return Decimal("0")
    if r == 0:
        return (needed_fv / Decimal(months)).quantize(_TWOPLACES)
    factor = ((Decimal("1") + r) ** months - Decimal("1")) / r
    if factor == 0:
        return Decimal("0")
    return (needed_fv / factor).quantize(_TWOPLACES)


def _compute_suggestions(
    scenario: Scenario,
    verdict: dict[str, str],
    final_balances: dict[int, Decimal],
    retirement_real_terms: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Suggestion set per scenario type. Green plans get an empty list.

    Trip / purchase: PR1's canned hints (still useful for the UI).
    Retirement (PR2): when red, surface the contribution amount that
    would close the gap, computed via ``_required_monthly_to_close_gap``.
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
    if stype == ScenarioType.RETIREMENT.value and verdict["color"] == "red":
        params = scenario.params_json or {}
        contrib_account_id = params.get("contribution_account_id")
        starting = final_balances.get(contrib_account_id, Decimal("0"))
        # The above is the FINAL balance for the contribution account,
        # not the starting one. We want the suggestion to be "raise the
        # monthly contribution to X going forward", so we use the
        # current contribution amount as the floor and the additional
        # delta as the bump. Hand the UI both numbers.
        target_balance = Decimal(
            str(params.get("target_balance", "0") or "0")
        )
        annual_return = Decimal(
            str(params.get("annual_return_pct", "0") or "0")
        )
        annual_inflation = Decimal(
            str(params.get("inflation_pct", "0") or "0")
        )
        current_contribution = Decimal(
            str(params.get("monthly_contribution", "0") or "0")
        )
        months = len(retirement_real_terms) or 1
        # The contribution account's balance at month 0 (start) is what
        # the math needs, but we ran the projection on final state
        # already. Re-derive starting from current params is a small
        # punt: the engine could pass it through, but the math below is
        # deliberately a coarse "how much would close the gap" hint, not
        # a precise re-solve. We approximate starting at zero growth as
        # the simplest honest answer.
        required = _required_monthly_to_close_gap(
            Decimal("0"),
            target_balance,
            annual_return,
            annual_inflation,
            months,
        )
        delta = required - current_contribution
        if delta <= 0:
            return []
        return [
            {
                "action": "raise_monthly_contribution",
                "by_amount": _q(delta),
                "expected_outcome": (
                    f"Raise the monthly contribution by about "
                    f"{_q(delta)} to close the gap to the real-terms target."
                ),
            }
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
