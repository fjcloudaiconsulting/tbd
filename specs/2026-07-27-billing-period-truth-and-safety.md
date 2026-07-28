---
name: Billing Period Truth + Safety Pass
description: TBD-232 — slice 1 of the TBD-213 split; make the billing period surface stop lying, stop 500ing, and stop re-anchoring budgets in two places
type: design
jira: TBD-232
parent: TBD-213
status: signed off by architect review 2026-07-27 (revision 2)
---

# Billing Period Truth + Safety Pass (TBD-232)

## Why this spec exists

TBD-213 ("Billing period roster UI with per-row editable dates") was tagged **M** and flagged
*VERIFY FIRST — medium confidence unbuilt*. Verification confirmed it is genuinely unbuilt, but a
four-architect design + validation round found the ticket as written is **1,200-2,400 LoC**, requires
a schema change nobody anticipated, and that the obvious implementation would **reject writes for
100% of active orgs**.

This spec covers **slice 1 only**: the subset that is safe, self-contained, and de-risks everything
after it. No new UI. No behaviour change users can trip over — the only user-visible change is copy
that currently makes false claims.

The remaining slices are TBD-233 (`closed_at`), TBD-234 (read-only roster), TBD-235 (boundary
editing), TBD-236 (scheduler pinning), TBD-237 (forecast spillover), TBD-238 (`Budget` FK).

### What the architects found (the reason for the split)

Recorded here because these findings must not be re-discovered later.

**1. The open period is not the last row.** `ensure_future_periods`
(`backend/app/services/billing_service.py:161-176`) creates future stub periods with *both* dates set
and a `start_date` *newer* than the open period. It fires on page mount from
`frontend/app/budgets/page.tsx:97` and `frontend/app/forecast-plans/ForecastPlansClient.tsx:290` (both
admin-gated, so precisely: every org whose *admin* has opened Budgets or Forecasts). A live org's roster
is `[…closed…, OPEN(end=NULL), stub, stub, stub]` — the NULL-ended row sits in the **middle**.

Any design resting on "exactly one `end_date IS NULL` per org and it must be the newest" is false for
those orgs. It also means the boundary the user most wants to move — *period ends May 24, salary lands
May 25*, literally `specs/user-billing-flow.md:8` — is the open→stub seam, which cannot be expressed
while `end_date IS NULL` doubles as the "is open" flag. **Decoupling those two meanings (TBD-233) is a
prerequisite for the editing slice, not a follow-up.**

**2. Moving a transaction backward across a close boundary is blocked.**
`backend/app/services/transaction_service.py:614-615` raises when `settled_date < date`. The spec's actual
use case (`specs/user-billing-flow.md:11` — card B charges attributed to the previous, already-closed
period) is a *backward* move, and is structurally impossible without falsifying `date`. Forward moves are
free. TBD-213's DoD item 3 is therefore not "already satisfied", as an early reading concluded.

**3. `ensure_future_periods` will fight a roster editor forever.** It skips only on exact `start_date`
match (`billing_service.py:165-172`). Once an admin moves a stub's boundary off the cycle-day grid, the
next Budgets or Forecasts page mount re-creates the grid-aligned stub, producing a permanent,
self-regenerating overlap.

**4. There are four independent definitions of "the period window".** The `BillingPeriod` row;
`current_cycle_window()` cycle-day math (`backend/app/services/scheduler/jobs/billing_close.py:22`,
`billing_reminder.py:23`, `recurring_service.py:266`); the `start + 1 month - 1 day` fallback
(`forecast_service.py:61`, `account_balance_forecast_service.py:67`, `forecast_plan_service.py:410`);
and client-side `projectedPeriodEnd()` (`frontend/lib/format.ts:49`). Hand-editing the row desynchronizes
the other three.

