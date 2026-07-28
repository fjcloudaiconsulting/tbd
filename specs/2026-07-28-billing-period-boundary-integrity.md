---
name: Billing Period Boundary Integrity
description: TBD-239 — stop three of the four write paths that produce gaps and overlaps in the billing period roster
type: design
jira: TBD-239
status: revision 3 — §4 split to TBD-241 on unanimous re-sign-off advice; retained sections signed off by both reviewers
---

# Billing Period Boundary Integrity (TBD-239)

## Amendment to a merged spec

`specs/2026-07-27-billing-period-truth-and-safety.md` finding 3 states that `ensure_future_periods`
will fight a roster editor *"once an admin moves a stub's boundary off the cycle-day grid"*, framing it
as a TBD-235 problem. **That is wrong.** `PUT /billing-cycle` is already an off-grid mover and moves the
exact row `ensure_future_periods` uses as its base. The condition is live today with no editor. Read
finding 3 as amended by this document. Finding 5 (the scheduler grid-guard is a regression) is unaffected.

## The defect

Four write paths mutate billing period boundaries. **All check only exact `start_date` equality; none
checks containment.** Result: gaps (transactions belonging to no period, invisible to
`budget_service.list_budgets` and `forecast_service`, while still counting toward the account balance)
and overlaps (days counted twice).

| # | Path | Defect | Ticket |
|---|---|---|---|
| 1 | `PUT /billing-cycle` (`backend/app/routers/settings.py:274-347`) | Re-roots the open period's `start_date` in place, never touches the predecessor's `end_date` | **TBD-239** |
| 2 | `ensure_future_periods` (`backend/app/services/billing_service.py:166-174`) | Skips a stub only on exact `start_date` match, so an intersecting window is created anyway | **TBD-239** |
| 3 | `POST /billing-period` (`settings.py:402-412`) | Exact-start pre-flight only; can insert a fully contained period | **TBD-239** |
| 4 | `close_period` (`billing_service.py:322-382`) | No upper bound on `close_date`; strands or overlaps intervening stubs | **TBD-241** |

## Why producer 4 is not here

Three designs were attempted inside this spec and all three failed sign-off: a 409 that permanently
wedged both close paths; an absorb that DELETEd stubs and violated the NOT NULL `ForecastPlan` FK with
no `ondelete` (`models/forecast_plan.py:43`) while stranding budgets; and a chain-close that is
promising but still needs `old_start` capture ordering, multi-row selection rules, a no-unremediable-409
resolution, and an idempotent `IntegrityError` retry path.

Both re-sign-off reviewers independently concluded that §§1-3 and §5 stand alone and are buildable
today, and that only producer 4 is blocked. It is now **TBD-241**, which carries all three rejected
designs and every open blocker so the work is not re-derived.

**Consequence, stated honestly:** after this ticket, `close_period` can still leave an overlap when a
close lands inside a stub's window. That is **pre-existing behaviour, not introduced here** — and it is
unreachable from the UI, which sends no `close_date` (`frontend/app/settings/organization/page.tsx:311`).
The three producers fixed here are the ones that fire from ordinary use.

## Ruling record: producer 1 is a delete, not a guard

The rejected design is the one a reader will re-propose, so the reasoning is recorded.

**Rejected: a local containment guard plus moving the predecessor's `end_date`.** *When it rejects*:
after any manual close the predecessor is short, because `close_period` defaults to closing yesterday
(`billing_service.py:328-329`) and the org page posts no `close_date` (`page.tsx:311`); with predecessor
`[Jul 25, Jul 26]` and open `[Jul 27, …)`, 25 of the 28 legal cycle days return 409 with no in-app
remedy. *When it accepts, which is worse*: with the ordinary predecessor `[Jun 25, Jul 26]`, setting
`P.end_date = Jul 4` silently moves **22 days of already-closed, already-reported settled spend** out of
a closed period, rewriting last month's totals with no confirmation.

