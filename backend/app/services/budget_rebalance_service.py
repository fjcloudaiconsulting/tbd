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
from app.services.transaction_filters import reportable_transaction_filter


logger = structlog.stdlib.get_logger()


FEATURE_KEY = "ai.budget"


# The structured-output schema the LLM must satisfy. ``call_llm_structured``
# validates ``type=object`` + required keys; we additionally validate
# the final payload in ``_validate_and_shape`` for type + bounds.
LLM_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "required": ["suggestions", "summary"],
    "properties": {
        "summary": {"type": "string"},
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "category_id",
                    "suggested_amount",
                    "reasoning",
                ],
                "properties": {
                    "category_id": {"type": "integer"},
                    "suggested_amount": {"type": "number"},
                    "reasoning": {"type": "string"},
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

    facts: list[_CategoryFact] = []
    for b in budget_rows:
        all_cat_ids = [b.category_id] + parent_to_subs.get(b.category_id, [])

        # Last 3 calendar months of settled expenses, strictly before
        # the current period and the current calendar month.
        three_mo_sum = await db.scalar(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.org_id == org_id,
                Transaction.category_id.in_(all_cat_ids),
                Transaction.type == TransactionType.EXPENSE,
                Transaction.status == TransactionStatus.SETTLED,
                Transaction.settled_date >= three_mo_lower,
                Transaction.settled_date < three_mo_upper,
                reportable_transaction_filter(),
            )
        )
        three_mo_total = Decimal(str(three_mo_sum or 0))
        three_mo_avg = (three_mo_total / Decimal(3)).quantize(Decimal("0.01"))

        # Current period's running spend.
        q = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.org_id == org_id,
            Transaction.category_id.in_(all_cat_ids),
            Transaction.type == TransactionType.EXPENSE,
            Transaction.status == TransactionStatus.SETTLED,
            Transaction.settled_date >= period_start,
            reportable_transaction_filter(),
        )
        if period_end is not None:
            q = q.where(Transaction.settled_date <= period_end)
        current_sum = await db.scalar(q)
        current_actual = Decimal(str(current_sum or 0))

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
        "You are a budgeting assistant. Suggest small budget shifts based "
        "on actual versus planned spending trends for ONE billing period.\n\n"
        "RULES (must be followed):\n"
        "1. Output ONLY a JSON object matching the schema; no prose, no markdown.\n"
        "2. Every suggestion MUST reference a category_id from this exact set: "
        f"{closed_set_ids}. Reject any other id.\n"
        "3. ``suggested_amount`` MUST be a non-negative number representing the "
        "new monthly budget for that category. Do not return deltas; return the "
        "new absolute amount.\n"
        "4. Limit reasoning to 2 short sentences per category. No personal "
        "advice, no investment guidance.\n"
        "5. Suggestions are ADVISORY ONLY — they are NOT auto-applied.\n"
        "6. The sum of ``suggested_amount`` across all categories should "
        "stay close to the sum of current budgets (within 10%). Reallocate, "
        "do not inflate.\n"
        "7. If no meaningful change is warranted, return an empty "
        "``suggestions`` array with a short ``summary`` saying so.\n"
    )

    aggregates = [
        {
            "category_id": f.category_id,
            "category_name": f.category_name,
            "current_budget": float(f.budget_amount),
            "last_3mo_avg_actual": float(f.last_3mo_avg),
            "current_period_actual_so_far": float(f.current_mo_actual),
        }
        for f in facts
    ]
    user_payload = {
        "period_start": period_start.isoformat(),
        "categories": aggregates,
    }
    user = (
        "Given the per-category aggregates below, suggest budget shifts.\n\n"
        f"AGGREGATES:\n{user_payload}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _validate_and_shape(
    raw: dict, facts: list[_CategoryFact]
) -> list[BudgetDeltaSuggestion]:
    """Convert raw LLM JSON into typed suggestions, rejecting drift.

    Raises ``ValueError`` if the response contains category ids not in
    the org's current-period budget set, or if numeric coercion fails.
    """
    allowed = {f.category_id: f for f in facts}
    suggestions = raw.get("suggestions") or []
    if not isinstance(suggestions, list):
        raise ValueError("suggestions is not a list")

    shaped: list[BudgetDeltaSuggestion] = []
    seen: set[int] = set()
    for item in suggestions:
        if not isinstance(item, dict):
            raise ValueError("suggestion item is not an object")
        cid = item.get("category_id")
        if not isinstance(cid, int) or cid not in allowed:
            raise ValueError(f"category_id {cid!r} is not in the allowed set")
        if cid in seen:
            # Drop duplicate suggestions for the same category (LLM noise).
            continue
        seen.add(cid)
        suggested = item.get("suggested_amount")
        try:
            suggested_amount = Decimal(str(suggested)).quantize(Decimal("0.01"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"suggested_amount not numeric: {suggested!r}"
            ) from exc
        if suggested_amount < 0:
            raise ValueError(
                f"suggested_amount must be >= 0 (got {suggested_amount})"
            )
        reasoning = item.get("reasoning") or ""
        if not isinstance(reasoning, str):
            raise ValueError("reasoning must be a string")
        reasoning = reasoning.strip()[:400]

        fact = allowed[cid]
        shaped.append(
            BudgetDeltaSuggestion(
                category_id=cid,
                category_name=fact.category_name,
                current_amount=fact.budget_amount,
                suggested_amount=suggested_amount,
                delta_amount=suggested_amount - fact.budget_amount,
                reasoning=reasoning,
            )
        )

    return shaped


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

    messages = _build_messages(facts, period.start_date)

    # ``call_llm_structured`` commits the session it's given (via
    # ``_write_ledger_row`` in ai_dispatch.py). Use a dedicated session
    # when one is available so the dispatcher's commit can't bleed
    # into the request transaction. Same pattern as #368/#369.
    try:
        if session_factory is not None:
            async with session_factory() as dispatch_db:
                result = await call_llm_structured(
                    dispatch_db,
                    org_id=org_id,
                    feature_key=FEATURE_KEY,
                    messages=messages,
                    response_schema=LLM_RESPONSE_SCHEMA,
                )
        else:
            result = await call_llm_structured(
                db,
                org_id=org_id,
                feature_key=FEATURE_KEY,
                messages=messages,
                response_schema=LLM_RESPONSE_SCHEMA,
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
        return BudgetRebalanceResponse(
            status="llm_unavailable",
            period_start=period.start_date.isoformat(),
            summary=(
                "AI rebalance is temporarily unavailable. Set up an AI "
                "provider in Settings or try again later."
            ),
        )

    try:
        shaped = _validate_and_shape(result.response.parsed, facts)
    except ValueError as exc:
        logger.info(
            "ai.budget.rebalance.response_invalid",
            org_id=org_id,
            error=str(exc),
        )
        return BudgetRebalanceResponse(
            status="llm_unavailable",
            period_start=period.start_date.isoformat(),
            summary=(
                "AI returned an unexpected response. Try again, or set "
                "up a different model in Settings."
            ),
        )

    summary_raw = (result.response.parsed.get("summary") or "").strip()
    summary = summary_raw[:400]

    return BudgetRebalanceResponse(
        status="ok",
        period_start=period.start_date.isoformat(),
        suggestions=shaped,
        summary=summary,
    )