**5. A cycle-day-grid guard on the scheduler would be a regression.** `BillingCloseJob.is_due`
(`scheduler/jobs/billing_close.py:21-24`) re-anchors off-grid periods. Gating it on grid-equality would
**permanently disable auto-close** for any org that ever closed manually — which is the normal workflow,
since `close_period` defaults to "close yesterday" and salary day varies. `automate_billing_close`
defaults on. Protecting a hand-set period needs explicit state, not date arithmetic. **Correction from
review:** no test in `backend/tests/services/test_scheduler_job_billing_close.py` seeds an off-grid
period (all four cases use on-grid starts at `:37,46,56,78`), so a naive grid-equality guard would
**pass the existing suite**. TBD-236 must add off-grid coverage.

None of 1-5 are in scope here. They are recorded as the design constraints for the follow-up slices.

## Scope of this slice

### 1. Fix three false copy strings

All in `frontend/app/settings/organization/page.tsx`.

| Line | Current string | Status |
|---|---|---|
| 491 | "Closed. Transactions in this range are locked from period rollover." | False, **and unreachable** |
| 298-300 | "…A new period will open automatically. Closing a period cannot be undone." | Misleading, not strictly false |
| 567 | "Saving will close the current period on {projected} and open a new one on day {day}." | False |

**Line 491 — false but dead code.** Nothing in the backend locks a closed period (`grep end_date
backend/app/services/transaction_service.py` returns zero hits). But the branch never renders:
`currentPeriod` is only ever populated from `GET /billing-period` (`page.tsx:186-188`, `:279`, `:308`),
which returns `billing_service.get_current_period`, which by construction only returns rows with
`end_date IS NULL` (`billing_service.py:66-71`). Fix the string anyway — it becomes reachable in
TBD-234 — but **do not** add a frontend test for it, because asserting it requires a fixture the real
API cannot produce. Note this in the code comment.

Replacement: `"Closed. This period's window is fixed at these dates. Transactions are still counted by
the date they settled, so editing a transaction can move it in or out."`

**Lines 298-300 — replace the whole two-line message, not one line.** The current text is a
concatenation across `:298-300`. The claim "cannot be undone" is *not* false today: there is no un-close
endpoint in `routers/settings.py` and no UI, so it is reversible only by direct SQL. Deleting a true
caution two slices before reversibility ships would be a small regression. Keep a caution, make the
mechanics accurate. The frontend calls close with no `close_date` (`page.tsx:306`), so `close_period`
defaults to **yesterday** (`billing_service.py:200-201`) and the new period opens **today** — not
"tomorrow".

Replacement: `` `Close the current billing period starting ${currentPeriod?.start_date}? This sets its
end date to yesterday and opens a new period starting today. There is no way to reopen a period from
the app yet, so close only when the period is done.` ``

**Line 567 — false, and the concrete date must survive.** `PUT /billing-cycle` (`settings.py:190-226`)
closes nothing and creates nothing; it re-roots the open period's `start_date` **in place** and migrates
budgets. The existing preview's value is the concrete date, so the replacement keeps one. The frontend
has no helper for the backend's anchor math, so mirror `settings.py:209-214` in a small local helper:
`new_start` = day `N` of this month if `today.day >= N`, else day `N` of last month.

Replacement: `` `Saving will move the current period's start to ${newStart} and move its budgets with
it.` ``

House rule: no em-dashes in user-facing copy. All three replacements verified em-dash-free.

**Deliberately NOT doing:** implementing locking to make the `:491` copy true. It contradicts the
product's own stated workflow (`specs/user-billing-flow.md:11`) and would require a cross-cutting write
guard across transaction create/update, import, recurring generation, reconciliation and CC statement
paths, each needing an override.

### 2. Add the two missing audit events

CLAUDE.md requires sensitive admin / org actions to write to `audit_events`. Two period write paths
have none.

- **`POST /billing-period/close`** (`settings.py:299-311`) — writes no audit row, while the scheduler
  path for the same operation does (`scheduler/jobs/billing_close.py:35`). Add `org.billing_period.closed`.
- **`PUT /billing-cycle`** (`settings.py:190-226`) — mutates a period boundary *and* budget rows with
  no audit trail. Add `org.billing_cycle_day.updated` with detail
  `{old_day, new_day, period_id, old_start, new_start, budgets_reanchored}`.

