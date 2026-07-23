# Credit Card Model V1 — Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ship the two owner follow-ups on top of merged CC Model V1 (slices 1-3, alembic head 074) + a residual docstring fix, on branch `cc-model-v1-followups` as one PR. **F2** reworks per-cycle payment from a standing `payment_strategy` config into a **universal single-cycle override**: collapse `PaymentStrategy` to `{full_balance, fixed_amount}` (migration 075), make `cc_target_payment` **override-first** (the per-cycle store wins for ANY card), de-gate the editor "Upcoming payments" list + add a contextual dashboard "Change" link. **F1** adds a `dash_cc_utilization` **Dashboard widget** (banded bar per CC reusing `BudgetBarsWidget` + `BudgetSpentBarShape`) + a shared `lib/credit.ts` helper. **Residual:** correct the `linked_transaction_id` docstring.

**Architecture:** F2's only functional backend edits are the enum removal (`models/account.py`) and the override-first branch in pure `cc_forecast_service.cc_target_payment` (no DB, no caller change). Validation/endpoints unchanged. F1 is frontend-only except one validator widening: the real layout validator is `backend/app/schemas/dashboard.py` (`DashWidgetType` + `_DashboardWidget` union); `DEFAULT_DASHBOARD_LAYOUT` stays 7 tiles. The F1 chip reads Slice-3 `cc_payments` from `useDashboard().accountMonthEndForecast` (NOT `activeAccounts`), joined by `account_id`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic v2, MySQL 8 (SQLite in-memory for tests); Next.js 16 + React 19 + TS, Vitest + Testing Library, recharts.

## Global Constraints
- Python 3.12 / FastAPI / SQLAlchemy async / Pydantic v2 / MySQL 8. Frontend Next.js 16 / React 19 / TS.
- Pre-launch, NO backcompat (`feedback_pre_launch_state`) — enum narrowing + migration ok.
- Enums lowercase values (`values_callable`); migrations pass raw value tuples, no app-model import (045/057/073 idiom).
- Migration verified on REAL MySQL (upgrade → downgrade → upgrade) in the `-p team-ccm1` stack; SQLite CI cannot exercise `ALTER … MODIFY ENUM`. The NULL-reset `UPDATE` MUST precede the `MODIFY`.
- No em-dashes in user copy. No off-token colors (design-token CI check blocking — `var(--color-*)` / `chartColor` / `lib/styles.ts` only). No AI attribution.
- `team-ccm1` stack is ALREADY UP — do NOT `docker compose up`; run with `-T`. Frontend gates before commit: `tsc --noEmit` + `eslint <files> --quiet` + design-token check.

> **Executor note:** line numbers are plan-time anchors and may drift; re-locate by quoted surrounding code. The TDD loop catches drift.

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `backend/alembic/versions/075_collapse_payment_strategy.py` | Create | NULL-reset dropped rows, then narrow `account_payment_strategy` ENUM to 2 members; downgrade re-widens. |
| `backend/app/models/account.py` | Modify | Remove `MINIMUM_ONLY` + `CUSTOM_PER_PERIOD` from `PaymentStrategy`. |
| `backend/app/services/cc_forecast_service.py` | Modify | `cc_target_payment` → override-first. |
| `backend/tests/services/test_cc_forecast_service.py` | Modify | Replace dropped-strategy tests with override-first tests. |
| `backend/tests/services/test_account_balance_forecast_service.py` | Modify | Rewrite the `minimum_only` integration test as `full_balance`-with-override. |
| `backend/tests/test_account_credit_card_fields.py` | Modify | Drop removed enum members from parametrize + the `minimum_only` PUT literal. |
| `backend/app/models/transaction.py` | Modify | Correct `linked_transaction_id` docstring (two writers). |
| `backend/app/schemas/dashboard.py` | Modify | Add `DashWidgetType.CC_UTILIZATION` + `DashCcUtilizationWidget` to the validator union. |
| `backend/tests/routers/test_dashboard.py` | Modify | Assert a layout with `dash_cc_utilization` validates/saves; 7-tile seed unchanged. |
| `frontend/lib/credit.ts` | Create | `creditUtilization(balance, creditLimit)` helper. |
| `frontend/tests/lib/credit.test.ts` | Create | Helper unit tests. |
| `frontend/app/accounts/page.tsx` | Modify | De-gate Upcoming payments; drop 2 select options (both forms); reframe copy; refactor subline onto helper. |
| `frontend/lib/types.ts` | Modify | Narrow `Account.payment_strategy` to the 2 kept values. |
| `frontend/components/dashboard/AccountMonthEndForecast.tsx` | Modify | Quiet "Change" `btnLink` on the imminent Payment line. |
| `frontend/components/dashboard/widgets/CreditUtilizationBar.tsx` | Create | Horizontal banded bar per CC. |
| `frontend/components/dashboard/widgets/CreditUtilizationWidget.tsx` | Create | Reads `activeAccounts` + `accountMonthEndForecast`; filter/sort/chip/empty. |
| `frontend/lib/dashboard/widget-types.ts` | Modify | Add `dash_cc_utilization` union + `DASHBOARD_WIDGET_DEFAULTS` (w=4 h=6). |
| `frontend/components/dashboard/renderDashboardWidget.tsx` | Modify | Case arm → `CreditUtilizationWidget`. |
| `frontend/components/dashboard/AddWidgetMenu.tsx` | Modify | `DASH_TILES` entry (`CreditCard` icon). |
| `frontend/tests/lib/dashboard/widget-defaults.test.ts` | Modify | Add `dash_cc_utilization` to exhaustive grid/content-floor records. |
| `frontend/tests/components/dashboard/credit-utilization-widget.test.tsx` | Create | Bar bands + widget filtering/sort/empty/chip. |
| `frontend/tests/app/accounts-cc-model.test.tsx` | Modify | Fixture strategy → `full_balance`; assert Upcoming payments shows + removed options gone. |