**Accepted: delete it.** `billing_cycle_day` is documented as a scheduling hint
(`billing_service.py:6-8`). The settling principle: a **forward** re-anchor is exactly expressible as a
close (close at `new_start - 1`, open at `new_start`) — contiguous, auditable, previewable. A
**backward** re-anchor is not expressible as anything honest. The capability returns in TBD-235 as an
explicit confirmed action.

**The delete is necessary but not sufficient.** After it, an admin changing cycle day writes nothing,
but the next Budgets or Forecasts mount runs `ensure_future_periods` with `base = current.start_date`
and the new `cycle_day`, matches no exact `start_date`, and builds a second grid. Producer 2 is the
load-bearing half.

## Scope

Effort **M**, one PR.

### 1. `PUT /billing-cycle` — delete the re-anchor

Delete **`settings.py:274-347`**. Note 274-345 would leave an orphaned `)` at `:346` and `raise` at
`:347` → SyntaxError.

The handler becomes `_require_admin`, load org, **retain the existing `get_current_period` call at
`settings.py:230`**, set `org.billing_cycle_day`, commit, audit.

The `get_current_period` call is retained because the audit payload needs `period_id` and
`open_period_start`. State honestly rather than claim otherwise: `get_current_period` auto-creates and
**commits** when no open row exists (`billing_service.py:88-112`), committing the pending
`billing_cycle_day` with it. Benign — the new row lands on the new grid — and pre-existing.

Also update:

- Handler docstring at **`settings.py:200-209`**, which describes the removed behaviour verbatim and
  names `reanchor_period_dependents` at `:205`.
- `_audit` closure (`def` at **`:236`**, docstring **`:237-247`**) and the actor-snapshot comment at
  **`:212-213`** — both justify themselves by a rollback path that no longer exists, since every
  `outcome="failure"` call site is inside the deleted block.
- Audit payload keys at **`:249-250`** and the success call `await _audit("success",
  budgets_reanchored=reanchored)` at **`:351`**.
- Remove dead locals `new_start` and `reanchored`. **`old_start` survives**, renamed to
  `open_period_start` for the audit payload.

Verified no dead imports: `IntegrityError` survives at `:135, :422`, `BillingPeriod` at `:403-414`,
`ConflictError` at `:409, :425`, `datetime` at `:463, :546`. `BillingCycleUpdate`
(`schemas/settings.py:11-12`) has only the one field.

Audit detail becomes `{old_day, new_day, period_id, open_period_start, applies_from: "next_period"}`.

**Behaviour change.** A cycle-day change now takes effect at the next close. `BillingCloseJob` migrates
the org onto the new grid contiguously on the first close after the change — verified both directions
(25→5 and 5→25). Without TBD-241 an org that has lapsed several cycles may need more than one close to
converge; that is the pre-existing behaviour, unchanged by this ticket.

For orgs with `automate_billing_close` OFF the cycle day becomes advisory until their next manual close.
Pre-existing divergence: any org that has manually closed is already off-grid. The capability genuinely
lost is *"fix my current period's start"*, which has no in-app path until TBD-235.

**`reanchor_period_dependents` loses its only production caller** (`settings.py:333`). Keep it and its
seven service tests — TBD-235 and TBD-241 are its named consumers. Add a module note that it has no
production caller until then, so nobody prunes it as dead code and nobody assumes it is covered
end to end.

### 2. `ensure_future_periods` — intersection skip

Replace the exact-match check at `billing_service.py:166-174` with an intersection test **in SQL, over
raw `end_date`**, org-scoped:

> Skip candidate `[s, e]` when a same-org row satisfies `start_date <= e AND end_date >= s`.

**Correction carried from revision 1.** Revision 1 called `AND end_date IS NOT NULL` *"the single most
important line to review"* and claimed omitting it stops stub creation permanently. That is **wrong**:
in SQL three-valued logic `end_date >= s` already does not match a NULL `end_date`, so the clause is
redundant here. Keep it only as documentation of intent, with a comment explaining *why* NULL-end rows
are excluded — candidates are `_snap_to_cycle(base + relativedelta(months=i), cycle_day)` for `i >= 1`
(`billing_service.py:164`) and `cycle_day ∈ [1, 28]` (`schemas/settings.py:12`), so a candidate always
falls in a strictly later calendar month than `base` and a backward overlap with the open row is
impossible. **Do not** use `effective_end`, `COALESCE(end_date, '9999-12-31')`, or hydrate-and-filter in
Python; those are the only ways to reach the failure revision 1 described.