**Naming decision.** Both `billing.period.closed` and `org.billing_period.closed` fit the dominant
`<domain>.<object>.<verb-past>` convention. Choose the `org.` prefix to match this router's existing
sibling `org.config.allow_manual_balance_adjustment.set` (`settings.py:390`) and the `target_org_id`
shape of the audit row. Nothing enumerates event types (no DB enum, no frontend registry — `/admin/audit`
filters `event_type` as free text), so this is a one-time choice with no migration cost.

**Note for the operator:** a manual close will audit as `org.billing_period.closed` while the scheduler's
close audits as `scheduler.billing_close.success` (`services/scheduler/audit.py:22`). "Show me all period
closes" is therefore a two-name query. This is consistent with the `scheduler.*` namespace convention and
is a deliberate decision, not an oversight.

**Obtaining the close detail keys.** `close_period` commits internally (`billing_service.py:228`) and
returns only the **new** period, so the router cannot see `closed_period_id`, and `close_date` arrives as
`None` on the default path. Resolution: the router calls `billing_service.get_current_period` **before**
`close_period` to snapshot `{id, start_date}` of the period about to close, and derives the resolved
close date as `new_period.start_date - 1 day`. Do **not** re-implement the "yesterday" default in the
router; it would drift from the service.

Detail: `{closed_period_id, closed_period_start, close_date, new_period_id, new_period_start}`.

**Pattern.** Both follow `settings.py:343-402`: `_require_admin`; snapshot actor identity
(`current_user.id` / `.email` / `.org_id`) **before any `await` on `db`** so a rollback cannot expire the
object; resolve client IP via `get_client_ip(request)` (never `request.client.host` — AST-enforced by
`backend/tests/test_no_raw_request_client.py`); fire
`audit_service.record_audit_event(session_factory, …)` on its own session **after** the business commit.

Both endpoints must gain `request: Request` and
`session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory)` parameters; neither
has them today. All required imports already exist in `settings.py` (`Request` line 4,
`async_sessionmaker` line 7, `get_session_factory` line 10, `get_client_ip` line 14, `audit_service`
line 22).

Write `outcome="failure"` rows on rejection, matching the house pattern at `admin_users.py:174-190` and
`org_data.py:120`.

### 3. Harden `POST /billing-period`

`settings.py:258-276` declares `start_date: datetime.date = None` as an **unvalidated query parameter**
in front of a NOT NULL column. FastAPI marks it not-required because a default was supplied, and neither
FastAPI nor Pydantic v2 validates defaults (`validate_default` is off), so `None` reaches the handler and
dies at commit. `main.py` registers no `IntegrityError` handler, so it surfaces as an unhandled **500**.

Replace with a Pydantic request body:

```python
class BillingPeriodCreate(BaseModel):
    start_date: datetime.date
    end_date: datetime.date | None = None

    @model_validator(mode="after")
    def _check_order(self): ...
```

**Status codes — decided, because the first draft contradicted itself.** The `end_date < start_date`
check is a **schema `model_validator`**, so it raises `RequestValidationError` → **422**, matching house
style for this exact shape (`schemas/announcement.py:48`, `schemas/org_ai_caps.py:26`,
`schemas/rate_limit_override.py:116`). A missing `start_date` is likewise **422**. Do not add a router
level `ValidationError` for ordering; one endpoint returning both 400 and 422 for shape errors would be
incoherent.

Keep the endpoint (do not delete it), keep it admin-only, and **keep `status_code=200`** — `seed.py:217`
branches on `r.status_code == 200`, so adopting 201 would silently break it.

Also add a pre-flight duplicate check on `(org_id, start_date)` raising `ConflictError` with code
`billing_period_exists`, so the hardened endpoint cannot 500 on `uq_billing_period_org_start` either.

**Blocking coupling — and the failure is silent.** `backend/seed.py:215` and `:223` call this endpoint
with `params={…}` (query params); they are the **only** callers anywhere (verified across `.py`, `.ts`,
`.tsx`, `.md`, `.sh`, `.json`). Switching to a body makes both 422. Worse: `seed.py:217` guards the first
call with `if r.status_code == 200:` and prints nothing on failure, `seed.py:223` checks nothing at all,
and `seed` appears in no `.github/workflows/` file. A missed update yields a demo org with **zero billing
periods** and a cheerful "Seed complete!". Therefore:

