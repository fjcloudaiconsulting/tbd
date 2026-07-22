# Credit Card Model V1 — Slice 3 (forecast integration) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Synthesize the projected credit-card payment into the per-account month-end forecast — an ephemeral in-memory delta (`source.expected -= outflow`, `cc.expected += outflow`) computed in `account_balance_forecast_service.compute_account_balance_forecast`, using the **carried-balance-as-of-close** `B_k`, the unified four-strategy amount resolver (`min(target, outstanding_at_close) - P_k_owned - S_prev`), and the locked edge-case guards (NULL source, cross-currency, inactive source, card-in-credit, unset per-cycle amount, grace-period cycle). Expose the synthesized payment as provenance on the per-account forecast line and render a quiet "Payment {amount} on {date}" muted subline. **No schema/migration changes**; `forecast_service.py` (reportable aggregate) is untouched.

**Architecture:** The risky math is extracted into a **pure, DB-free** module `backend/app/services/cc_forecast_service.py` (cycle enumeration over the horizon, `B_k` reconstruction from a signed ledger, per-strategy target, uniform clamp+net, `S_prev` + consumed-credit threading), so it is unit-testable without a database. `account_balance_forecast_service` batch-fetches the three inputs (per-cycle stored amounts, the non-reverted signed ledger for `B_k`, and the real CC payment-in credits for `P_k_owned`), then calls the pure orchestrator per active CC that has a non-null `payment_source_account_id`, applies the two conserved deltas to a `synth_delta_by_account` map, and attaches provenance to the CC's per-account payload row. The currency `totals` rollup is deliberately **left untouched** (derived from `balance + pending_delta`, not per-account `expected`), which preserves totals parity and is why cross-currency synthesis is skipped. The Pydantic response row gains an optional `cc_payments` list; the frontend `AccountMonthEndForecast` renders one muted subline per entry.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic v2, MySQL 8 (SQLite in-memory via aiosqlite for the service tests); Next.js 16 + React 19 + TypeScript, Vitest + Testing Library.

## Global Constraints

- Python 3.12 / FastAPI / SQLAlchemy 2.0 async / Pydantic v2 / MySQL 8. Frontend: Next.js 16 + React 19 + TypeScript.
- **NO schema/migration changes this slice.** The `cc_cycle_payments` store (migration 074) + `CcCyclePayment` model already exist (Slice 2). Do not add columns, tables, or migrations.
- **Two-service split (load-bearing):** synthesize the CC payment ONLY in `account_balance_forecast_service` (per-account balances, includes transfer legs). NEVER touch `forecast_service.py` — its `reportable_transaction_filter` excludes transfer legs; injecting there double-counts and breaks dashboard-donut parity. Read `forecast_service.py` once to CONFIRM the CC payment must not appear there (do not modify it).
- **Cash-basis** bucketing everywhere via `effective_period_date_expr()` = `coalesce(settled_date, date)`.
- **Owed balances are stored NEGATIVE** (liability). `outstanding_at_close = max(0, -B_k)`; `B_k` is signed income `+`, expense `−` (matches `transaction_service.apply_balance`: INCOME `+=`, else `−=`).
- **Currency-skip:** if `source.currency != cc.currency` → no synthesis (no FX in V1; would desync the per-currency totals). Same no-op as NULL source.
- No em-dashes in user copy. No off-token colors — the payment subline uses `text-[10px] tabular-nums text-text-muted`, NO status/accent color. No AI attribution in commits/PRs.
- **The isolated compose project `team-ccm1` is ALREADY UP.** Do NOT run `docker compose up`. Run backend tests with `docker compose -p team-ccm1 exec -T backend pytest ...` and frontend with `docker compose -p team-ccm1 exec -T frontend npm test -- ...`. Always pass `-T`.

> **Executor note:** line numbers below reflect the repo at plan-authoring time and may drift. Treat them as anchors — the TDD loop catches drift. Re-locate by the quoted surrounding code. The injection point in `account_balance_forecast_service.py` is **between the `pending_by_account` build and the per-account payload loop**; the loop's `expected = balance + delta` line and the `accounts_payload.append({...})` are the two edit sites inside the loop. Where the plan names variables in existing code (`rows`, `pending_by_account`, `eff_date`, `p_start`/`p_end`, `_q`, the payload dict keys, and the test harness `_seed`/`_new_tx`/`PERIOD_START`/`cat_transfer`), READ the real files and adapt to the actual names.

---

## File Structure

| File | Create / Modify | Responsibility |
|---|---|---|
| `backend/app/services/cc_forecast_service.py` | Create | Pure, DB-free synthesis math: `due_cycles_in_horizon`, `balance_at_close`, `outstanding_at_close`, `cc_target_payment`, `synthesize_account_cc_payments`. |
| `backend/tests/services/test_cc_forecast_service.py` | Create | Pure unit tests for all locked invariants at the math level. |
| `backend/app/services/account_balance_forecast_service.py` | Modify | Batch-fetch the three inputs; call the orchestrator per eligible CC; apply conserved deltas to `expected`; attach `cc_payments` provenance. Totals untouched. |
| `backend/tests/services/test_account_balance_forecast_service.py` | Modify | Append DB integration tests: conservation, NULL-source parity, cross-currency no-op, grace-period, two-due-dates, real-payment netting, clamp. Extend seed with a CC account. |
| `backend/app/schemas/forecast.py` | Modify | Add `CcPaymentLine` model + `cc_payments: list[CcPaymentLine] = []` to the per-account row. |
| `backend/tests/routers/test_forecast_account_balances_cc.py` | Create | Assert the endpoint response surfaces `cc_payments`. |
| `frontend/components/dashboard/AccountMonthEndForecast.tsx` | Modify | Add `cc_payments?` to the row type; render one muted "Payment {symbol}{amount} on {date}" subline per entry. |
| `frontend/tests/components/dashboard/account-month-end-forecast.test.tsx` | Modify | Append a describe block asserting the payment subline. |