---

## Task 1: F2 backend — migration 075 + enum collapse

**Files:** Create `backend/alembic/versions/075_collapse_payment_strategy.py`; Modify `backend/app/models/account.py`, `backend/tests/test_account_credit_card_fields.py`.

**Interfaces:** `account_payment_strategy` ENUM narrowed to `('full_balance','fixed_amount')`; `PaymentStrategy` with 2 members.

- [ ] **Step 1: Failing test (enum shape guard).** Append to `backend/tests/test_account_credit_card_fields.py`:

```python
def test_payment_strategy_enum_collapsed_to_two_members():
    """F2: minimum_only + custom_per_period are dropped; a per-cycle
    override (cc_cycle_payments) now expresses a single-cycle partial pay."""
    values = {e.value for e in PaymentStrategy}
    assert values == {"full_balance", "fixed_amount"}
    assert not hasattr(PaymentStrategy, "MINIMUM_ONLY")
    assert not hasattr(PaymentStrategy, "CUSTOM_PER_PERIOD")
```

- [ ] **Step 2: Run → FAIL.** `docker compose -p team-ccm1 exec -T backend pytest tests/test_account_credit_card_fields.py::test_payment_strategy_enum_collapsed_to_two_members -v` (enum still has 4). Note: `test_account_credit_card_fields.py` also references the members in a parametrize (~:312-320) and a PUT literal (~:417/:424) — Step 3c fixes them so removal doesn't `AttributeError` at collection.

- [ ] **Step 3: Implement.**
3a. `backend/app/models/account.py` — remove the two members:
```python
class PaymentStrategy(str, enum.Enum):
    FULL_BALANCE = "full_balance"
    FIXED_AMOUNT = "fixed_amount"
```
3b. Create `backend/alembic/versions/075_collapse_payment_strategy.py`:
```python
"""Collapse payment_strategy enum to {full_balance, fixed_amount} (F2).

Revision ID: 075_collapse_payment_strategy
Revises: 074_cc_cycle_payments
Create Date: 2026-07-22

F2 reframes per-cycle payment from a STANDING strategy config into a
universal SINGLE-CYCLE override (cc_cycle_payments, honored for any CC by
cc_forecast_service.cc_target_payment). The two members that mismodeled a
per-month decision are dropped:
  keep: full_balance (default, NULL-at-rest), fixed_amount
  drop: minimum_only, custom_per_period
Rows on a dropped strategy reset to NULL (= full_balance default); their
amounts survive as plain overrides in cc_cycle_payments (lossless in intent;
pre-launch, no backcompat). Ordering is load-bearing: the NULL-reset UPDATE
MUST run BEFORE the MODIFY, or MySQL truncates/errors on out-of-set rows.
Raw value tuples (no app-model import), mirroring 045/057/073. VERIFY on a
real MySQL 8 container (upgrade + downgrade + re-upgrade) — SQLite CI cannot
exercise ALTER ... MODIFY ENUM.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


revision: str = "075_collapse_payment_strategy"
down_revision: Union[str, None] = "074_cc_cycle_payments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_NEW = "ENUM('full_balance','fixed_amount')"
_ENUM_OLD = "ENUM('full_balance','minimum_only','fixed_amount','custom_per_period')"


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(
        text(
            "UPDATE accounts SET payment_strategy = NULL "
            "WHERE payment_strategy IN ('minimum_only', 'custom_per_period')"
        )
    )
    if bind.dialect.name == "mysql":
        op.execute(
            text(f"ALTER TABLE accounts MODIFY COLUMN payment_strategy {_ENUM_NEW} NULL")
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.execute(
            text(f"ALTER TABLE accounts MODIFY COLUMN payment_strategy {_ENUM_OLD} NULL")
        )
```
3c. `backend/tests/test_account_credit_card_fields.py`: drop `PaymentStrategy.MINIMUM_ONLY` + `PaymentStrategy.CUSTOM_PER_PERIOD` from the `test_fixed_payment_forbidden_for_non_fixed_strategy` parametrize (leave `FULL_BALANCE`, `None`); change the PUT literal `"payment_strategy": "minimum_only"` → `"full_balance"` and its assertion `== "minimum_only"` → `== "full_balance"`.

- [ ] **Step 4: Run → PASS.** `docker compose -p team-ccm1 exec -T backend pytest tests/test_account_credit_card_fields.py -v`

- [ ] **Step 5: Verify migration on REAL MySQL.**
```bash
docker compose -p team-ccm1 exec -T backend alembic upgrade head
docker compose -p team-ccm1 exec -T backend alembic downgrade -1
docker compose -p team-ccm1 exec -T backend alembic upgrade head
docker compose -p team-ccm1 exec -T backend alembic current
```
Expected: all succeed; head = `075_collapse_payment_strategy`; no truncation.

- [ ] **Step 6: Commit.**
```bash
git add backend/alembic/versions/075_collapse_payment_strategy.py backend/app/models/account.py backend/tests/test_account_credit_card_fields.py
git commit -m "feat(cc): collapse payment_strategy enum to full_balance + fixed_amount (migration 075)"
```

---

## Task 2: F2 backend — override-first resolver

**Files:** Modify `backend/app/services/cc_forecast_service.py`, `backend/tests/services/test_cc_forecast_service.py`, `backend/tests/services/test_account_balance_forecast_service.py`.

**Interfaces:** `cc_target_payment(account, cycle, outstanding, per_cycle_amounts) -> Decimal` — override-first (anchor wins for ANY strategy; elif fixed_amount → literal; else outstanding). Signature + caller unchanged.