- Update both calls to `json={…}`.
- Add `r.raise_for_status()` to both so the next drift is loud.

### 4. One budget re-anchor code path

`PUT /billing-cycle` currently re-anchors budgets with a bare inline statement (`settings.py:218-223`),
guarded by `if old_start != new_start:`.

Defects:

1. It writes `period_start` but not `period_end`.
2. If a budget already exists for that category at the destination start, it trips
   `uq_budget_org_cat_period` → **uncaught IntegrityError → 500**.
3. It is about to be the second of two re-anchor implementations once TBD-235 lands.

Extract into `billing_service`:

```python
async def reanchor_period_dependents(
    db, *, org_id: int,
    old_start: datetime.date, new_start: datetime.date,
    new_end: datetime.date | None,
) -> int:
    """Move budgets anchored to `old_start` onto `new_start` / `new_end`.

    Returns the number of budget rows re-anchored. Raises ConflictError
    (code `budget_period_conflict`) when a budget already exists for the
    same category at `new_start`.

    A boundary move in TBD-235 calls this TWICE: once for the previous
    period (old_start == new_start, only `new_end` changes) and once for
    the next period (old_start != new_start).
    """
```

**The identity case is the single most important rule in this spec.** When `old_start == new_start` the
rows being moved *are* the rows at `(category_id, new_start)`, so a naive pre-flight finds every budget
conflicting with itself and raises. This is not hypothetical:

- `close_period` defaults to "close yesterday", so an org that ever closes manually has an open period
  starting on an arbitrary day while `billing_cycle_day` says something else. An admin re-saving the same
  cycle day hits `new_start == old_start` and would get a **permanent 409 on a previously working no-op**.
- `seed.py:227` issues `PUT /billing-cycle {billing_cycle_day: 25}` immediately after creating a period
  that starts on the 25th. Whenever `date.today().day >= 25`, that is the identity case — so `./pfv seed`
  would fail roughly 7 days a month, silently, since `seed.py:227` does not check the response.

Required rules, stated so two engineers cannot build them differently:

- `reanchor_period_dependents` **early-returns 0** when `old_start == new_start` **and** `new_end` is
  unchanged. It must not run either pre-flight in that case.
- The pre-flight for `uq_budget_org_cat_period` **excludes rows whose `period_start == old_start`**
  (the rows being moved).
- The period-existence pre-flight **excludes `current_period.id`**.
- `PUT /billing-cycle` **keeps** its `if old_start != new_start:` guard around the period reassignment.
  The guard is not what is being deleted; only the inline `UPDATE Budget` statement is.

**`new_end` at the `PUT /billing-cycle` call site is `current_period.end_date`, which is `None`.** State
this explicitly: the period being re-anchored is the *open* period, whose `end_date` is `None` by
construction. An implementer who computes a projected end and writes a non-null value would silently
corrupt open-period budget snapshots for every org whose admin touches the cycle day.

**Honest rationale for writing `period_end`** (the first draft's justification was wrong and would not
survive review): no frontend file renders `Budget.period_end`; `list_budgets` computes spend from the
**live** period row (`budget_service.py:112`), not the snapshot; and for this slice's only caller the
value is `None` both before and after, so the write is a no-op *here*. The genuine case is narrow: a
future stub carrying an `end_date` gets budgets copied into it (`budget_service.py:374`, `:433`), then
`close_period` revives that stub as the open period (`billing_service.py:225-228`), leaving a stale
non-null snapshot that `update_budget` reads for non-current periods (`budget_service.py:177`). Keep the
change as correct hygiene and as groundwork for TBD-235; do not claim a symptom that does not occur.

**Keep the `IntegrityError` backstop alongside the pre-flight.** A pre-flight SELECT alone is TOCTOU, and
under real MySQL two admins — or an admin and `BillingCloseJob`, which runs every 900s with
`automate_billing_close` on by default — can race between the SELECT and the UPDATE and 500 anyway,
defeating the entire point of this slice. The house pattern in this exact area is *both*: pre-flight for
the good message, `except IntegrityError: rollback; raise ConflictError(...)` as the backstop. See
`billing_service.py:214-219` + `:229-251`, `:165-172` + `:186-189`, `settings.py:131-144`,
`budget_service.py:135-142`.