---

## Task 1: Pure synthesis math — `cc_forecast_service.py` + unit tests

**Files:**
- Create: `backend/app/services/cc_forecast_service.py`
- Create: `backend/tests/services/test_cc_forecast_service.py`

**Interfaces:**
- Consumes: `cc_cycle_service.resolve_cycle_for_account(account, target_date) -> CreditCardCycle(period_start, period_end_inclusive, payment_date, source)`.
- Produces:
  - `due_cycles_in_horizon(account, *, p_start, p_end) -> list[CreditCardCycle]` — cycles whose `payment_date` ∈ `[p_start, p_end]`, deduped by close, sorted by `payment_date`.
  - `balance_at_close(opening_balance, ledger, close_date) -> Decimal`.
  - `outstanding_at_close(b_k) -> Decimal`.
  - `cc_target_payment(account, cycle, outstanding, per_cycle_amounts) -> Decimal`.
  - `synthesize_account_cc_payments(account, *, p_start, p_end, opening_balance, ledger, credits, per_cycle_amounts) -> list[tuple[date, Decimal]]`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/services/test_cc_forecast_service.py`:

```python
"""Pure-math unit tests for cc_forecast_service (Credit Card Model V1, Slice 3).

DB-free. Proves the locked forecast invariants at the arithmetic level:
carried-balance-as-of-close B_k, the unified clamp+net resolver, S_prev
and consumed-credit threading, and every strategy's target.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.services import cc_forecast_service as svc


class _FakeAccount:
    """Stand-in exposing exactly the attributes the pure math reads."""

    def __init__(
        self,
        *,
        id=1,
        close_day=25,
        payment_day=1,
        payment_day_relative_month=1,
        payment_strategy="full_balance",
        fixed_payment_amount=None,
        currency="EUR",
    ):
        self.id = id
        self.close_day = close_day
        self.payment_day = payment_day
        self.payment_day_relative_month = payment_day_relative_month
        self.payment_strategy = payment_strategy
        self.fixed_payment_amount = fixed_payment_amount
        self.currency = currency


P_START = date(2026, 5, 1)
P_END = date(2026, 5, 31)


def test_due_cycles_in_horizon_grace_period_close_in_past():
    acct = _FakeAccount(close_day=25)
    cycles = svc.due_cycles_in_horizon(acct, p_start=P_START, p_end=P_END)
    assert len(cycles) == 1
    assert cycles[0].period_end_inclusive == date(2026, 4, 25)
    assert cycles[0].payment_date == date(2026, 5, 1)


def test_due_cycles_in_horizon_two_due_dates():
    acct = _FakeAccount(close_day=25)
    cycles = svc.due_cycles_in_horizon(
        acct, p_start=date(2026, 5, 1), p_end=date(2026, 6, 30)
    )
    assert [c.payment_date for c in cycles] == [date(2026, 5, 1), date(2026, 6, 1)]


def test_due_cycles_in_horizon_none_when_no_due_date():
    acct = _FakeAccount(close_day=25)
    cycles = svc.due_cycles_in_horizon(
        acct, p_start=date(2026, 5, 10), p_end=date(2026, 5, 15)
    )
    assert cycles == []


def test_balance_at_close_excludes_activity_after_close():
    ledger = [
        (date(2026, 4, 10), Decimal("-500.00")),   # before close -> counts
        (date(2026, 5, 3), Decimal("-700.00")),     # after close  -> excluded
    ]
    b_k = svc.balance_at_close(Decimal("0.00"), ledger, date(2026, 4, 25))
    assert b_k == Decimal("-500.00")
    assert svc.outstanding_at_close(b_k) == Decimal("500.00")


def test_outstanding_at_close_card_in_credit_is_zero():
    assert svc.outstanding_at_close(Decimal("120.00")) == Decimal("0")


def _cycle(acct):
    return svc.due_cycles_in_horizon(acct, p_start=P_START, p_end=P_END)[0]


def test_target_full_balance_is_outstanding():
    acct = _FakeAccount(payment_strategy="full_balance")
    assert svc.cc_target_payment(acct, _cycle(acct), Decimal("1200.00"), {}) == Decimal("1200.00")


def test_target_fixed_amount_is_literal():
    acct = _FakeAccount(payment_strategy="fixed_amount", fixed_payment_amount=Decimal("150.00"))
    assert svc.cc_target_payment(acct, _cycle(acct), Decimal("1200.00"), {}) == Decimal("150.00")


@pytest.mark.parametrize("strategy", ["minimum_only", "custom_per_period"])
def test_target_stored_amount_when_present(strategy):
    acct = _FakeAccount(payment_strategy=strategy)
    cyc = _cycle(acct)  # close Apr 25 -> anchor (id, 2026, 4)
    per_cycle = {(acct.id, 2026, 4): Decimal("75.00")}
    assert svc.cc_target_payment(acct, cyc, Decimal("1200.00"), per_cycle) == Decimal("75.00")


@pytest.mark.parametrize("strategy", ["minimum_only", "custom_per_period"])
def test_target_zero_when_stored_amount_unset(strategy):
    acct = _FakeAccount(payment_strategy=strategy)
    assert svc.cc_target_payment(acct, _cycle(acct), Decimal("1200.00"), {}) == Decimal("0")


def test_target_none_strategy_defaults_full_balance():
    acct = _FakeAccount(payment_strategy=None)
    assert svc.cc_target_payment(acct, _cycle(acct), Decimal("900.00"), {}) == Decimal("900.00")


def test_synth_full_balance_pays_total_owed_at_close():
    acct = _FakeAccount(payment_strategy="full_balance")
    ledger = [(date(2026, 4, 10), Decimal("-500.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=P_START, p_end=P_END,
        opening_balance=Decimal("-400.00"), ledger=ledger, credits=[], per_cycle_amounts={},
    )
    assert pays == [(date(2026, 5, 1), Decimal("900.00"))]


def test_synth_card_in_credit_no_outflow():
    acct = _FakeAccount(payment_strategy="full_balance")
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=P_START, p_end=P_END,
        opening_balance=Decimal("50.00"), ledger=[], credits=[], per_cycle_amounts={},
    )
    assert pays == []


def test_synth_clamp_never_pays_into_credit():
    acct = _FakeAccount(payment_strategy="fixed_amount", fixed_payment_amount=Decimal("500.00"))
    ledger = [(date(2026, 4, 10), Decimal("-300.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=P_START, p_end=P_END,
        opening_balance=Decimal("0.00"), ledger=ledger, credits=[], per_cycle_amounts={},
    )
    assert pays == [(date(2026, 5, 1), Decimal("300.00"))]


def test_synth_real_payment_nets_p_k():
    acct = _FakeAccount(payment_strategy="full_balance")
    ledger = [(date(2026, 4, 10), Decimal("-1000.00"))]
    credits = [(99, date(2026, 4, 28), Decimal("300.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=P_START, p_end=P_END,
        opening_balance=Decimal("0.00"), ledger=ledger, credits=credits, per_cycle_amounts={},
    )
    assert pays == [(date(2026, 5, 1), Decimal("700.00"))]


def test_synth_two_due_dates_s_prev_prevents_double_bill():
    acct = _FakeAccount(close_day=25, payment_strategy="full_balance")
    ledger = [(date(2026, 3, 10), Decimal("-1000.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=date(2026, 5, 1), p_end=date(2026, 6, 30),
        opening_balance=Decimal("0.00"), ledger=ledger, credits=[], per_cycle_amounts={},
    )
    assert pays == [(date(2026, 5, 1), Decimal("1000.00"))]


def test_synth_consumed_credit_attributed_to_one_cycle():
    acct = _FakeAccount(close_day=25, payment_day=1, payment_day_relative_month=2,
                        payment_strategy="full_balance")
    ledger = [(date(2026, 3, 10), Decimal("-1000.00"))]
    credits = [(7, date(2026, 5, 3), Decimal("200.00"))]
    pays = svc.synthesize_account_cc_payments(
        acct, p_start=date(2026, 6, 1), p_end=date(2026, 7, 31),
        opening_balance=Decimal("0.00"), ledger=ledger, credits=credits, per_cycle_amounts={},
    )
    assert pays == [(date(2026, 6, 1), Decimal("800.00")), (date(2026, 7, 1), Decimal("200.00"))]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_cc_forecast_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.cc_forecast_service'`.

- [ ] **Step 3: Write the implementation**

Create `backend/app/services/cc_forecast_service.py`:

```python
"""Pure credit-card projected-payment math (Credit Card Model V1, Slice 3).

