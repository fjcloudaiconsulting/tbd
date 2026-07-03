"""LAI.3 — Smart Budget Rebalance service.

The service inspects the org's CURRENT period budgets and the prior 3
months of actual settled expenses per master category. It builds a
**redacted, aggregated** prompt (per-category monthly totals — never raw
transaction rows) and asks the LLM via
``ai_dispatch.call_llm_structured`` for a set of per-category budget
deltas with reasoning.

Hard rules baked into this layer:

1. **Suggestion only.** This service never mutates the budget table.
   The router returns deltas; the frontend collects user accept / skip
   per row and re-uses the existing ``PUT /api/v1/budgets/{id}`` to
   apply changes.

2. **Category whitelist.** The LLM is told the closed set of
   ``category_id`` values from the org's current-period budgets. Any
   response containing a ``category_id`` outside that set is rejected
   (defense-in-depth against prompt injection / LLM drift).

3. **Aggregates only.** The prompt carries
   ``{category_name, budget_amount, last_3mo_avg_actual, current_mo_actual}``
   per category. No transaction descriptions, no merchant names, no
   account ids, no user ids leave this layer.

4. **Friendly empty states.** No budgets, no actuals, or LLM
   unavailable all map to a typed ``status`` in the response so the UI
   can show an empty-state message instead of crashing.
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import structlog
from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models.budget import Budget
from app.models.category import Category
from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.schemas.budget_rebalance import (
    BudgetDeltaSuggestion,
    BudgetRebalanceResponse,
)
from app.services.ai_dispatch import (
    AICapabilityNotSupported,
    AICapExceeded,
    AIDispatchFailed,
    NoRoutingConfigured,
    call_llm_structured,
)
from app.services.ai_providers import (
    NativeNotAvailable,
    StructuredOutputError,
)
from app.services.billing_service import get_current_period
from app.services.transaction_filters import (
    effective_period_date_expr,
    reportable_transaction_filter,
)


logger = structlog.stdlib.get_logger()


# The dispatch feature_key MUST be the routable name (``smart_budget``), NOT the
# entitlement key (``ai.budget``) — same convention as forecast (``smart_forecast``)
# and categorize (``categorize_transactions``). ``call_llm_structured`` uses this
# for routing lookup + cap/ledger accounting; using the entitlement key here would
# silently ignore a per-feature ``smart_budget`` routing override and disagree with
# the /ai/status gating signal. The 403 entitlement gate (``ai.budget``) lives in
# the router via ``require_feature``.
ROUTING_KEY = "smart_budget"


# The structured-output schema the LLM must satisfy. The model returns a
# PRIORITY ORDERING + narrative only — never amounts. The deterministic
# allocator (``_allocate_rebalance``) computes every money movement, so the
# LLM cannot inflate or deflate the total. ``_parse_ai_guidance`` defensively
# re-validates the shape and drops any ids outside the closed set.
LLM_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "required": ["priority", "summary"],
    "properties": {
        "summary": {"type": "string"},
        "priority": {"type": "array", "items": {"type": "integer"}},
        "reasoning": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["category_id", "text"],
                "properties": {
                    "category_id": {"type": "integer"},
                    "text": {"type": "string"},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class _CategoryFact:
    category_id: int
    category_name: str
    budget_amount: Decimal
    last_3mo_total: Decimal
    last_3mo_avg: Decimal
    current_mo_actual: Decimal


# Money quantum for every rebalance computation.
CENT = Decimal("0.01")


def _project_period_spend(fact: "_CategoryFact") -> Decimal:
    """Conservative projected full-period spend for one category.

    ``max(current_mo_actual, last_3mo_avg)``: a category already pacing
    above its 3-month average is projected at the higher run-rate, so the
    allocator never frees money a category is on track to need. Early in a
    period, ``current_mo_actual`` is small and the 3-month average drives
    the projection.
    """
    return max(fact.current_mo_actual, fact.last_3mo_avg).quantize(CENT)


def _allocate_rebalance(
    facts: list["_CategoryFact"],
    priority_ids: list[int],
    reasoning_by_cat: Optional[dict[int, str]] = None,
) -> tuple[list[BudgetDeltaSuggestion], Decimal]:
    """Move available surplus into deficits, conserving the total.

    Returns ``(suggestions_for_changed_rows, uncovered_overspend)``. The
    sum of new amounts over ALL facts equals the sum of budgets over all
    facts (zero-sum by construction): we only ever move money that a
    category is projected NOT to need into categories projected to
    overspend. ``priority_ids`` orders which deficits are covered first
    (essential bills before discretionary); any deficit the AI did not
    name is appended largest-need-first. ``reasoning_by_cat`` supplies
    per-category narrative; missing entries get a deterministic default.
    """
    reasoning_by_cat = reasoning_by_cat or {}
    proj = {f.category_id: _project_period_spend(f) for f in facts}

    headroom: dict[int, Decimal] = {}  # cid -> positive surplus available
    deficit: dict[int, Decimal] = {}  # cid -> positive need
    for f in facts:
        s = (f.budget_amount - proj[f.category_id]).quantize(CENT)
        if s > 0:
            headroom[f.category_id] = s
        elif s < 0:
            deficit[f.category_id] = -s

    total_headroom = sum(headroom.values(), Decimal("0"))
    total_deficit = sum(deficit.values(), Decimal("0"))
    movable = min(total_headroom, total_deficit).quantize(CENT)
    uncovered = (total_deficit - movable).quantize(CENT)

    # --- pull `movable` from headroom categories, proportional to each
    # category's surplus, via largest-remainder (Hamilton) apportionment
    # in integer cents. Working in cents keeps every giver's share within
    # [0, its own headroom] AND makes sum(given) == movable exactly. A
    # naive "float the rounding residual onto the last giver" approach can
    # push that giver below its projected need (over-giving) or even hand
    # it a negative give (raising a surplus category's budget), because the
    # residual is unbounded; apportionment never does. ---
    given: dict[int, Decimal] = {}
    if total_headroom > 0 and movable > 0:
        movable_c = int((movable / CENT).to_integral_value())
        total_hr_c = int((total_headroom / CENT).to_integral_value())
        hids = list(headroom.keys())
        floors: dict[int, int] = {}
        remainders: dict[int, int] = {}
        for cid in hids:
            hr_c = int((headroom[cid] / CENT).to_integral_value())
            num = movable_c * hr_c
            floors[cid] = num // total_hr_c
            remainders[cid] = num % total_hr_c
        # Distribute the leftover cents to the largest fractional
        # remainders (ties broken by lowest cid, for determinism). There
        # are always strictly more positive remainders than leftover
        # cents, so every +1 lands on a category with headroom to spare.
        leftover = movable_c - sum(floors.values())
        for cid in sorted(hids, key=lambda c: (-remainders[c], c))[:leftover]:
            floors[cid] += 1
        for cid in hids:
            given[cid] = (Decimal(floors[cid]) * CENT).quantize(CENT)

    # --- distribute `movable` to deficits in priority order (waterfall) ---
    received: dict[int, Decimal] = {}
    ordered = [cid for cid in priority_ids if cid in deficit]
    # Append any deficit the AI did not name, largest-need first.
    for cid in sorted(deficit, key=lambda c: deficit[c], reverse=True):
        if cid not in ordered:
            ordered.append(cid)
    remaining = movable
    for cid in ordered:
        if remaining <= 0:
            break
        take = min(remaining, deficit[cid]).quantize(CENT)
        received[cid] = take
        remaining -= take

    # --- build suggestions for changed rows only ---
    suggestions: list[BudgetDeltaSuggestion] = []
    for f in facts:
        cid = f.category_id
        new_amount = f.budget_amount
        if cid in given:
            new_amount = (f.budget_amount - given[cid]).quantize(CENT)
        elif cid in received:
            new_amount = (f.budget_amount + received[cid]).quantize(CENT)
        delta = (new_amount - f.budget_amount).quantize(CENT)
        if delta == 0:
            continue
        default_reason = (
            f"Freeing {-delta:.2f} of projected surplus"
            if delta < 0
            else f"Covering {delta:.2f} of projected overspend"
        )
        suggestions.append(
            BudgetDeltaSuggestion(
                category_id=cid,
                category_name=f.category_name,
                current_amount=f.budget_amount,
                suggested_amount=new_amount,
                delta_amount=delta,
                reasoning=(reasoning_by_cat.get(cid) or default_reason)[:400],
            )
        )

    return suggestions, uncovered


async def _gather_facts(
    db: AsyncSession,
    *,
    org_id: int,
    period_start: datetime.date,
    period_end: Optional[datetime.date],
) -> list[_CategoryFact]:
    """Aggregate per-master-category facts for the prompt.

    Returns one row per current-period budget. Includes the prior 3
    months' total + average + this month's running actual. Aggregates
    only — no transaction-level data leaves this function.
    """
    # Eager-load the master category so we don't fire N+1 refreshes
    # row-by-row below. selectinload issues one extra IN-query for all
    # categories at once.
    budget_rows = (
        await db.execute(
            select(Budget)
            .where(
                Budget.org_id == org_id,
                Budget.period_start == period_start,
            )
            .order_by(Budget.category_id)
            .options(selectinload(Budget.category))
        )
    ).scalars().all()
    if not budget_rows:
        return []

    cat_ids = [b.category_id for b in budget_rows]
    budgeted_cat_ids = set(cat_ids)

    sub_rows = (
        await db.execute(
            select(Category.id, Category.parent_id).where(
                Category.org_id == org_id,
                Category.parent_id.in_(cat_ids),
            )
        )
    ).all()
    parent_to_subs: dict[int, list[int]] = {cid: [] for cid in cat_ids}
    for sub_id, parent_id in sub_rows:
        # Skip children that have their own budget row — otherwise
        # the child's transactions are counted twice (once via the
        # parent's rollup, once via the child's own facts row).
        if sub_id in budgeted_cat_ids:
            continue
        parent_to_subs.setdefault(parent_id, []).append(sub_id)

    today = datetime.date.today()
    current_month_start = today.replace(day=1)

    # The 3-month window must be (a) exactly 3 calendar months wide so
    # the divisor of 3 is honest, AND (b) strictly disjoint from the
    # current-period filter below — otherwise transactions in the
    # overlap are counted in both totals. Pick whichever cutoff comes
    # first (current calendar month vs. the period start) as the
    # upper bound, then take a clean 3-month window ending there.
    three_mo_upper = min(current_month_start, period_start)
    three_mo_lower = three_mo_upper - relativedelta(months=3)

    # Collect every category_id we need to sum over (budgets + their
    # rolled-up children) so we can do TWO grouped queries instead of
    # 2N round-trips inside the loop.
    all_rollup_ids: set[int] = set()
    for b in budget_rows:
        all_rollup_ids.add(b.category_id)
        for sub_id in parent_to_subs.get(b.category_id, []):
            all_rollup_ids.add(sub_id)

    base_filter = [
        Transaction.org_id == org_id,
        Transaction.category_id.in_(all_rollup_ids),
        Transaction.type == TransactionType.EXPENSE,
        Transaction.status == TransactionStatus.SETTLED,
        reportable_transaction_filter(),
    ]

    # One query for the 3-month rollup, GROUP BY category_id.
    three_mo_rows = (
        await db.execute(
            select(Transaction.category_id, func.sum(Transaction.amount))
            .where(
                *base_filter,
                effective_period_date_expr() >= three_mo_lower,
                effective_period_date_expr() < three_mo_upper,
            )
            .group_by(Transaction.category_id)
        )
    ).all()
    three_mo_by_cat: dict[int, Decimal] = {
        cid: Decimal(str(total or 0)) for cid, total in three_mo_rows
    }

    # One query for the current-period rollup, GROUP BY category_id.
    current_q = (
        select(Transaction.category_id, func.sum(Transaction.amount))
        .where(*base_filter, effective_period_date_expr() >= period_start)
    )
    if period_end is not None:
        current_q = current_q.where(effective_period_date_expr() <= period_end)
    current_rows = (
        await db.execute(current_q.group_by(Transaction.category_id))
    ).all()
    current_by_cat: dict[int, Decimal] = {
        cid: Decimal(str(total or 0)) for cid, total in current_rows
    }

    facts: list[_CategoryFact] = []
    for b in budget_rows:
        all_cat_ids = [b.category_id] + parent_to_subs.get(b.category_id, [])
        three_mo_total = sum(
            (three_mo_by_cat.get(c, Decimal(0)) for c in all_cat_ids),
            Decimal(0),
        )
        three_mo_avg = (three_mo_total / Decimal(3)).quantize(Decimal("0.01"))
        current_actual = sum(
            (current_by_cat.get(c, Decimal(0)) for c in all_cat_ids),
            Decimal(0),
        )

        facts.append(
            _CategoryFact(
                category_id=b.category_id,
                category_name=b.category.name if b.category else "",
                budget_amount=b.amount,
                last_3mo_total=three_mo_total,
                last_3mo_avg=three_mo_avg,
                current_mo_actual=current_actual,
            )
        )

    return facts


def _build_messages(
    facts: list[_CategoryFact], period_start: datetime.date
) -> list[dict]:
    """Build the LLM chat messages from aggregated facts.

    The system prompt establishes the closed-set category contract +
    the JSON output shape. The user message carries ONLY the aggregated
    per-category numbers. No transaction descriptions, no PII.
    """
    closed_set_ids = sorted(f.category_id for f in facts)
    system = (
        "You are a budgeting assistant. For ONE billing period you help "
        "prioritize which overspending categories matter most, so a "
        "deterministic system can move money from categories with surplus.\n\n"
        "RULES (must be followed):\n"
        "1. Output ONLY a JSON object matching the schema; no prose, no markdown.\n"
        "2. Every category_id you reference MUST come from this exact set: "
        f"{closed_set_ids}. Reject any other id.\n"
        "3. Return ONLY a priority ordering of the category_ids that are "
        "projected to OVERSPEND (most important to cover first — essential "
        "bills before discretionary), short per-category reasoning, and a "
        "one-line summary. Do NOT return any amounts; the system computes "
        "the money movements.\n"
        "4. Limit each reasoning ``text`` to 2 short sentences. No personal "
        "advice, no investment guidance.\n"
        "5. Suggestions are ADVISORY ONLY — they are NOT auto-applied.\n"
        "6. You are reallocating a FIXED total. You cannot add money; you can "
        "only rank which overspending categories matter most.\n"
        "7. If nothing is projected to overspend, return an empty "
        "``priority`` array with a short ``summary`` saying so.\n"
    )

    aggregates = [
        {
            "category_id": f.category_id,
            "category_name": f.category_name,
            "current_budget": float(f.budget_amount),
            "last_3mo_avg_actual": float(f.last_3mo_avg),
            "current_period_actual_so_far": float(f.current_mo_actual),
            "projected_spend": float(_project_period_spend(f)),
            "surplus": float(
                (f.budget_amount - _project_period_spend(f)).quantize(CENT)
            ),
        }
        for f in facts
    ]
    user_payload = {
        "period_start": period_start.isoformat(),
        "categories": aggregates,
    }
    # Serialize the aggregates as strict JSON (not Python repr) so the
    # system prompt's "output JSON" instruction is paired with valid
    # JSON input — sort_keys keeps the payload stable across runs and
    # makes it easy for tests to parse with json.loads.
    user = (
        "Given the per-category aggregates below (including each category's "
        "projected end-of-period spend and surplus), rank which overspending "
        "categories matter most to cover.\n\n"
        f"AGGREGATES:\n{json.dumps(user_payload, sort_keys=True)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _parse_ai_guidance(
    raw: dict, allowed_ids: set[int]
) -> tuple[list[int], dict[int, str], str]:
    """Extract ``(priority_ids, reasoning_by_cat, summary)`` from LLM JSON.

    Drops any ids outside the closed set and de-dupes the priority list.
    Never raises — a bad shape degrades to empty guidance so the
    deterministic allocator still runs and produces a balanced result.
    """
    priority: list[int] = []
    for cid in raw.get("priority") or []:
        if isinstance(cid, int) and cid in allowed_ids and cid not in priority:
            priority.append(cid)

    reasons: dict[int, str] = {}
    for item in raw.get("reasoning") or []:
        if not isinstance(item, dict):
            continue
        cid = item.get("category_id")
        text = item.get("text")
        if isinstance(cid, int) and cid in allowed_ids and isinstance(text, str):
            reasons[cid] = text.strip()[:400]

    summary = (raw.get("summary") or "").strip()[:400]
    return priority, reasons, summary


async def suggest_rebalance(
    db: AsyncSession,
    *,
    org_id: int,
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
) -> BudgetRebalanceResponse:
    """Compute the AI rebalance suggestion for an org's current period.

    Always returns a typed ``BudgetRebalanceResponse`` — the router
    maps this to HTTP 200 regardless of internal failure mode. The
    feature gate is enforced at the router layer before this service
    runs.

    ``session_factory`` is optional only because some legacy tests
    call this without one; the router always passes it. When provided,
    the LLM dispatch runs in its own session so the dispatcher's
    ledger commit can't bleed into the request transaction.
    """
    period = await get_current_period(db, org_id)
    facts = await _gather_facts(
        db,
        org_id=org_id,
        period_start=period.start_date,
        period_end=period.end_date,
    )
    if not facts:
        return BudgetRebalanceResponse(
            status="empty_no_budgets",
            period_start=period.start_date.isoformat(),
            summary=(
                "No budgets are set for the current period. Add a few "
                "budgets first, then come back for a rebalance suggestion."
            ),
        )

    if all(f.last_3mo_total <= 0 for f in facts):
        return BudgetRebalanceResponse(
            status="empty_no_history",
            period_start=period.start_date.isoformat(),
            summary=(
                "Not enough recent spending history to suggest a "
                "rebalance. Come back once you have a few months of "
                "categorized transactions."
            ),
        )

    # Short-circuit before spending an LLM call: if no category is
    # projected to come in under budget, there is no surplus to move and
    # the rebalance is a no-op. Refuse honestly instead of inventing money.
    total_budget = sum((f.budget_amount for f in facts), Decimal("0")).quantize(CENT)
    total_headroom = sum(
        (
            max(f.budget_amount - _project_period_spend(f), Decimal("0"))
            for f in facts
        ),
        Decimal("0"),
    )
    if total_headroom <= 0:
        return BudgetRebalanceResponse(
            status="empty_no_surplus",
            period_start=period.start_date.isoformat(),
            total_budget=total_budget,
            total_suggested=total_budget,
            uncovered_overspend=Decimal("0.00"),
            is_balanced=True,
            summary=(
                "Every category is projected at or over budget — there's "
                "nothing to reallocate. Your total budget is below projected "
                "spending this period."
            ),
        )

    messages = _build_messages(facts, period.start_date)

    # ``call_llm_structured`` commits the session it's given (via
    # ``_write_ledger_row`` in ai_dispatch.py). Use a dedicated session
    # when one is available so the dispatcher's commit can't bleed
    # into the request transaction. Same pattern as #368/#369.
    #
    # The LLM only supplies a PRIORITY ORDER + narrative. If dispatch
    # fails for any reason, we fall through with empty guidance: the
    # deterministic allocator still conserves the total and covers the
    # largest deficits first, so the rebalance works fully offline.
    priority: list[int] = []
    reasons: dict[int, str] = {}
    summary = ""
    try:
        if session_factory is not None:
            async with session_factory() as dispatch_db:
                result = await call_llm_structured(
                    dispatch_db,
                    org_id=org_id,
                    feature_key=ROUTING_KEY,
                    messages=messages,
                    response_schema=LLM_RESPONSE_SCHEMA,
                )
        else:
            result = await call_llm_structured(
                db,
                org_id=org_id,
                feature_key=ROUTING_KEY,
                messages=messages,
                response_schema=LLM_RESPONSE_SCHEMA,
            )
        priority, reasons, summary = _parse_ai_guidance(
            result.response.parsed, {f.category_id for f in facts}
        )
    except (
        NoRoutingConfigured,
        AICapExceeded,
        AICapabilityNotSupported,
        NativeNotAvailable,
        StructuredOutputError,
        AIDispatchFailed,
    ) as exc:
        logger.info(
            "ai.budget.rebalance.unavailable",
            org_id=org_id,
            error_class=type(exc).__name__,
        )
    except Exception as exc:  # noqa: BLE001 - defensive
        # Anything unexpected (engine errors from session_factory(),
        # transient DB blips, a malformed parsed payload, etc.) degrades
        # to empty guidance instead of a 500. The deterministic allocator
        # below still yields a balanced suggestion.
        logger.warning(
            "ai.budget.rebalance.unexpected_error",
            org_id=org_id,
            error_class=type(exc).__name__,
        )

    suggestions, uncovered = _allocate_rebalance(facts, priority, reasons)

    # Conservation guard: the allocator conserves the total by
    # construction, but assert it before returning — if a future change
    # ever drifts, re-run with empty guidance (pure deterministic
    # baseline) and log rather than emit a non-zero-sum plan.
    emitted = {s.category_id: s.suggested_amount for s in suggestions}
    total_suggested = sum(
        (emitted.get(f.category_id, f.budget_amount) for f in facts),
        Decimal("0"),
    ).quantize(CENT)
    if abs(total_suggested - total_budget) > CENT:
        logger.warning(
            "ai.budget.rebalance.guard_tripped",
            org_id=org_id,
            total_budget=str(total_budget),
            total_suggested=str(total_suggested),
        )
        suggestions, uncovered = _allocate_rebalance(facts, [], {})
        emitted = {s.category_id: s.suggested_amount for s in suggestions}
        total_suggested = sum(
            (emitted.get(f.category_id, f.budget_amount) for f in facts),
            Decimal("0"),
        ).quantize(CENT)

    return BudgetRebalanceResponse(
        status="ok",
        period_start=period.start_date.isoformat(),
        suggestions=suggestions,
        summary=summary or "Here's a balanced way to cover your overspending.",
        total_budget=total_budget,
        total_suggested=total_suggested,
        uncovered_overspend=uncovered,
        is_balanced=(uncovered == 0),
    )