**Same transaction.** The re-anchor must commit with the period write. `Budget.period_start` is the sole
join key (`budget_service.py:100-101`); a crash between the two writes orphans every budget for that
period and `list_budgets` silently returns `[]` (it swallows the `ValidationError` at
`budget_service.py:90-93`). This is achievable: `update_billing_cycle` calls `get_current_period` at
`settings.py:204`, before the period write at `:215`, and the only commit is at `:225`.
`get_current_period`'s internal commit (`billing_service.py:96`) fires only on auto-create, which cannot
happen once a period exists. Pre-existing wart worth knowing: on a period-less org that auto-create commit
early-commits the `billing_cycle_day` assignment made at `:201`. Not introduced here, not fixed here.

**Also fix:** `PUT /billing-cycle` assigns `current_period.start_date = new_start` (`settings.py:215`)
without checking whether a period already exists at `new_start`. That violates
`uq_billing_period_org_start` → uncaught 500. Add the pre-flight described above.

**Explicitly deferred to TBD-238:** migrating `Budget` to a `billing_period_id` FK.

## Error contract

Verified in `backend/app/main.py` and `backend/app/services/exceptions.py`:

- Domain `ValidationError` → **400**, `{"detail": …}`, no code (`main.py:376-378`).
- `ConflictError` → **409**, adds `"code"` when truthy (`main.py:381-389`). Constructor is
  `ConflictError(detail, *, code=None)` — `code` is **keyword-only** (`exceptions.py:20-29`).
- `RequestValidationError` → **422** with its own redacted `detail[i].input` shape (`main.py:463-488`).
  Do not hand-roll a 422; it would give the frontend two incompatible 422 shapes.

New conflict codes, snake_case to match `mixed_granularity` (`forecast_plan_service.py:218`):
`budget_period_conflict`, `billing_period_exists`.

**The 409 must be visible to the user.** `organization/page.tsx:287` maps errors through
`mapBillingCycleError`, **not** `extractErrorMessage`, and `frontend/lib/formErrors.ts:124-142` switches
on 400/422/403/429 with a `default:` fallback. A 409 currently falls through to "We could not save the
billing cycle. Try again." — byte-identical to what a 500 renders today, discarding the conflicting
category names this slice goes to the trouble of producing. Add a 409 case to `mapBillingCycleError` and
assert it in `frontend/tests/formErrors.test.ts` alongside the existing 422/403/429 cases. Without this,
scope item 4 has zero user-facing value.

## Testing

`backend/tests/routers/` currently has **zero** route-level coverage for the billing endpoints in
`routers/settings.py` (only `test_scheduler_settings_api.py`, `test_settings_feature_namespace.py`,
`test_settings_forecast_granularity.py` touch that router, none hit billing). Building that harness is a
large part of this slice's value.

New file: `backend/tests/routers/test_settings_billing_periods.py`, modelled on
`test_settings_forecast_granularity.py:54-71`.

**Harness requirement.** Those tests build a bare `FastAPI()` and mount only the router, so `main.py`'s
exception handlers are never registered and an uncaught `ConflictError` **re-raises through TestClient
instead of becoming a 409**. Register the domain handlers on the isolated app, following
`test_tags.py:124-139` — but **include `code` in the `ConflictError` handler body**, which
`test_tags.py:135-137` omits. Every 409 assertion below depends on this.

Backend cases:

- `POST /billing-period` with a valid body creates a period and returns **200**.
- `POST /billing-period` with no `start_date` returns **422** (framework-shaped), not 500.
- `POST /billing-period` with `end_date < start_date` returns **422**.
- `POST /billing-period` with a duplicate `start_date` returns **409** `billing_period_exists`.
- `POST /billing-period/close` writes an `org.billing_period.closed` audit row carrying
  `closed_period_id` and a resolved `close_date`.