- [ ] **Step 1: Failing tests.** In `test_cc_forecast_service.py`, REPLACE the two dropped-strategy parametrized tests with:
```python
def test_target_override_wins_for_full_balance():
    acct = _FakeAccount(payment_strategy="full_balance")
    cyc = _cycle(acct)  # close Apr 25 -> anchor (id, 2026, 4)
    per_cycle = {(acct.id, 2026, 4): Decimal("75.00")}
    assert svc.cc_target_payment(acct, cyc, Decimal("1200.00"), per_cycle) == Decimal("75.00")


def test_target_override_wins_for_none_strategy():
    acct = _FakeAccount(payment_strategy=None)
    cyc = _cycle(acct)
    per_cycle = {(acct.id, 2026, 4): Decimal("40.00")}
    assert svc.cc_target_payment(acct, cyc, Decimal("900.00"), per_cycle) == Decimal("40.00")


def test_target_override_wins_over_fixed_amount():
    acct = _FakeAccount(payment_strategy="fixed_amount", fixed_payment_amount=Decimal("150.00"))
    cyc = _cycle(acct)
    per_cycle = {(acct.id, 2026, 4): Decimal("60.00")}
    assert svc.cc_target_payment(acct, cyc, Decimal("1200.00"), per_cycle) == Decimal("60.00")


def test_target_full_balance_without_override_is_outstanding():
    acct = _FakeAccount(payment_strategy="full_balance")
    assert svc.cc_target_payment(acct, _cycle(acct), Decimal("1200.00"), {}) == Decimal("1200.00")


def test_target_fixed_amount_without_override_is_literal():
    acct = _FakeAccount(payment_strategy="fixed_amount", fixed_payment_amount=Decimal("150.00"))
    assert svc.cc_target_payment(acct, _cycle(acct), Decimal("1200.00"), {}) == Decimal("150.00")
```
(Leave existing no-override tests green.)

- [ ] **Step 2: Run → FAIL.** `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_cc_forecast_service.py -k "override" -v`

- [ ] **Step 3: Implement.** Rewrite `cc_target_payment` in `cc_forecast_service.py`:
```python
def cc_target_payment(
    account: object,
    cycle: CreditCardCycle,
    outstanding: Decimal,
    per_cycle_amounts: dict[tuple[int, int, int], Decimal],
) -> Decimal:
    """Per-cycle target BEFORE the uniform clamp + net.

    Override-first (F2): a stored per-cycle amount, anchored on the cycle's
    CLOSE month, wins for ANY strategy — a single-cycle "pay X this cycle"
    decision that never auto-carries. With no override, fixed_amount pays its
    literal and full_balance / NULL pays the whole outstanding balance.
    """
    anchor = (
        account.id,
        cycle.period_end_inclusive.year,
        cycle.period_end_inclusive.month,
    )
    if anchor in per_cycle_amounts:
        return per_cycle_amounts[anchor]
    if _strategy_value(account) == "fixed_amount":
        amt = getattr(account, "fixed_payment_amount", None)
        return Decimal(str(amt)) if amt is not None else Decimal("0")
    return outstanding
```
Keep `_strategy_value`, `balance_at_close`, `outstanding_at_close`, `due_cycles_in_horizon`, `synthesize_account_cc_payments` untouched.

- [ ] **Step 4: Run → PASS.** `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_cc_forecast_service.py -v`

- [ ] **Step 5: Rewrite the integration test using a dropped strategy.** In `test_account_balance_forecast_service.py`, replace `test_cc_synth_minimum_only_reads_store_and_zero_when_unset` with:
```python
async def test_cc_synth_override_applies_to_full_balance(db_session: AsyncSession):
    """F2: a per-cycle override is honored on a full_balance card."""
    seed = await _seed_cc(db_session)  # default strategy = FULL_BALANCE
    cc = seed["cc"]
    db_session.add(_charge(seed, cc, amount="900.00", on=datetime.date(2026, 4, 10)))
    cc.balance = Decimal("-900.00")
    await db_session.commit()
    r1 = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    assert next(a for a in r1["accounts"] if a["account_id"] == cc.id)["cc_payments"] == [
        {"amount": "900.00", "date": "2026-05-01"}]
    db_session.add(CcCyclePayment(account_id=cc.id, period_anchor_year=2026,
                                  period_anchor_month=4, amount=Decimal("75.00")))
    await db_session.commit()
    r2 = await compute_account_balance_forecast(db_session, seed["org_id"], period_start=PERIOD_START)
    assert next(a for a in r2["accounts"] if a["account_id"] == cc.id)["cc_payments"] == [
        {"amount": "75.00", "date": "2026-05-01"}]
```

- [ ] **Step 6: Run forecast suites → PASS.** `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_cc_forecast_service.py tests/services/test_account_balance_forecast_service.py -v`

- [ ] **Step 7: Commit.**
```bash
git add backend/app/services/cc_forecast_service.py backend/tests/services/test_cc_forecast_service.py backend/tests/services/test_account_balance_forecast_service.py
git commit -m "feat(cc): resolve per-cycle payment override-first for any card"
```

---

## Task 3: F2 frontend — de-gate editor, drop options, reframe copy

**Files:** Modify `frontend/app/accounts/page.tsx`, `frontend/lib/types.ts`, `frontend/tests/app/accounts-cc-model.test.tsx`.