Test 4 is the real guard.

**Known hole to note in the comment:** the exclusion is blind to a *second* open row.
`get_current_period` warns about multiples (`billing_service.py:76-84`) and `POST /billing-period` can
insert an open row at an arbitrary start (`seed.py:250-251` does exactly that).
`uq_billing_period_org_start` backstops only exact-start collisions.

On intersection: **skip silently, create nothing, emit `structlog` warning
`billing.stub.skipped_overlap`. Never 409** — mount-fired from `frontend/app/budgets/page.tsx:97` and
`frontend/app/forecast-plans/ForecastPlansClient.tsx:290`. Keep the loop running through all `count`
iterations; keep the existing commit and `IntegrityError` rollback.

The check is a SQL `SELECT` and autoflush is on, so pending stubs are already visible within a call — no
in-memory list needed. (Revision 2 claimed a flush-time `IntegrityError` could "now" fire from inside
the check; that was already true of the existing `db.scalar` at `:167`. Not a new hazard.)

**Fix the docstring at `billing_service.py:151-155`** — it claims "Always anchored to today" while line
**161** uses `base = current.start_date`. Correct the docstring only; changing the anchor is TBD-235
blocker 1.

### 3. `POST /billing-period` — containment

Add an intersection check **after** the existing exact-start check at `settings.py:402-412`, so a seed
re-run still hits `billing_period_exists` first. New containment conflict → 409 `billing_period_overlap`.

**NULL semantics, both directions, pinned:**

