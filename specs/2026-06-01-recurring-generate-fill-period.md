# Recurring "Generate Due" fills the current billing period

**Date:** 2026-06-01
**Status:** Spec — ready for implementation plan
**Branch target:** new feature branch off `main`

## Problem

"Generate Due" (`POST /api/v1/recurring/generate`) only materializes recurring
instances whose `next_due_date <= today`. The owner expected it to bring up
**all** of the current billing period's recurring transactions as pending, so
the month's plan is visible at a glance. Today only manually-added transactions
show as pending for the period; recurring items either don't appear yet (future
due dates) or appear already-settled (`auto_settle=True`).

Separately observed but **explicitly out of scope** here: billing periods do not
close automatically, and there is no pre-close alert. The owner does not want to
couple generation to the close event for now.

## Goal

Make Generate Due fill the **current billing cycle window** (e.g. June 1–30):
materialize every active template's instances due anywhere in that window,
future-dated ones as `PENDING`. Keep catch-up of overdue prior-period items.
Keep it idempotent. Make `auto_settle` honest: future items are PENDING until
their date passes, then settle.

## Current behavior (baseline)

`generate_due_transactions(db, org_id)` — `backend/app/services/recurring_service.py:197`:

- Selects active `RecurringTransaction` rows where `next_due_date <= today`,
  `with_for_update()`.
- For each, `while r.next_due_date <= today`: creates one `Transaction`
  (`SETTLED` if `r.auto_settle` else `PENDING`; balance adjusted + `settled_date`
  set when settled), then `r.next_due_date = advance_date(...)`.
- One outer `db.commit()` at the end; per-instance `begin_nested()` savepoints.

Route: `backend/app/routers/recurring.py:63` returns `{"generated": count}`.
Frontend button + toast: `frontend/app/recurring/page.tsx:78,115`.

## Design

### 1. Cycle window helper (pure, no I/O)

Add a pure function that computes the current cycle window from the org's
`billing_cycle_day` and today — **not** from the `BillingPeriod` row.

```
current_cycle_window(cycle_day: int, today: date) -> tuple[date, date]
    # returns (start, end_inclusive)
    # start  = most recent occurrence of cycle_day on/before today
    # end    = (start + 1 month, snapped to cycle_day, clamped to month length)
    #          minus 1 day
```

Reuse the existing math: the start logic already lives in
`billing_service.get_current_period` (lines 49–56) and the snap-to-cycle + month
clamp already lives in `ensure_future_periods._snap_to_cycle` (lines 126–149).
Factor both into this shared helper and have `get_current_period` /
`ensure_future_periods` call it too (no behavior change for them).

**Why not the `BillingPeriod` row:**
- `get_current_period` *commits* and can *auto-create* a period row. Calling it
  inside generation (which holds `FOR UPDATE` locks and commits once at the end)
  would release locks / partial-commit mid-loop, and silently writing a
  `billing_periods` row as a side effect of "Generate" is surprising.
- A stale never-closed open period has a window in the past, so "fill the current
  period" would generate nothing for the real month. Pure cycle math from `today`
  is robust regardless of close discipline.

Generation reads `org.billing_cycle_day` (one cheap `SELECT`), computes the
window, and never touches `billing_periods`.

### 2. Generation loop changes

`generate_due_transactions(db, org_id)`:

1. `today = date.today()`.
2. Load `org.billing_cycle_day`; `period_start, period_end = current_cycle_window(cycle_day, today)`.
3. **Settle-on-due sweep (makes auto_settle honest).** Before generating, find
   existing `PENDING` transactions that originated from an `auto_settle` template
   and whose `date <= today`, and settle them:
   - `SELECT ... FOR UPDATE` transactions where `status == PENDING`,
     `recurring_id` is not null, `date <= today`, joined to templates with
     `auto_settle == True`, scoped to `org_id`.
   - For each: set `status = SETTLED`, `settled_date = date`, and apply the
     balance via `get_account_for_update` + `apply_balance` (same primitives the
     generation loop already uses).
   - Non-auto_settle pending items are never touched.
4. Lock due templates: active rows where `next_due_date <= period_end`
   (was `<= today`), `with_for_update()`, `populate_existing=True`.
5. For each template, `while r.next_due_date <= period_end`:
   - `due = r.next_due_date`
   - `tx_status = SETTLED if (r.auto_settle and due <= today) else PENDING`
   - Create `Transaction` dated `due`; `settled_date = due` and balance applied
     **only** when `SETTLED`.
   - `r.next_due_date = advance_date(due, r.frequency)`.
6. One `db.commit()` at the end (unchanged).

**Invariant (must hold):** after generation, every materialized template has
`next_due_date` strictly after `period_end`. This is what keeps re-runs
idempotent and keeps the forecast buckets from double-counting (see §5).

### 3. Catch-up of overdue (kept)

Templates whose `next_due_date` predates `period_start` are still caught up: the
loop dates each missed instance at its real past date; past instances from an
`auto_settle` template are `SETTLED` (since `due <= today`), others `PENDING`.
Nothing is dropped.

### 4. Route + response shape

`POST /api/v1/recurring/generate` returns a richer payload so the UI can give a
useful summary:

```json
{ "generated": 7, "settled": 3, "pending": 4, "period_end": "2026-06-30" }
```