- [ ] **Step 1: Failing test.** In `accounts-cc-model.test.tsx`, set the CC fixture `payment_strategy: "full_balance"` and add:
```tsx
it("shows Upcoming payments for a full_balance CC and hides removed strategy options", async () => {
  renderPage();
  await openEditRow(11);
  expect(screen.getByText("Upcoming payments")).toBeInTheDocument();
  expect(screen.getByText(/Paying the full balance by default\. Enter a different amount/)).toBeInTheDocument();
  const select = screen.getByLabelText("Payment strategy") as HTMLSelectElement;
  const optionValues = Array.from(select.options).map((o) => o.value);
  expect(optionValues).not.toContain("minimum_only");
  expect(optionValues).not.toContain("custom_per_period");
  expect(optionValues).toEqual(["", "full_balance", "fixed_amount"]);
});
```
Adapt `renderPage`/`openEditRow` to the file's real helpers.

- [ ] **Step 2: Run → FAIL.** `docker compose -p team-ccm1 exec -T frontend npm test -- accounts-cc-model`

- [ ] **Step 3: Implement.**
3a. Create-form select (~:895-900) → only `(default)`, `Pay full balance`, `Pay a fixed amount`.
3b. Edit-form select (~:1090-1095) → same three options.
3c. Fetch `useEffect` (~:437-464): drop the `perCycle` gate; deps `[editAcctId, editingTypeSlug]`; fetch for any `credit_card`:
```tsx
  useEffect(() => {
    if (editAcctId == null || editingTypeSlug !== "credit_card") {
      setUpcomingCycles([]);
      setCycleDrafts({});
      return;
    }
    let cancelled = false;
    apiFetch<UpcomingCyclePayment[]>(`/api/v1/accounts/${editAcctId}/cycle-payments`)
      .then((rows) => {
        if (cancelled) return;
        setUpcomingCycles(rows);
        setCycleDrafts(Object.fromEntries(rows.map((r) => [`${r.year}-${r.month}`, r.amount ?? ""])));
      })
      .catch(() => {
        if (!cancelled) { setUpcomingCycles([]); setCycleDrafts({}); }
      });
    return () => { cancelled = true; };
  }, [editAcctId, editingTypeSlug]);
```
3d. Render gate + copy (~:1110-1117): drop the strategy condition; helper copy:
```tsx
                    {editingTypeSlug === "credit_card" && (
                        <div className="w-full">
                          <div className={label}>Upcoming payments</div>
                          <p className="mb-2 text-xs text-text-muted">
                            Paying the full balance by default. Enter a different amount for any cycle you plan to pay partially.
                          </p>
```
(Keep the empty branch, per-row input, Clear, closing tags.)
3e. Narrow `frontend/lib/types.ts` `Account.payment_strategy` to `"full_balance" | "fixed_amount" | null`.

- [ ] **Step 4: Run → PASS + gates.** `npm test -- accounts-cc-model`, then `npx tsc --noEmit`, then `npx eslint app/accounts/page.tsx lib/types.ts tests/app/accounts-cc-model.test.tsx --quiet` (all via `docker compose -p team-ccm1 exec -T frontend …`).

- [ ] **Step 5: Commit.**
```bash
git add frontend/app/accounts/page.tsx frontend/lib/types.ts frontend/tests/app/accounts-cc-model.test.tsx
git commit -m "feat(accounts): offer per-cycle payment override for any credit card"
```

---

## Task 4: F2 frontend — contextual "Change" link on the forecast payment line

**Files:** Modify `frontend/components/dashboard/AccountMonthEndForecast.tsx`, `frontend/tests/components/dashboard/account-month-end-forecast.test.tsx`.

> **Deep-link note:** the accounts page opens its editor via `startEditAcct(a)` (`useState`), with NO `?edit=<id>` param (no `next/navigation` import). The "Change" link targets `/accounts` (minimal correct); a cycle-specific deep-link is out of scope (noted).

- [ ] **Step 1: Failing test.** Append:
```tsx
describe("AccountMonthEndForecast — contextual Change link", () => {
  it("renders a Change link on the imminent payment line pointing at /accounts", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: CC_WITH_PAYMENT })} />);
    const change = screen.getByRole("link", { name: /change/i });
    expect(change).toBeInTheDocument();
    expect(change.getAttribute("href")).toBe("/accounts");
  });
  it("renders no Change link when there are no cc_payments", () => {
    render(<AccountMonthEndForecast {...defaults({ forecast: TWO_ACCOUNTS_EUR })} />);
    expect(screen.queryByRole("link", { name: /change/i })).toBeNull();
  });
});
```

- [ ] **Step 2: Run → FAIL.** `docker compose -p team-ccm1 exec -T frontend npm test -- account-month-end-forecast`

- [ ] **Step 3: Implement.** Add imports (`import Link from "next/link";` and extend the `@/lib/styles` import with `btnLink`), then replace the `cc_payments.map` block:
```tsx
                  {(row.cc_payments ?? []).map((p, i) => (
                    <p key={`${p.date}-${i}`} className="text-[10px] tabular-nums text-text-muted">
                      Payment {pendingCurrencySymbol}{formatAmount(p.amount)} on {p.date}
                      {i === 0 && (
                        <> <Link href="/accounts" className={btnLink}>Change</Link></>
                      )}
                    </p>
                  ))}
```

- [ ] **Step 4: Run → PASS + gates.** `npm test -- account-month-end-forecast`; `npx tsc --noEmit`; `npx eslint components/dashboard/AccountMonthEndForecast.tsx tests/components/dashboard/account-month-end-forecast.test.tsx --quiet`.

- [ ] **Step 5: Commit.**
```bash
git add frontend/components/dashboard/AccountMonthEndForecast.tsx frontend/tests/components/dashboard/account-month-end-forecast.test.tsx
git commit -m "feat(dashboard): add contextual Change link to the projected cc payment line"
```

---

## Task 5: F1 — shared `lib/credit.ts` + accounts subline refactor