- *Existing rows*: compare against raw `end_date`; rows with `end_date IS NULL` are ignored (the open
  row's true extent is unknowable at insert time).
- *The candidate*: `BillingPeriodCreate.end_date` is optional (`schemas/settings.py:30`) and
  `seed.py:250-251` posts exactly that shape. **A candidate with no `end_date` is checked on its
  `start_date` alone** — reject only if that single date falls inside an existing row's
  `[start_date, end_date]`. Do not treat an open candidate as unbounded, or seeding an open period after
  any closed period would always conflict.

Note this leaves an open candidate able to create a second open row, which `get_current_period` warns
about. Pre-existing; called out here because §3 makes it reachable by design.

**Forces a `seed.py` change in the same PR.** `billing_period_outcome` (`seed.py:30-54`) absorbs only
`billing_period_exists` and raises on any other non-2xx. Absorb both codes or `./pfv seed` fails on a
cross-day re-run.

### 4. Copy

`frontend/app/settings/organization/page.tsx`:

- **Preview (~`:603`)** → `Saving changes the day your billing periods start on, from your next period onward. Saving on its own does not re-date the period you are in now. If automatic billing close is on, that period may close shortly so the new day can take effect.`
  A shorter *"the period you are in now keeps its current dates"* was drafted and rejected: it is false
  within fifteen minutes. `BillingCloseJob.is_due` compares the open period's start against
  `current_cycle_window(NEW cycle_day, today)[0]`, so a forward move past a day that already went by
  this month makes the next 900s tick close the current period with an end date in the past.
- **Success message (~`:282`)** → `Billing cycle saved. Day N applies from your next billing period. Closing a period by hand still opens the next one the day after the close date.`
  *"Your next period will start on day N"* was drafted and rejected: it is true only with
  `automate_billing_close` ON. With it off, the only in-app close path (`:311`) posts no `close_date`,
  so `close_period` defaults to yesterday and the next period starts **today**.
- **Drop the `GET /billing-period` refetch after the PUT (~`:278-281`)**. The PUT writes one org column
  and leaves the open period alone, so there is nothing to re-read; a failing refetch landed in the
  `catch` and reported a save failure for a save that had already succeeded.
- **`currentPeriodEndDisplay` (`:112-113`)** must read `savedCycleDay`, not the live `billingCycleDay`
  input. Reading the live value made the `Current:` line jump to a different end date on the first
  keystroke while the preview below it simultaneously said the current period is not re-dated. (The
  deeper problem — `projectedPeriodEnd` being a month out for off-grid orgs — needs a backend-supplied
  projected end and is TBD-235.)
- **Delete `:570-571`** — *"Saving does not close anything: PUT /billing-cycle re-roots the open period's start in place and drags its budgets along."* Describes deleted code. Keep the rest of the `DELIBERATELY DATELESS` block (`:573-585`); its server-vs-browser date reasoning stands.
- **Stale comment (~`:494-505`)** claiming a branch *"becomes reachable in TBD-234"* — it does not; TBD-234 is a separate read-only route that never feeds `currentPeriod`. Rewrite.
- **The `if (!currentPeriod?.start_date) return "";` guard at `:602`, whose rationale comment is at `:597-601`** — justified by *"the sentence promises a move of 'the current period'"*. **Keep the guard** (the new sentence still refers to "the period you are in now") and update the comment. Decide explicitly so two engineers do not differ.
- **`~:299`** cites `billing_service.py:200-201` for the close-yesterday default; the actual location is **`:328-329`**. Fix while editing that block.
- **Field hint (`:561-562`)** needs no change; it already describes the new behaviour.

`frontend/lib/formErrors.ts`:

- `mapBillingCycleError`'s 409 branch (`:137-143`) becomes unreachable for this endpoint. **Leave it**,
  comment why; TBD-235 and TBD-241 need it.
- `mapBillingPeriodCloseError` (`:156-176`) is **not touched by this ticket** — its 400 and 409 cases
  move to TBD-241 along with producer 4.

**Verbatim `detail` string** (409 details reach users through these mappers, so they are UX copy —
complete sentences, concrete dates, no em-dashes):

- `billing_period_overlap` on `POST /billing-period`:
  `A billing period already covers {start} to {end}. Choose dates outside that range.`

## Error contract

Verified in `backend/app/main.py`: domain `ValidationError` → **400** `{detail}` (`:376-378`);
`ConflictError` → **409** `{detail, code}`, `code` keyword-only (`:381-389`, `exceptions.py:20-30`);
**422 belongs to the framework handler** (`:463`) — do not hand-roll.

New code: `billing_period_overlap` (producer 3 only). `billing_period_exists` keeps its meaning and its
first position.

**Concurrency honesty.** Producers 2 and 3 are SELECT-then-write, therefore TOCTOU under real MySQL, and
unlike the exact-start case **no unique constraint backstops an intersecting insert**. The containment
invariant is best-effort under concurrency; a production detector is deferred with the anomaly kernel to
TBD-234, whose roster is its only consumer.

## Testing

Extend `backend/tests/routers/test_settings_billing_periods.py`. Its harness comment is load-bearing:
the bare `FastAPI()` re-registers the domain exception handlers, and the `ConflictError` handler must
keep `code` in the body or every conflict-code assertion is vacuous. The fixture sets
`PRAGMA foreign_keys=ON` at `:63`; `tests/services/test_billing_service.py:23-36` does **not**, so any
FK-sensitive assertion belongs in the router file.

**Delete or rewrite** the router-level re-anchor tests at `:417, :449, :486, :527, :568` — **and
`:389`** (`test_update_billing_cycle_writes_audit_row`), which asserts `detail["old_start"]`,
`detail["new_start"]` and `detail["budgets_reanchored"]` at `:411-413`, all dropped by §1. `:644` is
**not** affected and stays.

**Keep all seven** direct `reanchor_period_dependents` service tests at `:658, :681, :701, :726, :743,
:770, :794`, plus the `_seed_other_org` helper at `:819`.

Cases:

1. Cycle-day change forward and backward mutates **zero** `billing_periods` rows — **only for an org
   that already has an open period**. For an org with none, the retained `get_current_period` call takes
   its auto-create branch and commits one row, carrying the pending `billing_cycle_day` with it. That
   shape needs its own case, pinning the created row's start on the NEW grid; the handler docstring must
   not claim "touches no period row" unconditionally.
2. Audit row carries `applies_from: "next_period"`, `open_period_start`, and no `budgets_reanchored`.
3. `./pfv seed`'s shape produces **no gap**. The `today.day < 25` branch is the one that currently
   orphans day 24, so it **needs a frozen date** — today's real date is in the `>= 25` branch, which is
   a no-op, so an unfrozen test proves nothing. **Do not patch `seed.py`'s dates**; the delete fixes it
   for free and patching would mask the regression.
4. **`ensure_future_periods` still creates stubs on a healthy org.** The real guard for §2 — without it,
   a wrong intersection predicate silently disables stub creation and `test_budgets_next_period.py`
   becomes the thing that catches it, confusingly.
5. Dual grid killed: after a cycle-day change, `ensure_future_periods` creates no intersecting stub.
6. Stale open period (start four months back) → stubs build without intersecting the open window.
7. `POST /billing-period` rejects an overlapping window → 409 `billing_period_overlap`; a duplicate start
   still returns `billing_period_exists` first; a candidate with `end_date IS NULL` is checked on its
   start date alone; an existing row with `end_date IS NULL` is compared on its **start** only — waved
   through when that start is outside the candidate window, rejected when it falls inside it (its end is
   unknowable, its start is not).
7b. **The `ensure_future_periods` skip keeps an exact-start arm alongside the intersection arm.**
   `close_period` REVIVES a stub at its `new_start` rather than inserting, so a closed intersecting row
   can become an open row at a start still under consideration; the intersection arm alone then misses
   it and `db.add` duplicates. The resulting `IntegrityError` fires from autoflush in the NEXT
   iteration's `db.scalar`, outside the `try/except` around `db.commit()`, and escapes as a 500. Force
   the race by monkeypatching `get_current_period` to return the pre-revive open row.
7c. The `billing.stub.skipped_overlap` warning is asserted (`structlog.testing.capture_logs`); the skip
   is otherwise invisible, so the event name and payload keys are the only production signal.
8. `seed.billing_period_outcome` absorbs `billing_period_overlap` as well as `billing_period_exists`.
9. Frontend: the four assertions on the old preview string at
   `organization-billing-period-polish.test.tsx:219, 236, 253, 259` updated; the em-dash guards at
   `:240` and `:289` stay green.

An **off-grid fixture** must appear in cases 1, 3 and 5.

**SQLite caveats.** `sqlite+aiosqlite:///:memory:` with `StaticPool` — one connection, no real
concurrency, so TOCTOU paths must be forced by monkeypatch as the existing suite does. Do not assert on
`rowcount` deltas.

## Out of scope

| Work | Ticket |
|---|---|
| `close_period` bound + intervening-stub handling (producer 4) | **TBD-241** |
| Adopt `effective_end` in `_compute_spent` / forecast actuals / account balance forecast | **TBD-240** (blocks TBD-234) |
| Anomaly kernel `find_period_anomalies`, sweep script, roster page | TBD-234 |
| Boundary editor; re-anchor as an explicit confirmed action; `ensure_future_periods` anchor change | TBD-235 |
| `closed_at` decoupling | TBD-233 |
| Scheduler pinning | TBD-236 |

`get_current_period`'s auto-create (`billing_service.py:88-101`) INSERTs from a read path and commits.
**Documented, not fixed**: unreachable for any org that already has an open row.

## Risk

Low. **No row is deleted, no schema change, no migration.** `GET /billing-periods` keeps its exact
shape, and no read path used by dashboard, transactions, budgets or forecast is touched.

1. **Behaviour change on `PUT /billing-cycle`** — a cycle-day change no longer moves the current period.
   Intended, covered by §4 copy. Orgs with automation ON converge at the next close; orgs with it OFF
   have no in-app re-anchor until TBD-235.
2. **The intersection predicate in §2.** If built with `effective_end` semantics instead of raw
   `end_date`, stub creation stops for every org and next-period budgets break silently. Case 4 is the
   guard.