- `PUT /billing-cycle` writes an `org.billing_cycle_day.updated` audit row.
- `PUT /billing-cycle` re-anchors budget `period_start` and `period_end`.
- **`PUT /billing-cycle` with `old_start == new_start` succeeds and re-anchors nothing.** This is the
  single most important new test in the slice; it is the `./pfv seed` path and the admin-resave path.
- `PUT /billing-cycle` returns 409 `budget_period_conflict` (not 500) when a budget already exists for
  the same category at the destination start.
- `PUT /billing-cycle` returns 409 `billing_period_exists` (not 500) when a period already exists at the
  destination start.
- `reanchor_period_dependents` with a **non-None** `new_end` writes both columns. Every other case here
  exercises the `None` path, so without this the "writes both columns" fix is never actually observed.
- `reanchor_period_dependents` returns an accurate count and leaves budgets untouched on conflict.
- Failure-outcome audit rows are written on rejection.
- Non-admin callers are rejected on every mutating endpoint.

Note: backend tests run on SQLite in-memory, where `SELECT … FOR UPDATE` is a no-op. This slice adds no
locking, so nothing here depends on that. TBD-235 will, and cannot be validated by this suite.

**Seed contract test.** `seed.py` is one monolithic `async def main()` against a hardcoded
`BASE = "http://localhost:8000"` using a real `httpx.AsyncClient`, with the period calls behind ~40 prior
network calls. It cannot be driven in-process. The test is therefore a **source-level AST guard**
asserting that both `POST /api/v1/settings/billing-period` calls pass `json=` and not `params=` — an
established house pattern (`backend/tests/test_no_raw_request_client.py`,
`test_deploy_workflow.py`, `test_requirements_pins.py`). Name it
`backend/tests/test_seed_billing_period_contract.py`.

**Frontend.** `frontend/tests/settings/organization-billing-period-polish.test.tsx` **already exists**
(268 lines, 10 tests) and asserts the `:567` string three times, at `:212`, `:227` and `:233`. **Edit that
file; do not create it.** Two tests must be rewritten:

- `"renders the projected close preview when the new value is dirty + valid"` (`:201`) — its entire
  premise is the projected date. Rewrite to assert the new re-anchor wording with the new computed
  `new_start` date.
- `"clears the preview once the value matches the saved one again"` (`:227`, `:233`) — same string,
  regex updated; the clear-on-match behaviour itself is unchanged.

Add assertions for the corrected `:300` confirm copy. Do **not** add one for `:491` (unreachable, see
above). Add the `mapBillingCycleError` 409 case to `frontend/tests/formErrors.test.ts`.

## Out of scope

| Work | Ticket |
|---|---|
| Read-only period roster page | TBD-234 |
| Per-row date editing / boundary moves | TBD-235 |
| `closed_at` decoupling | TBD-233 |
| Scheduler pinning guard | TBD-236 |
| Forecast spillover at close | TBD-237 |
| `Budget.billing_period_id` FK migration | TBD-238 |

## Risk

Low by construction. No schema change, no migration, no new UI, no change to any read path used by the
dashboard, transactions, budgets or forecast surfaces. `GET /billing-periods` keeps its exact shape, so
the cold-mount path guarded by `frontend/tests/app/transactions-cold-mount-single-fetch.test.tsx` is
untouched. `POST /billing-period` has no frontend caller. Both 500→409 flips are strict improvements: no
request that succeeds today starts failing.

Ranked residual risks:

1. **The identity case.** If the `old_start == new_start` rules in section 4 are not implemented exactly,
   an admin re-saving the same cycle day gets a hard 409 on a previously working no-op, and `./pfv seed`
   fails ~7 days a month. This is the default failure mode of a careless build and it is why that section
   is written as explicit rules rather than prose.
2. **`./pfv seed`** breaking if `seed.py` is not updated in the same commit. Developer-local only (seed is
   in no CI workflow), and mitigated by the AST guard plus `raise_for_status()`.
3. **Writing a non-null `Budget.period_end`** at the `PUT /billing-cycle` call site would corrupt
   open-period budget snapshots. Mitigated by stating `new_end = current_period.end_date` explicitly.