**Files:** Create `frontend/lib/credit.ts`, `frontend/tests/lib/credit.test.ts`; Modify `frontend/app/accounts/page.tsx`.

**Interfaces:** `creditUtilization(balance, creditLimit) => { outstanding, utilizationPct, available, over }` (liabilities negative; outstanding=max(0,-balance); util=outstanding/limit*100 uncapped; available=limit+balance; over=outstanding-limit).

- [ ] **Step 1: Failing tests.** Create `frontend/tests/lib/credit.test.ts`:
```ts
import { describe, expect, it } from "vitest";
import { creditUtilization } from "@/lib/credit";

describe("creditUtilization", () => {
  it("computes outstanding, util%, available, over for a mid-utilization card", () => {
    const r = creditUtilization(-500, 2000);
    expect(r.outstanding).toBe(500);
    expect(r.utilizationPct).toBe(25);
    expect(r.available).toBe(1500);
    expect(r.over).toBe(-1500);
  });
  it("treats a positive (in-credit) balance as zero outstanding", () => {
    const r = creditUtilization(120, 2000);
    expect(r.outstanding).toBe(0);
    expect(r.utilizationPct).toBe(0);
    expect(r.available).toBe(2120);
    expect(r.over).toBe(-2000);
  });
  it("reports over-limit with an uncapped util% and positive over", () => {
    const r = creditUtilization(-2500, 2000);
    expect(r.outstanding).toBe(2500);
    expect(r.utilizationPct).toBe(125);
    expect(r.available).toBe(-500);
    expect(r.over).toBe(500);
  });
});
```

- [ ] **Step 2: Run → FAIL.** `docker compose -p team-ccm1 exec -T frontend npm test -- credit`

- [ ] **Step 3: Implement.** Create `frontend/lib/credit.ts`:
```ts
/**
 * Shared credit-card utilization math. Single home so the accounts-page
 * subline and the CreditUtilizationWidget can't drift (F1). Liabilities are
 * stored NEGATIVE, so an owed card has a negative balance.
 */
export interface CreditUtilization {
  outstanding: number;
  utilizationPct: number;
  available: number;
  over: number;
}

export function creditUtilization(balance: number, creditLimit: number): CreditUtilization {
  const outstanding = Math.max(0, -balance);
  const utilizationPct = creditLimit > 0 ? (outstanding / creditLimit) * 100 : 0;
  const available = creditLimit + balance;
  const over = outstanding - creditLimit;
  return { outstanding, utilizationPct, available, over };
}
```
3b. Refactor the accounts subline (~:1287-1309) to consume the helper (add `import { creditUtilization } from "@/lib/credit";`), keeping the displayed rounding + copy identical:
```tsx
                      {a.account_type_slug === "credit_card" && Number(a.credit_limit) > 0
                        ? (() => {
                            const { outstanding, utilizationPct, available, over } =
                              creditUtilization(Number(a.balance), Number(a.credit_limit));
                            const util = Math.round(utilizationPct);
                            let text: string;
                            if (outstanding === 0) {
                              text = "0% used · full limit available";
                            } else if (over > 0) {
                              text = `Using ${util}% of limit · ${formatAmount(over)} ${a.currency} over`;
                            } else {
                              text = `Using ${util}% of limit · ${formatAmount(available)} ${a.currency} left`;
                            }
                            return (
                              <span className="text-xs tabular-nums text-text-muted">{text}</span>
                            );
                          })()
                        : null}
```

- [ ] **Step 4: Run → PASS + gates.** `npm test -- credit accounts-cc-model`; `npx tsc --noEmit`; `npx eslint lib/credit.ts app/accounts/page.tsx tests/lib/credit.test.ts --quiet`.

- [ ] **Step 5: Commit.**
```bash
git add frontend/lib/credit.ts frontend/tests/lib/credit.test.ts frontend/app/accounts/page.tsx
git commit -m "refactor(credit): extract shared creditUtilization helper and reuse in accounts subline"
```

---

## Task 6: F1 — `CreditUtilizationBar` banded bar

**Files:** Create `frontend/components/dashboard/widgets/CreditUtilizationBar.tsx`; Create `frontend/tests/components/dashboard/credit-utilization-widget.test.tsx` (bar section).

**Interfaces:** `CreditUtilizationBar({ name, balance, creditLimit, currency })` — domain clamped to 100; fill `util>=100 ? chartColor.over : util>=75 ? "var(--color-warning)" : chartColor.watch`; track `chartColor.remaining`; over-limit label `Over limit · {over} {ccy} over`; band paired with text.

- [ ] **Step 1: Failing tests.** Create `credit-utilization-widget.test.tsx`:
```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import CreditUtilizationBar from "@/components/dashboard/widgets/CreditUtilizationBar";

describe("CreditUtilizationBar", () => {
  it("labels a low-utilization card with just the percent (neutral band)", () => {
    render(<CreditUtilizationBar name="Visa" balance={-500} creditLimit={2000} currency="EUR" />);
    expect(screen.getByText("Visa")).toBeInTheDocument();
    expect(screen.getByText(/25%/)).toBeInTheDocument();
    expect(screen.queryByText(/Over limit/)).toBeNull();
  });
  it("labels a high-utilization card (>=75%) with High", () => {
    render(<CreditUtilizationBar name="Amex" balance={-1700} creditLimit={2000} currency="EUR" />);
    expect(screen.getByText(/85%/)).toBeInTheDocument();
    expect(screen.getByText(/High/)).toBeInTheDocument();
  });
  it("labels an over-limit card with the overage in currency", () => {
    render(<CreditUtilizationBar name="Store" balance={-2500} creditLimit={2000} currency="EUR" />);
    expect(screen.getByText(/Over limit/)).toBeInTheDocument();
    expect(screen.getByText(/500/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run → FAIL.** `docker compose -p team-ccm1 exec -T frontend npm test -- credit-utilization-widget`

- [ ] **Step 3: Implement.** Create `CreditUtilizationBar.tsx` (reuse BudgetBars idiom). READ `BudgetBarsWidget.tsx` + `lib/chart-shapes.tsx` + `lib/chart-colors.ts` first and match the real `chartColor` keys / `BudgetSpentBarShape` prop type:
```tsx
"use client";