DB-free. All arithmetic for the forecast synthesis lives here so it is
unit-testable without a database; ``account_balance_forecast_service``
supplies the batch-fetched inputs and applies the resulting deltas.

Locked contract (``specs/2026-07-22-cc-model-v1-design.md`` § "Forecast
integration", § "Amount resolution", § "Edge cases", § "Resolved by
architect review"):

  outstanding_at_close = max(0, -B_k)          # owed stored NEGATIVE
  B_k = opening_balance + Σ signed(eff_date <= close_date)   # cash-basis, as-of-CLOSE
  target  = per-strategy target payment
  capped  = min(target, outstanding_at_close)  # never pay a card into credit
  outflow = max(0, capped - P_k_owned - S_prev)

``S_prev`` threads earlier synthesized outflows this horizon (stops a
two-due-date horizon double-billing carried debt); ``P_k_owned``
attributes each real payment-in credit to exactly ONE owning cycle (the
earliest window), so overlapping windows (payment_day_relative_month>=2)
don't double-net.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from app.services.cc_cycle_service import CreditCardCycle, resolve_cycle_for_account

# Safety cap on the enumeration walk (a horizon is at most a couple of
# cycles; this only guards against a pathological resolver loop).
_MAX_WALK_ITERS = 60


def due_cycles_in_horizon(
    account: object, *, p_start: date, p_end: date
) -> list[CreditCardCycle]:
    """Every cycle whose ``payment_date`` falls in ``[p_start, p_end]``.

    Walks close dates forward from a lookback point far enough before the
    horizon to catch grace-period cycles (a cycle that closed before the
    horizon but is due inside it). ``payment_date`` is monotonic in the
    close date, so once it exceeds ``p_end`` the walk is done.
    """
    lookback_months = (getattr(account, "payment_day_relative_month", None) or 1) + 1
    cursor = p_start - relativedelta(months=lookback_months)
    seen: set[date] = set()
    cycles: list[CreditCardCycle] = []
    for _ in range(_MAX_WALK_ITERS):
        cycle = resolve_cycle_for_account(account, cursor)
        close = cycle.period_end_inclusive
        if close in seen:
            cursor = close + timedelta(days=1)
            continue
        seen.add(close)
        if cycle.payment_date > p_end:
            break
        if cycle.payment_date >= p_start:
            cycles.append(cycle)
        cursor = close + timedelta(days=1)
    cycles.sort(key=lambda c: c.payment_date)
    return cycles


def balance_at_close(
    opening_balance: Decimal,
    ledger: list[tuple[date, Decimal]],
    close_date: date,
) -> Decimal:
    """B_k = opening_balance + Σ signed(eff_date <= close_date).

    ``ledger`` is (eff_date, signed_amount) pairs (income +, expense -,
    transfer legs included) for this CC only; the caller filters to
    non-reverted rows so the reconstruction matches the stored balance.
    """
    total = Decimal(str(opening_balance))
    for eff, signed in ledger:
        if eff <= close_date:
            total += signed
    return total


def outstanding_at_close(b_k: Decimal) -> Decimal:
    """Positive magnitude of owed debt at close (owed is stored negative)."""
    return max(Decimal("0"), -b_k)


def _strategy_value(account: object) -> str:
    s = getattr(account, "payment_strategy", None)
    if s is None:
        return "full_balance"          # NULL-at-rest -> resolver default
    return s.value if hasattr(s, "value") else str(s)


def cc_target_payment(
    account: object,
    cycle: CreditCardCycle,
    outstanding: Decimal,
    per_cycle_amounts: dict[tuple[int, int, int], Decimal],
) -> Decimal:
    """Per-strategy target BEFORE the uniform clamp + net."""
    s = _strategy_value(account)
    if s == "full_balance":
        return outstanding
    if s == "fixed_amount":
        amt = getattr(account, "fixed_payment_amount", None)
        return Decimal(str(amt)) if amt is not None else Decimal("0")
    # minimum_only + custom_per_period: close-month-anchored stored amount.
    anchor = (
        account.id,
        cycle.period_end_inclusive.year,
        cycle.period_end_inclusive.month,
    )
    return per_cycle_amounts.get(anchor, Decimal("0"))


def synthesize_account_cc_payments(
    account: object,
    *,
    p_start: date,
    p_end: date,
    opening_balance: Decimal,
    ledger: list[tuple[date, Decimal]],
    credits: list[tuple[int, date, Decimal]],
    per_cycle_amounts: dict[tuple[int, int, int], Decimal],
) -> list[tuple[date, Decimal]]:
    """Chronological (payment_date, outflow) pairs for one CC (outflow > 0).

    ``credits`` is (id, eff_date, amount) for real CC payment-in legs
    (transfer income legs). Threads ``S_prev`` and a consumed-credit set
    across the horizon's cycles.
    """
    cycles = due_cycles_in_horizon(account, p_start=p_start, p_end=p_end)
    s_prev = Decimal("0")
    consumed: set[int] = set()
    payments: list[tuple[date, Decimal]] = []
    for cycle in cycles:
        b_k = balance_at_close(opening_balance, ledger, cycle.period_end_inclusive)
        outstanding = outstanding_at_close(b_k)
        target = cc_target_payment(account, cycle, outstanding, per_cycle_amounts)
        capped = min(target, outstanding)
        p_k_owned = Decimal("0")
        for cid, eff, amt in credits:
            if cid in consumed:
                continue
            if cycle.period_end_inclusive < eff <= cycle.payment_date:
                p_k_owned += amt
                consumed.add(cid)
        outflow = max(Decimal("0"), capped - p_k_owned - s_prev)
        s_prev += outflow
        if outflow > 0:
            payments.append((cycle.payment_date, outflow))
    return payments
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_cc_forecast_service.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/cc_forecast_service.py backend/tests/services/test_cc_forecast_service.py
git commit -m "feat(forecast): pure credit-card projected-payment math (carried-balance, unified resolver)"
```

---

## Task 2: Forecast injection in `account_balance_forecast_service` + DB integration tests

**Files:**
- Modify: `backend/app/services/account_balance_forecast_service.py`
- Modify: `backend/tests/services/test_account_balance_forecast_service.py`

**Interfaces:**
- Consumes: `cc_forecast_service.synthesize_account_cc_payments(...)`; `CcCyclePayment`; `non_reverted_transaction_filter()`; the cash-basis effective-date expr (already in scope); `case` (SQLAlchemy).
- Produces: unchanged public shape of `compute_account_balance_forecast(...)` PLUS a `"cc_payments": list[{"amount": str, "date": str}]` key on every account payload row (empty list when no synthesis). `expected_month_end_balance` on synthesized source/CC rows now includes the conserved delta; `totals` unchanged.

- [ ] **Step 1: Write the failing tests**

Append CC integration tests to `backend/tests/services/test_account_balance_forecast_service.py`. READ the file's real seed helpers first (`_seed`, `_new_tx`, `PERIOD_START`, category ids, the `BillingPeriod` seed) and adapt the helper below to them. Add model imports:

```python
from app.models.account import PaymentStrategy
from app.models.cc_cycle_payment import CcCyclePayment
```

Add a CC-seeding helper + charge helper (adapt names to the real `_seed`/`_new_tx`):

```python
async def _seed_cc(
    db,
    *,
    strategy=PaymentStrategy.FULL_BALANCE,
    fixed_payment_amount=None,
    cc_currency="EUR",
    source_currency="EUR",
    close_day=25,
    opening_balance=Decimal("0.00"),
):
    """Seed a checking source + a credit_card paid from it. Returns the base
    _seed() dict plus 'cc', 'source', 'cc_type_id'."""
    seed = await _seed(db)
    org_id = seed["org_id"]
    source = seed["accounts"]["primary"]
    if source_currency != source.currency:
        source.currency = source_currency
    cc_type = AccountType(org_id=org_id, name="Credit Card", slug="credit_card", is_system=True)
    db.add(cc_type)
    await db.flush()
    cc = Account(
        org_id=org_id, name="Visa", account_type_id=cc_type.id,
        balance=Decimal("0.00"), currency=cc_currency, is_default=False,
        close_day=close_day, payment_day=1, payment_day_relative_month=1,
        payment_source_account_id=source.id, payment_strategy=strategy,
        fixed_payment_amount=fixed_payment_amount, opening_balance=opening_balance,
    )
    db.add(cc)
    await db.flush()
    seed["cc"] = cc
    seed["source"] = source
    seed["cc_type_id"] = cc_type.id
    return seed


def _charge(seed, cc, *, amount, on, settled=True):
    """A settled CC expense (lowers the CC balance)."""
    return _new_tx(
        org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_expense"],
        amount=Decimal(amount), type=TransactionType.EXPENSE,
        status=TransactionStatus.SETTLED if settled else TransactionStatus.PENDING,
        date=on, settled_date=on if settled else None,
    )
```

Then append the tests (adapt `PERIOD_START`, `seed["period"]`, and the payload key names — `expected_month_end_balance`, `pending_delta`, `balance` — to the real ones):

```python
# ---------- Slice 3: CC projected-payment synthesis ----------

async def test_cc_synth_grace_period_uses_balance_as_of_close(db_session):
    """(h)+(a) close in the past, due in horizon: outflow == owed AS OF CLOSE."""
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]
    db_session.add_all([
        _charge(seed, cc, amount="500.00", on=datetime.date(2026, 4, 10)),
        _charge(seed, cc, amount="700.00", on=datetime.date(2026, 5, 3)),
    ])
    cc.balance = Decimal("-1200.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    by_id = {a["account_id"]: a for a in result["accounts"]}
    cc_row, src_row = by_id[cc.id], by_id[source.id]
    assert cc_row["cc_payments"] == [{"amount": "500.00", "date": "2026-05-01"}]
    assert Decimal(cc_row["expected_month_end_balance"]) == Decimal(cc_row["balance"]) + Decimal("500.00")
    assert Decimal(src_row["expected_month_end_balance"]) == (
        Decimal(src_row["balance"]) + Decimal(src_row["pending_delta"]) - Decimal("500.00")
    )


async def test_cc_synth_conservation_same_currency(db_session):
    """(b) totals unchanged; Σ per-account expected == Σ(balance+pending)."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="300.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-300.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    eur = next(t for t in result["totals"] if t["currency"] == "EUR")
    assert eur["expected_month_end_balance"] == str(
        (Decimal(eur["balance"]) + Decimal(eur["pending_delta"])).quantize(Decimal("0.01")))
    eur_rows = [a for a in result["accounts"] if a["currency"] == "EUR"]
    assert sum(Decimal(a["expected_month_end_balance"]) for a in eur_rows) == sum(
        Decimal(a["balance"]) + Decimal(a["pending_delta"]) for a in eur_rows)


async def test_cc_synth_null_source_value_parity(db_session):
    """(e) NULL source -> no synth; money fields match pre-Slice-3; cc_payments empty."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    cc.payment_source_account_id = None
    db_session.add(_charge(seed, cc, amount="800.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-800.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    for a in result["accounts"]:
        assert a["cc_payments"] == []
        assert a["expected_month_end_balance"] == str(
            (Decimal(a["balance"]) + Decimal(a["pending_delta"])).quantize(Decimal("0.01")))


async def test_cc_synth_cross_currency_source_no_op(db_session):
    """(f) source currency != CC currency -> no synthesis."""
    seed = await _seed_cc(db_session, cc_currency="EUR", source_currency="USD")
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="400.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-400.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == []


async def test_cc_synth_card_in_credit_no_outflow(db_session):
    """(g) nothing owed -> no outflow."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    cc.balance = Decimal("120.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == []


async def test_cc_synth_fixed_amount_clamped_to_owed(db_session):
    """(c)+(k) fixed_amount literal, clamped so it never pays into credit."""
    seed = await _seed_cc(db_session, strategy=PaymentStrategy.FIXED_AMOUNT,
                          fixed_payment_amount=Decimal("500.00"))
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="300.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-300.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == [{"amount": "300.00", "date": "2026-05-01"}]


async def test_cc_synth_minimum_only_reads_store_and_zero_when_unset(db_session):
    """(d) minimum_only reads the per-cycle stored amount; nothing when unset."""
    seed = await _seed_cc(db_session, strategy=PaymentStrategy.MINIMUM_ONLY)
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="900.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-900.00")
    await db_session.commit()
    r1 = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    assert next(a for a in r1["accounts"] if a["account_id"] == cc.id)["cc_payments"] == []
    db_session.add(CcCyclePayment(account_id=cc.id, period_anchor_year=2026,
                                  period_anchor_month=4, amount=Decimal("75.00")))
    await db_session.commit()
    r2 = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    assert next(a for a in r2["accounts"] if a["account_id"] == cc.id)["cc_payments"] == [
        {"amount": "75.00", "date": "2026-05-01"}]


async def test_cc_synth_real_payment_nets_once(db_session):
    """(j) a real CC payment-in credit in (close, due] nets P_k."""
    seed = await _seed_cc(db_session)
    cc, source = seed["cc"], seed["source"]
    db_session.add(_charge(seed, cc, amount="1000.00", on=datetime.date(2026, 4, 10)))
    src_leg = _new_tx(org_id=seed["org_id"], account_id=source.id, category_id=seed["cat_transfer"],
                      amount=Decimal("300.00"), type=TransactionType.EXPENSE,
                      status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 28),
                      settled_date=datetime.date(2026, 4, 28))
    cc_leg = _new_tx(org_id=seed["org_id"], account_id=cc.id, category_id=seed["cat_transfer"],
                     amount=Decimal("300.00"), type=TransactionType.INCOME,
                     status=TransactionStatus.SETTLED, date=datetime.date(2026, 4, 28),
                     settled_date=datetime.date(2026, 4, 28))
    db_session.add_all([src_leg, cc_leg])
    await db_session.flush()
    src_leg.linked_transaction_id = cc_leg.id
    cc_leg.linked_transaction_id = src_leg.id
    cc.balance = Decimal("-700.00")
    source.balance = source.balance - Decimal("300.00")
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == [{"amount": "700.00", "date": "2026-05-01"}]


async def test_cc_synth_two_due_dates_s_prev(db_session):
    """(i) a two-month horizon bills carried debt once."""
    seed = await _seed_cc(db_session)
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="1000.00", on=datetime.date(2026, 3, 10)))
    cc.balance = Decimal("-1000.00")
    seed["period"].end_date = datetime.date(2026, 6, 30)
    await db_session.commit()
    result = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    cc_row = next(a for a in result["accounts"] if a["account_id"] == cc.id)
    assert cc_row["cc_payments"] == [{"amount": "1000.00", "date": "2026-05-01"}]
```

> If `Account(...)` kwargs are rejected, read `backend/app/models/account.py` (all these columns exist post-Slice 1/2) and adapt. If mutating `BillingPeriod.end_date` doesn't widen the horizon (because `resolve_period` re-derives bounds), pass an explicit wider period instead.

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_account_balance_forecast_service.py -k "cc_synth" -v`
Expected: FAIL — rows have no `cc_payments` key; `expected_month_end_balance` excludes the synth delta.

- [ ] **Step 3: Write the implementation**

Edit `backend/app/services/account_balance_forecast_service.py`.

3a. Imports: add `case` to the `from sqlalchemy import ...` line; add:

```python
from app.models.cc_cycle_payment import CcCyclePayment
from app.services import cc_forecast_service
from app.services.transaction_filters import non_reverted_transaction_filter
```

3b. Insert the synthesis block AFTER the `pending_by_account` build and BEFORE the per-account payload loop. (Use the real names for the rows list, the horizon bounds `p_start`/`p_end`, the effective-date expr, the money quantizer `_q`, and `Transaction`/`TransactionType`.)

```python
    # ── Credit-card projected-payment synthesis (Slice 3) ─────────────────────
    # Ephemeral in-memory deltas with provenance source="credit_card_payment":
    # on each resolved due date the source asset drops and the CC liability
    # moves toward zero. Synthesized HERE (per-account balances include transfer
    # legs), never in forecast_service (reportable aggregate excludes them).
    # Totals are NOT adjusted (they derive from balance+pending); same-currency
    # conservation keeps them correct, and cross-currency is skipped so the
    # per-currency rollup never desyncs.
    accounts_by_id = {acct.id: (acct, slug) for acct, slug in rows}
    cc_accounts = [
        acct for acct, slug in rows
        if slug == "credit_card"
        and acct.close_day is not None
        and acct.payment_source_account_id is not None
    ]
    synth_delta_by_account: dict[int, Decimal] = {}
    cc_payments_by_account: dict[int, list[dict]] = {}

    if cc_accounts:
        cc_ids = [a.id for a in cc_accounts]

        pcp_rows = (await db.execute(
            select(CcCyclePayment.account_id, CcCyclePayment.period_anchor_year,
                   CcCyclePayment.period_anchor_month, CcCyclePayment.amount)
            .where(CcCyclePayment.account_id.in_(cc_ids))
        )).all()
        per_cycle_amounts = {(aid, y, m): Decimal(str(amt)) for aid, y, m, amt in pcp_rows}

        signed = case(
            (Transaction.type == TransactionType.INCOME, Transaction.amount),
            else_=-Transaction.amount,
        )
        ledger_rows = (await db.execute(
            select(Transaction.account_id, eff_date.label("eff"), signed.label("signed"))
            .where(Transaction.org_id == org_id,
                   Transaction.account_id.in_(cc_ids),
                   non_reverted_transaction_filter())
        )).all()
        ledger_by_account: dict[int, list[tuple]] = {}
        for aid, eff, s in ledger_rows:
            ledger_by_account.setdefault(aid, []).append((eff, Decimal(str(s))))

        credit_rows = (await db.execute(
            select(Transaction.id, Transaction.account_id, eff_date.label("eff"), Transaction.amount)
            .where(Transaction.org_id == org_id,
                   Transaction.account_id.in_(cc_ids),
                   Transaction.linked_transaction_id.is_not(None),
                   Transaction.type == TransactionType.INCOME,
                   non_reverted_transaction_filter())
        )).all()
        credits_by_account: dict[int, list[tuple]] = {}
        for cid, aid, eff, amt in credit_rows:
            credits_by_account.setdefault(aid, []).append((cid, eff, Decimal(str(amt))))

        for cc in cc_accounts:
            source_entry = accounts_by_id.get(cc.payment_source_account_id)
            if source_entry is None:
                continue  # source inactive/not loaded -> no-op (do not resurrect)
            source, _ = source_entry
            if source.currency != cc.currency:
                continue  # no FX in V1 -> would desync per-currency totals
            payments = cc_forecast_service.synthesize_account_cc_payments(
                cc, p_start=p_start, p_end=p_end,
                opening_balance=Decimal(str(cc.opening_balance)),
                ledger=ledger_by_account.get(cc.id, []),
                credits=credits_by_account.get(cc.id, []),
                per_cycle_amounts=per_cycle_amounts,
            )
            for pay_date, outflow in payments:
                synth_delta_by_account[source.id] = synth_delta_by_account.get(source.id, Decimal("0")) - outflow
                synth_delta_by_account[cc.id] = synth_delta_by_account.get(cc.id, Decimal("0")) + outflow
                cc_payments_by_account.setdefault(cc.id, []).append(
                    {"amount": _q(outflow), "date": pay_date.isoformat()})
```

3c. In the per-account payload loop, fold the synth delta into `expected` and add the key:

```python
        synth = synth_delta_by_account.get(account.id, Decimal("0"))
        expected = balance + delta + synth
```

and add `"cc_payments": cc_payments_by_account.get(account.id, []),` to the appended dict. LEAVE the `totals_by_currency` accumulation exactly as-is (no `synth`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_account_balance_forecast_service.py -v`
Expected: PASS (the new `cc_synth` tests AND all pre-existing — the pre-existing now carry an empty `cc_payments: []` they don't assert against).

- [ ] **Step 5: Confirm forecast_service is untouched**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_forecast_service.py -q` (adjust to the real reportable-forecast test file name)
Expected: PASS, unchanged.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/account_balance_forecast_service.py backend/tests/services/test_account_balance_forecast_service.py
git commit -m "feat(forecast): synthesize carried-balance credit-card payment in account-balance forecast"
```

---

## Task 3: Forecast schema provenance + response wiring + test

**Files:**
- Modify: `backend/app/schemas/forecast.py`
- Create: `backend/tests/routers/test_forecast_account_balances_cc.py`

**Interfaces:**
- Produces: the per-account forecast response row gains `cc_payments: list[CcPaymentLine]` (`CcPaymentLine{amount: Decimal, date: date}`), default `[]`, so `/api/v1/forecast/account-balances` surfaces provenance instead of dropping the key.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/routers/test_forecast_account_balances_cc.py`. Reuse the `_make_app`/`worlds` harness from `tests/test_cc_cycle_payments.py` (which already seeds an admin + checking + credit_card with a `payment_source_account_id`); seed a carried charge on the CC and assert the endpoint row carries `cc_payments`. If wiring the forecast router into that harness is heavy, INSTEAD add a Pydantic round-trip assertion in `test_account_balance_forecast_service.py` (`AccountBalanceForecastResponse(**result)` and assert the CC row's `cc_payments` survives validation). Either satisfies "response wiring + test"; prefer the endpoint test.

```python
"""The /api/v1/forecast/account-balances endpoint surfaces the synthesized
credit-card payment provenance (Credit Card Model V1, Slice 3)."""
from decimal import Decimal

from fastapi.testclient import TestClient
from tests.test_cc_cycle_payments import _make_app, session_factory, worlds  # noqa: F401


def _add_carried_charge(session_factory, a):
    """Insert a settled CC EXPENSE before this month's close, set cc.balance
    negative, ensure payment_source_account_id points at the checking account
    and both share currency. Adapt to the harness's real seed helpers."""
    ...  # see Task 2's _charge for the row shape


def test_account_balances_endpoint_exposes_cc_payments(session_factory, worlds):
    a = worlds["a"]
    _add_carried_charge(session_factory, a)
    app = _make_app(session_factory, a["admin_id"])
    with TestClient(app) as client:
        res = client.get("/api/v1/forecast/account-balances")
    assert res.status_code == 200, res.text
    rows = {r["account_id"]: r for r in res.json()["accounts"]}
    cc_row = rows[a["cc_id"]]
    assert "cc_payments" in cc_row
    assert len(cc_row["cc_payments"]) == 1
    assert set(cc_row["cc_payments"][0].keys()) == {"amount", "date"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/routers/test_forecast_account_balances_cc.py -v`
Expected: FAIL — `cc_payments` absent from the serialized row.

- [ ] **Step 3: Write the implementation**

In `backend/app/schemas/forecast.py`, add above the per-account row model:

```python
class CcPaymentLine(BaseModel):
    """A synthesized credit-card payment on the per-account forecast line
    (provenance source="credit_card_payment"). ``amount`` is the projected
    outflow on ``date`` (the resolved cycle due date)."""

    amount: Decimal
    date: datetime.date
```

and add to the per-account row model (after `expected_month_end_balance`):

```python
    cc_payments: list[CcPaymentLine] = []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-ccm1 exec -T backend pytest tests/routers/test_forecast_account_balances_cc.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/forecast.py backend/tests/routers/test_forecast_account_balances_cc.py
git commit -m "feat(forecast): expose synthesized cc payment provenance on the account-balance response"
```

---

## Task 4: Frontend "Payment {amount} on {date}" muted subline + test

**Files:**
- Modify: `frontend/components/dashboard/AccountMonthEndForecast.tsx`
- Modify: `frontend/tests/components/dashboard/account-month-end-forecast.test.tsx`

**Interfaces:**
- Consumes: the per-account row now carries optional `cc_payments?: { amount: string; date: string }[]` (`amount` a string Decimal, `date` an ISO `YYYY-MM-DD`).
- Produces: one quiet muted subline per `cc_payments` entry under the EOMF cell, in the existing "Includes … pending" idiom.

> **Placement (assumption, surfaced):** the visual idiom is locked (`text-[10px] tabular-nums text-text-muted`, no status color, currency via the existing `currencySymbol()` helper). The spec leaves exact placement an implementation call. This renders the line on the CC's row under the End-of-month-forecast cell (the same column that hosts "Includes … pending"). Date is the raw ISO string; a friendlier format is a follow-up.

- [ ] **Step 1: Write the failing test**

Append to `frontend/tests/components/dashboard/account-month-end-forecast.test.tsx` a fixture + describe block asserting a muted "Payment €500.00 on 2026-05-01" line renders from `cc_payments`, uses `text-text-muted` + `text-[10px]`, and renders nothing when `cc_payments` is absent/empty. Adapt the fixture shape + `render` helper to the file's real `AccountMonthEndForecastResponse`/`defaults` helpers.

```tsx
const CC_WITH_PAYMENT = {
  period_start: "2026-05-01", period_end: "2026-05-31",
  totals: [{ currency: "EUR", balance: "-500.00", pending_delta: "0.00", expected_month_end_balance: "-500.00" }],
  accounts: [{
    account_id: 1, account_name: "Visa", currency: "EUR", is_default: false,
    account_type_slug: "credit_card", balance: "-500.00", pending_delta: "0.00",
    expected_month_end_balance: "0.00",
    cc_payments: [{ amount: "500.00", date: "2026-05-01" }],
  }],
};

describe("AccountMonthEndForecast — credit-card projected payment", () => {
  it("renders a muted Payment line from cc_payments", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: CC_WITH_PAYMENT })} />);
    const line = screen.getByText(/Payment.*€500\.00 on 2026-05-01/);
    expect(line).toBeInTheDocument();
    expect(line.className).toContain("text-text-muted");
    expect(line.className).toContain("text-[10px]");
  });

  it("renders no payment line when cc_payments is absent or empty", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />);
    expect(screen.queryByText(/Payment.*on /)).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-ccm1 exec -T frontend npm test -- account-month-end-forecast`
Expected: FAIL — no element matches the Payment line.

- [ ] **Step 3: Write the implementation**

In `frontend/components/dashboard/AccountMonthEndForecast.tsx`:

3a. Add the optional field to the row type (after `expected_month_end_balance: string;`):

```tsx
  // Slice 3: synthesized credit-card payment(s) projected in this period.
  cc_payments?: { amount: string; date: string }[];
```

3b. In the row `.map(...)`, inside the right-aligned cell, AFTER the existing pending subline, add (reuse the row's already-computed currency symbol variable — read the file for its name):

```tsx
                  {(row.cc_payments ?? []).map((p, i) => (
                    <p key={`${p.date}-${i}`} className="text-[10px] tabular-nums text-text-muted">
                      Payment {pendingCurrencySymbol}{formatAmount(p.amount)} on {p.date}
                    </p>
                  ))}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-ccm1 exec -T frontend npm test -- account-month-end-forecast`
Expected: PASS.

- [ ] **Step 5: Type-check + lint + backend sanity**

Run: `docker compose -p team-ccm1 exec -T frontend npx tsc --noEmit` then `docker compose -p team-ccm1 exec -T frontend npx eslint components/dashboard/AccountMonthEndForecast.tsx tests/components/dashboard/account-month-end-forecast.test.tsx --quiet` then `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_cc_forecast_service.py tests/services/test_account_balance_forecast_service.py -q`
Expected: no type/lint errors; backend green.

- [ ] **Step 6: Commit**

```bash
git add frontend/components/dashboard/AccountMonthEndForecast.tsx frontend/tests/components/dashboard/account-month-end-forecast.test.tsx
git commit -m "feat(dashboard): render projected credit-card payment line on the month-end forecast"
```

---

## Self-review notes

**Spec requirement → task mapping**
- Synthesize in `account_balance_forecast_service`, NOT `forecast_service` (ephemeral delta, provenance) → Task 2; `forecast_service` confirmed untouched (Task 2 Step 5). Its `reportable_transaction_filter` excludes transfer legs, so a CC payment can never appear there.
- Carried-balance `B_k` as-of-close from its own windows → `balance_at_close` (Task 1) fed by the non-reverted signed ledger (Task 2). Filtering `eff <= close` is the BLOCKER fix.
- Unified amount resolution across all four strategies → `cc_target_payment` + `synthesize_account_cc_payments` (Task 1).
- `P_k_owned` (one owning cycle each) + `S_prev` threading → Task 1 orchestrator + Task 2 credits query.
- Cycle enumeration per active CC with non-null source over the horizon via the resolver → `due_cycles_in_horizon` + the `cc_accounts` filter.
- Edge cases: NULL source, cross-currency, inactive source (`accounts_by_id.get` None → skip), `outstanding==0` clamp, minimum/custom unset, zero due-dates, grace-period.
- Provenance schema + response + FE line → Tasks 3, 4.
- No schema/migration changes; Slice-2 endpoints/editor untouched.

**Locked-invariant → test mapping:** (a) full_balance→owed-at-close: T1 `test_synth_full_balance…` + T2 grace; (b) conservation: T2 `test_cc_synth_conservation…`; (c) fixed literal: T1/T2; (d) minimum/custom store: T1/T2; (e) NULL parity: T2; (f) cross-currency: T2; (g) card-in-credit: T1/T2; (h) grace/as-of-close: T1 `test_balance_at_close…` + T2; (i) two due dates/S_prev: T1/T2; (j) real-payment net: T1/T2; (k) clamp: T1/T2.

**Assumptions / deviations (call out at review)**
1. **NULL-source "byte-parity" is implemented as value-parity + empty provenance.** Adding `cc_payments: []` to every row changes the JSON shape (a new always-present key). The parity test asserts the money fields + totals are identical AND `cc_payments == []`. A consistent always-present key is a cleaner FE contract than a conditional one. If literal byte-parity is required, make the field optional and omit when empty.
2. **`B_k` ledger uses `non_reverted_transaction_filter`** (keeps manual adjustments + transfer legs, drops reverted skipped/rejected rows), settled + pending. This reconstructs the stored balance faithfully. If the reviewer wants manual adjustments excluded, `B_k` would diverge from the real balance.
3. **Provenance line placement is the CC account's row** in `AccountMonthEndForecast`; the outflow also silently moves the source's expected balance.
4. **Date rendering is the raw ISO string**; a localized format is a low-risk follow-up.
5. **Enumeration lookback** = `payment_day_relative_month + 1` months before `p_start`, with a `_MAX_WALK_ITERS` guard; `payment_date` monotonicity in the close date makes the forward-break correct.