`settled` counts both the sweep promotions and freshly-created settled rows;
`generated` counts newly-created transactions (sweep promotions are not "new"
rows — keep them separate or fold them in, but document which). Recommended:
`generated` = new rows created this run; `settled`/`pending` describe the new
rows; optionally add `auto_settled_now` for sweep count. Final shape decided in
the plan; the toast only needs new-row counts + `period_end`.

### 5. Forecast interaction (verify, do not change forecast)

Forecast (`forecast_service.py`) buckets settled by `settled_date`, pending by
`date`, and projects upcoming recurring by `next_due_date > today`. Because
generation always advances `next_due_date` past `period_end`, a given instance
is either materialized (counted in pending/settled) **or** still projected
(counted in recurring), never both — totals are preserved regardless of whether
generation's window and forecast's window align exactly. **No forecast code
changes.** Add a regression test asserting `forecast_net` is identical
immediately before and after a Generate within the same period.

### 6. Frontend

`frontend/app/recurring/page.tsx`:

- Rename the button **"Generate Due" → "Generate this period"**.
- Toast: `Generated 7 transaction(s) (3 settled, 4 pending) through Jun 30.`
  (format `period_end` for display; no em-dashes per house style).
- Update the page helper `<p>` to explain that generating fills the current
  billing cycle with this period's recurring transactions (future-dated ones
  appear as pending).
- The Stop / Delete `ConfirmModal` copy (lines 302, 311) already warns that
  pending future transactions will be removed — keep, but the implementer should
  confirm the wording still reads well now that a full period of pending rows can
  exist.

## Edge cases & safeguards

- **Sub-monthly frequencies** (weekly/biweekly) generate ~2–5 rows per period —
  bounded, fine.
- **Far-past `next_due_date`** (pathological catch-up) is a pre-existing
  unbounded-loop hazard, slightly amplified by extending the cutoff. Add a
  defensive iteration cap per template (e.g. 500) that logs a structured warning
  and stops rather than silently dropping; normal data never hits it.
- **`update_recurring` allows `next_due_date` to move backward** with no guard
  (`recurring_service.py:110`). With the wider cutoff this could re-emit a full
  period of duplicates. Add a lightweight existence guard in the loop: skip
  creating a `Transaction` when one already exists for `(recurring_id, due)`.
  (Cheap insurance; also protects the sweep/idempotency story.)
- **`SETTLED ⇒ settled_date not null`** invariant (migration 036 CHECK) is
  satisfied: settled rows always set `settled_date = due`.
- **billing_cycle_day near month end (29–31):** the helper clamps to month
  length (Feb → 28/29). Covered by a dedicated test.
- **No org / no billing_cycle_day:** default is 1 (`models/user.py:28`,
  non-nullable default). No special handling needed.

## Out of scope (deferred)

- Automatic generation (scheduler / on-login). Build window-first so a future
  trigger just supplies the window. Tracked in the credit-card billing backlog.
- Pre-close alert (~2 days before cycle close) and automatic period close.
- Reducing the recurring page to stop/delete-only.
- Coupling generation to the billing-period *close* event.
- Fixing the forecast stale-open-period window (note only).

## Tests

Backend service (`backend/tests/` — currently **zero** coverage for
`generate_due_transactions`, so this is greenfield):

1. Open cycle, monthly template due later in period → one PENDING dated at due;
   `next_due_date` advanced past `period_end`; balance unchanged.
2. `auto_settle`, due `<= today` → SETTLED, `settled_date == due`, balance
   applied once.
3. `auto_settle`, due `> today` (in period) → PENDING, `settled_date` null,
   balance not applied.
4. **Settle-on-due sweep:** a PENDING auto_settle row dated yesterday → after a
   later Generate it becomes SETTLED with balance applied; a PENDING
   *non-auto_settle* row dated yesterday is left PENDING.
5. Idempotency: Generate twice in the same period → second run creates 0 new
   rows, no duplicates, balance unchanged on the second run.
6. Overdue catch-up across the period boundary → all missed instances created
   once, correctly dated and statused.
7. Weekly template within a period → correct count of rows up to `period_end`;
   loop terminates.
8. Boundary precision: due `== period_end` included; due `== period_end + 1`
   excluded.
9. `billing_cycle_day = 31` month-length clamp → window end correct across a
   short month.
10. Forecast parity: `forecast_net` identical immediately before and after a
    Generate within the same period.
11. `(recurring_id, due)` existence guard: a template whose `next_due_date` was
    edited backward does not produce duplicate rows on the next Generate.

Router: `POST /api/v1/recurring/generate` returns the expanded payload and is
org-scoped.

Frontend: button label, success-toast formatting (counts + `period_end`), and
that a successful generate triggers a reload.

## Files of record

- `backend/app/services/recurring_service.py:197` — generation loop + new sweep.
- `backend/app/services/billing_service.py:22,126` — extract shared cycle-window
  helper; `get_current_period` / `ensure_future_periods` call it.
- `backend/app/routers/recurring.py:63` — response shape.
- `backend/app/services/forecast_service.py` — no change; parity test only.
- `frontend/app/recurring/page.tsx:78,115,126,302,311` — button, toast, copy.
- `backend/app/services/date_utils.py:10` — `advance_date` (unchanged, referenced).