/**
 * CreditUtilizationBar — one horizontal banded bar for a single credit card,
 * reusing the BudgetBars idiom (BudgetSpentBarShape + chartColor tokens). The
 * numeric domain is clamped to 100 so the fill maxes at the track; the overage
 * is surfaced in the text label, never by letting the bar exceed the track.
 * Bands (color earned only at the risky end; color never the sole signal):
 * util >= 100 -> over (danger); 75 <= util < 100 -> warning ("High");
 * util < 75 -> neutral watch (the % carries it).
 */
import { BarChart, Bar, XAxis, YAxis, Cell, ResponsiveContainer } from "recharts";
import { chartColor } from "@/lib/chart-colors";
import { creditUtilization } from "@/lib/credit";
import { formatAmount } from "@/lib/format";
import { BudgetSpentBarShape, type BudgetSpentBarShapeProps } from "@/lib/chart-shapes";

export interface CreditUtilizationBarProps {
  name: string;
  balance: number;
  creditLimit: number;
  currency: string;
}

export default function CreditUtilizationBar({ name, balance, creditLimit, currency }: CreditUtilizationBarProps) {
  const { utilizationPct, over } = creditUtilization(balance, creditLimit);
  const util = Math.round(utilizationPct);
  const used = Math.min(utilizationPct, 100);
  const remaining = Math.max(0, 100 - utilizationPct);
  const isOver = utilizationPct >= 100;
  const isHigh = utilizationPct >= 75 && utilizationPct < 100;
  const fill = isOver ? chartColor.over : isHigh ? "var(--color-warning)" : chartColor.watch;
  const data = [{ name, used, remaining, over: isOver ? 1 : 0 }];
  const bandLabel = isOver
    ? `Over limit · ${formatAmount(over)} ${currency} over`
    : isHigh ? "High" : `${util}%`;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between text-xs text-text-secondary">
        <span className="truncate">{name}</span>
        <span className="tabular-nums">{isOver || isHigh ? `${util}% · ${bandLabel}` : bandLabel}</span>
      </div>
      <div className="h-4 w-full">
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
          <BarChart data={data} layout="vertical" margin={{ left: 0, right: 0, top: 0, bottom: 0 }}>
            <XAxis type="number" domain={[0, 100]} hide />
            <YAxis type="category" dataKey="name" hide />
            <Bar dataKey="used" stackId="a" shape={(props: BudgetSpentBarShapeProps) => <BudgetSpentBarShape {...props} />}>
              <Cell fill={fill} />
            </Bar>
            <Bar dataKey="remaining" stackId="a" fill={chartColor.remaining} radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
```
> If the real `BudgetSpentBarShape` prop type / `chartColor` keys differ, adapt to them (that is the source of truth); keep the band logic + labels identical.

- [ ] **Step 4: Run → PASS + gates + design-token check.** `npm test -- credit-utilization-widget`; `npx tsc --noEmit`; `npx eslint components/dashboard/widgets/CreditUtilizationBar.tsx --quiet`; run `frontend/scripts/check-design-tokens.sh` if present.

- [ ] **Step 5: Commit.**
```bash
git add frontend/components/dashboard/widgets/CreditUtilizationBar.tsx frontend/tests/components/dashboard/credit-utilization-widget.test.tsx
git commit -m "feat(dashboard): add banded credit-utilization bar reusing the budget bar idiom"
```

---

## Task 7: F1 — `CreditUtilizationWidget` (filter, sort, chip, empty)

**Files:** Create `frontend/components/dashboard/widgets/CreditUtilizationWidget.tsx`; extend `credit-utilization-widget.test.tsx`.

**Interfaces:** reads `activeAccounts` + `accountMonthEndForecast` from `useDashboard()`; filters `credit_card` + `credit_limit > 0`; sorts by utilization desc; one `CreditUtilizationBar` per CC; `badgeNeutral` "Next payment {amount} {ccy} on {date}" chip from the CC's forecast `cc_payments[0]`; empty states.

> **Chip data note:** `activeAccounts` (`Account[]`) has NO `cc_payments`; that lives on `AccountMonthEndForecastRow` in `useDashboard().accountMonthEndForecast`. Read BOTH and join by `account_id`.

- [ ] **Step 1: Failing tests.** Extend the test file (mock `useDashboard` at the module boundary, mirroring the dashboard registry tests). Include: renders a bar per CC sorted desc, excludes non-CC + null-limit (with "No limit set" when it has a balance), empty state with `/accounts` link when no CCs, "Next payment" chip from forecast. READ `DashboardDataProvider` for the real `useDashboard` return shape (`activeAccounts`, `accountMonthEndForecast`) and adapt the mock.

- [ ] **Step 2: Run → FAIL.** `docker compose -p team-ccm1 exec -T frontend npm test -- credit-utilization-widget`

- [ ] **Step 3: Implement.** Create `CreditUtilizationWidget.tsx` (READ `AccountsWidget.tsx` + `DashboardDataProvider` + `lib/styles.ts` `card`/`cardHeader`/`cardTitle`/`badgeNeutral` for the real names):
```tsx
"use client";

/**
 * CreditUtilizationWidget — "Credit card utilization" dashboard tile. Reads
 * activeAccounts (balances + limits) AND accountMonthEndForecast (Slice-3
 * cc_payments) from DashboardDataProvider, joins by account_id, renders one
 * banded bar per credit card sorted by utilization desc. A quiet "Next
 * payment" chip shows when the forecast projects one.
 */
import { useMemo } from "react";
import Link from "next/link";
import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import CreditUtilizationBar from "@/components/dashboard/widgets/CreditUtilizationBar";
import { creditUtilization } from "@/lib/credit";
import { formatAmount } from "@/lib/format";
import { badgeNeutral, card, cardHeader, cardTitle } from "@/lib/styles";

export default function CreditUtilizationWidget() {
  const { activeAccounts, accountMonthEndForecast } = useDashboard();
  const creditCards = useMemo(
    () => activeAccounts.filter((a) => a.account_type_slug === "credit_card"),
    [activeAccounts],
  );
  const withLimit = useMemo(
    () => creditCards.filter((a) => Number(a.credit_limit) > 0).slice().sort(
      (a, b) =>
        creditUtilization(Number(b.balance), Number(b.credit_limit)).utilizationPct -
        creditUtilization(Number(a.balance), Number(a.credit_limit)).utilizationPct,
    ),
    [creditCards],
  );
  const noLimit = useMemo(
    () => creditCards.filter((a) => !(Number(a.credit_limit) > 0) && Number(a.balance) !== 0),
    [creditCards],
  );
  const nextPaymentByAccount = useMemo(() => {
    const map: Record<number, { amount: string; date: string }> = {};
    for (const row of accountMonthEndForecast?.accounts ?? []) {
      const first = row.cc_payments?.[0];
      if (first) map[row.account_id] = first;
    }
    return map;
  }, [accountMonthEndForecast]);
  return (
    <div className={`${card} flex flex-col overflow-hidden`}>
      <div className={`flex items-center justify-between ${cardHeader}`}>
        <h2 className={cardTitle}>Credit card utilization</h2>
        <Link href="/accounts" className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary">Accounts</Link>
      </div>
      {creditCards.length === 0 ? (
        <div className="px-5 py-6 text-center text-sm text-text-muted">
          No credit cards yet.{" "}
          <Link href="/accounts" className="text-text-primary underline underline-offset-2 hover:text-text-secondary">Add one</Link>
        </div>
      ) : (
        <div className="flex flex-col gap-4 p-4">
          {withLimit.map((a) => {
            const next = nextPaymentByAccount[a.id];
            return (
              <div key={a.id} className="flex flex-col gap-1.5">
                <CreditUtilizationBar name={a.name} balance={Number(a.balance)} creditLimit={Number(a.credit_limit)} currency={a.currency} />
                {next && (
                  <span className={badgeNeutral}>Next payment {formatAmount(next.amount)} {a.currency} on {next.date}</span>
                )}
              </div>
            );
          })}
          {noLimit.map((a) => (
            <div key={a.id} className="flex items-center justify-between text-xs text-text-muted">
              <span className="truncate">{a.name}</span>
              <span>No limit set</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```
> If `useDashboard`'s field for the forecast is named differently than `accountMonthEndForecast`, or `card`/`cardHeader`/`cardTitle`/`badgeNeutral` differ, adapt to the real names.

- [ ] **Step 4: Run → PASS + gates.** `npm test -- credit-utilization-widget`; `npx tsc --noEmit`; `npx eslint components/dashboard/widgets/CreditUtilizationWidget.tsx --quiet`.

- [ ] **Step 5: Commit.**
```bash
git add frontend/components/dashboard/widgets/CreditUtilizationWidget.tsx frontend/tests/components/dashboard/credit-utilization-widget.test.tsx
git commit -m "feat(dashboard): add credit-card utilization widget with next-payment chip"
```

---

## Task 8: F1 — register `dash_cc_utilization` (frontend)

**Files:** Modify `frontend/lib/dashboard/widget-types.ts`, `frontend/components/dashboard/renderDashboardWidget.tsx`, `frontend/components/dashboard/AddWidgetMenu.tsx`, `frontend/tests/lib/dashboard/widget-defaults.test.ts`.

> **Exhaustive-Record note:** `DASHBOARD_WIDGET_DEFAULTS` + the test's `CANONICAL_GRIDS`/`MIN_CONTENT_H` are `Record<DashboardWidgetType, …>` — adding the union member REQUIRES a key in each or `tsc` fails. `dash_cc_utilization` is intentionally NOT in the backend `DEFAULT_DASHBOARD_LAYOUT`.

- [ ] **Step 1: Failing test.** Add `dash_cc_utilization` to both exhaustive records in `widget-defaults.test.ts` (e.g. `CANONICAL_GRIDS: { x:0, y:25, w:4, h:6 }`, `MIN_CONTENT_H: 6`) — matching the real record names + shapes (READ the test first).

- [ ] **Step 2: Run → FAIL.** `docker compose -p team-ccm1 exec -T frontend npx tsc --noEmit` (missing key) / `npm test -- widget-defaults`.

- [ ] **Step 3: Implement.**
3a. `widget-types.ts`: add `"dash_cc_utilization"` to the `DashboardWidgetType` union + a `DASHBOARD_WIDGET_DEFAULTS` entry (title "Credit card utilization", `grid { w:4, h:6 }`, matching the real entry shape).
3b. `renderDashboardWidget.tsx`: `import CreditUtilizationWidget …` + `case "dash_cc_utilization": return fill(<CreditUtilizationWidget />);`.
3c. `AddWidgetMenu.tsx`: append a `DASH_TILES` entry (`type: "dash_cc_utilization"`, label "Credit card utilization", description, `Icon: CreditCard` — already imported).

- [ ] **Step 4: Run → PASS + gates.** `npm test -- widget-defaults dashboard-widget-registry`; `npx tsc --noEmit`; `npx eslint lib/dashboard/widget-types.ts components/dashboard/renderDashboardWidget.tsx components/dashboard/AddWidgetMenu.tsx tests/lib/dashboard/widget-defaults.test.ts --quiet`.

- [ ] **Step 5: Commit.**
```bash
git add frontend/lib/dashboard/widget-types.ts frontend/components/dashboard/renderDashboardWidget.tsx frontend/components/dashboard/AddWidgetMenu.tsx frontend/tests/lib/dashboard/widget-defaults.test.ts
git commit -m "feat(dashboard): register dash_cc_utilization widget in the canvas registry"
```

---

## Task 9: F1 — backend layout validator accepts `dash_cc_utilization`

**Files:** Modify `backend/app/schemas/dashboard.py`, `backend/tests/routers/test_dashboard.py`.

> The real Pydantic validator is `schemas/dashboard.py` (`DashWidgetType` + `_DashboardWidget`); the router only owns `DEFAULT_DASHBOARD_LAYOUT` (leave it — stays 7 tiles, parity test green).

- [ ] **Step 1: Failing test.** Append to `test_dashboard.py` (mirror the existing chart-tile-accept test): PATCH `/api/v1/dashboard` with a layout containing a `dash_cc_utilization` widget → expect 200 and verbatim round-trip. READ the file's real `_seed`/`_make_app`/`_resolver` helpers + the existing accept test and adapt.

- [ ] **Step 2: Run → FAIL (422).** `docker compose -p team-ccm1 exec -T backend pytest tests/routers/test_dashboard.py -k "cc_utilization" -v`

- [ ] **Step 3: Implement.** In `backend/app/schemas/dashboard.py`: add `CC_UTILIZATION = "dash_cc_utilization"` to `DashWidgetType`; add a `DashCcUtilizationWidget(_DashWidgetBase)` with `type: Literal[DashWidgetType.CC_UTILIZATION]` + the standard `config` default; add it to the `_DashboardWidget` union. Match the real base-class/config names.

- [ ] **Step 4: Run → PASS.** `docker compose -p team-ccm1 exec -T backend pytest tests/routers/test_dashboard.py -v` (new test green; the "seven tiles" default test still passes).

- [ ] **Step 5: Commit.**
```bash
git add backend/app/schemas/dashboard.py backend/tests/routers/test_dashboard.py
git commit -m "feat(dashboard): accept dash_cc_utilization in the layout validator"
```

---

## Task 10: Residual — `linked_transaction_id` docstring fix

**Files:** Modify `backend/app/models/transaction.py`. Docstring only.

- [ ] **Step 1: Implement.** In `backend/app/models/transaction.py`, replace the `linked_transaction_id` docstring block (~:52-55) with an accurate description of BOTH writers:
```python
    ``linked_transaction_id`` has two writers. ``transaction_service._link_pair``
    sets it **bidirectionally** for transfer pairs and import pairing (and
    ``unpair_transactions`` clears that pairing). ``reconciliation_service._apply_match``
    also writes it **one-way** to point an imported row at its reconcile match.
    Because a reconcile match reuses this column, forecast/balance queries must
    gate CC payment-in legs with ``balance_contribution_filter`` (transaction_filters.py)
    rather than assume every ``linked_transaction_id`` is a transfer leg (the Slice-3 gotcha).
```
Match the surrounding docstring style; adapt names if the real helpers differ.

- [ ] **Step 2: Verify.** `docker compose -p team-ccm1 exec -T backend python -c "import app.models.transaction"` then `docker compose -p team-ccm1 exec -T backend pytest tests/ -k "transaction and (link or pair or reconcil)" -q` → import clean; unaffected.

- [ ] **Step 3: Commit.**
```bash
git add backend/app/models/transaction.py
git commit -m "docs(transaction): describe both linked_transaction_id writers (pairing + reconcile)"
```

---

## Final whole-branch verification (before the single PR)
- [ ] Backend: `docker compose -p team-ccm1 exec -T backend pytest tests/services/test_cc_forecast_service.py tests/services/test_account_balance_forecast_service.py tests/test_account_credit_card_fields.py tests/routers/test_dashboard.py -q`
- [ ] Migration round-trip re-confirmed on MySQL (Task 1 Step 5).
- [ ] Frontend: `docker compose -p team-ccm1 exec -T frontend npm test -- credit credit-utilization-widget widget-defaults accounts-cc-model account-month-end-forecast dashboard-widget-registry`
- [ ] Gates: `npx tsc --noEmit` + `npx eslint . --quiet` (or the changed set) + design-token check.
- [ ] ONE PR for `cc-model-v1-followups`. No AI attribution.

## Self-review notes
Each spec ruling maps to a task (F2 migration/enum → T1; resolver → T2; editor de-gate + options + copy → T3; Change link → T4; lib/credit + subline → T5; bar → T6; widget → T7; registry → T8; validator → T9; docstring → T10). Two flagged unknowns RESOLVED: (1) the chip's `cc_payments` come from `useDashboard().accountMonthEndForecast` joined by `account_id` (activeAccounts has none); (2) no accounts deep-link param exists → "Change" → `/accounts`. Assumptions: validator is `schemas/dashboard.py` not the router; exhaustive `Record<DashboardWidgetType,…>` forces keys in the defaults + test mirrors; migration 075 is MySQL-dialect-guarded (SQLite no-op, fixtures rebuild schema); band cuts use the raw uncapped util%, display rounds; High band uses the literal token `"var(--color-warning)"`; Change link only on the first (imminent) cc_payments entry.
