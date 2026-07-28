# TBD-241 — `close_period`: chain-close, bound `close_date`, never delete a row

Status: REVISION 4 — APPROVED, implementation in progress
Date: 2026-07-28
Jira: TBD-241 (Bug, High, effort M) — child of TBD-213
Predecessors: TBD-232 (#586), TBD-239 (#587), both merged 2026-07-28
Related specs: `2026-07-27-billing-period-truth-and-safety.md`, `2026-07-28-billing-period-boundary-integrity.md`

> **Revision 4 — approved for implementation.** This spec drew **six independent sign-off
> reviews across three rounds**, each grounded in the source rather than in the document.
> Revisions 1 and 2 were rejected 2-0. Round 3 returned APPROVE-WITH-CHANGES plus a narrow
> REJECT that stated explicitly that **no design change is required**; every remaining finding
> was spec text or test construction, and all are folded here.
>
> **The clamp in §2 survived all six reviews unchanged.** Every reviewer independently confirmed
> that the strict `>` protects the lower bound, that `new_start == s0` exactly so no
> INSERT-inside-a-row is reachable on the clamped path, that convergence terminates, and that
> `ensure_future_periods` arm 1 survives. Rounds 2 and 3 additionally verified step e's and
> step h's autoflush safety on both entry paths, and D4's re-entrancy across a rollback.
>
> The rejections moved steadily outward — round 1 found design-level falsehoods, round 2 found
> contract and choreography defects, round 3 found only text and test mechanics. Round 2's
> blocker was answered by **subtracting**: `runner.py:67-72` already rolls back and records a
> failure row for any exception a job raises, so D11's partial-audit choreography was deleted
> outright rather than repaired. Full record in §8.

---

## 0. Correction to a merged spec

`2026-07-28-billing-period-boundary-integrity.md:45-48` records the residual `close_period`
overlap as acceptable because it "is unreachable from the UI, which sends no `close_date`."

**That is wrong, and it is the reason this ticket is being built now.** Two paths reach the
defect with no `close_date` from any caller. Both were found independently by two architects,
then verified at source, then re-confirmed by both sign-off reviewers.

**Path 1 — the scheduler, fully unattended.** `BillingCloseJob.run` computes
`boundary = current_cycle_window(cycle_day, today)[0]` and closes at `boundary - 1` in **one
jump** (`backend/app/services/scheduler/jobs/billing_close.py:27-29`), not one cycle at a time.
For an org whose open period is two or more cycles behind:

```
open   [2026-04-25, NULL)
stubs  [05-25, 06-24]  [06-25, 07-24]  [07-25, 08-24]     (a Forecasts mount creates 3)
today  2026-07-28
```

`run` writes `end_date = 2026-07-24` on the open row, which now wholly contains two intact stub
periods. `scheduler_enabled` (`config.py:258`; the 900s tick is `:259`) and
`automate_billing_close` (`scheduler/org_settings.py:21`) both default on.

**Path 2 — an ordinary manual close on a lapsed org.** Same roster, admin clicks the one Close
button. `close_date = 2026-07-27`, `new_start = 2026-07-28`. The revive lookup at
`billing_service.py:423-427` matches on **exact start only**, finds nothing at 07-28, and so
**INSERTs a new open row inside the still-live stub `[07-25, 08-24]`**. Two rows now own
07-28 through 08-24.

Neither path involves a `close_date` from a human. The residual-risk note is amended by this
document.

**Coordinates.** Every `billing_service.py` line citation in the TBD-241 Jira description is
stale by roughly +78 lines (written pre-#587). All the claims hold; the numbers do not. This
spec uses verified post-#587 coordinates throughout.

---

## 1. What is actually broken

`close_period` (`backend/app/services/billing_service.py:403-463`) has three defects:

1. **No upper bound on `close_date`** — `:412-413` guards only `close_date < current.start_date`.
2. **No awareness of intervening rows** — it sets `current.end_date = close_date` at `:418`
   regardless of how many later periods that window swallows.
3. **Revive is exact-start only** — `:423-428`. When `new_start` lands *inside* a later row
   rather than *at* its start, the `else` branch at `:432-434` inserts a second open row inside
   that row's window.

Defects 2 and 3 compound: the same close both swallows the stubs and opens a row inside one.

### Why the two previously rejected designs stay rejected

- **409 when `close_date >= next.start_date`.** Wedges the Close button permanently: the
  frontend sends no date (`frontend/app/settings/organization/page.tsx:326`), and
  `next.start_date` never moves until a close succeeds, so the button 409s every day forever.
  The scheduler wedges identically, retrying every 900s with no failure notification —
  `billing_close.py:36-41` dispatches on the success path only.
- **Absorb by DELETEing intervening rows.** `forecast_plans.billing_period_id` is NOT NULL with
  no `ondelete` (`models/forecast_plan.py:43`), so InnoDB RESTRICTs → MySQL 1451 → unhandled
  500. Future stubs routinely carry plans. Separately `Budget` has no FK at all and
  `period_start` is its sole join key, so a deleted period strands its budgets: invisible via
  `list_budgets` (which swallows the error and returns `[]`, `budget_service.py:92-95`) while
  still occupying `uq_budget_org_cat_period`.

**No design in this document deletes a `BillingPeriod` row, and none moves a row's
`start_date`.** Those two prohibitions are the spec's load-bearing invariants.

---

## 2. The design: clamp, then reuse the existing revive

**Clamp the close to the first intervening boundary.** Before writing anything, find

```
s0 = MIN(start_date)  over same-org rows  WHERE start_date > current.start_date
```

and clamp iff `s0 is not None and s0 <= close_date`, setting `close_date = s0 - 1 day`.

### Why this shape and not another

`new_start = close_date + 1 day`, so after clamping **`new_start == s0` exactly**. The existing
exact-start revive at `:423-428` then matches by construction and the row at `s0` becomes the new
open period, verbatim through the code path that already ships. That yields, for free:

- **No step-selection rules, no tie-break.** The target is `MIN(start_date)`, and
  `uq_billing_period_org_start` (`models/billing.py:12-14`) makes it unique. We never select a
  row by *containment* — only by *start* — so the "two rows contain `new_start`" problem the
  ticket worried about does not arise.
- **No `start_date` ever moves**, so `reanchor_period_dependents` is never called in its
  horizontal (`old_start != new_start`) mode. The ticket's `old_start`-capture-ordering trap is
  out of reach. (Its `ConflictError` is *not* out of reach — see D5, and the correction below.)
- **The lower bound cannot be violated.** Candidates satisfy `start_date > current.start_date`
  *strictly*, so `s0 - 1 >= current.start_date` unconditionally and the clamped date can never
  trip `:412-413`. This is the specific hazard that killed the 409 design. **A row starting at or
  before `current.start_date` but ending after it is a pre-existing overlap; it is excluded from
  clamp selection and logged, not repaired.** Repairing it is TBD-235.

### Worked examples

Cycle day 25, three stubs from a Forecasts mount.

| Roster before | `close_date` in | Clamped to | Result |
|---|---|---|---|
| open `[04-25,∅)`, stubs 05-25, 06-25, 07-25 | 07-24 (scheduler, today 07-28) | **05-24** | open ends 05-24; stub `[05-25,06-24]` revived open. Still due → next step. |
| open `[05-25,∅)`, stubs 06-25, 07-25 | 07-24 | **06-24** | open ends 06-24; stub `[06-25,07-24]` revived open. Still due. |
| open `[06-25,∅)`, stub 07-25 | 07-24 | none (`s0`=07-25 > close_date) | ends 07-24; stub `[07-25,…]` revived open. Not due. Converged. |
| open `[07-25,∅)`, no later rows | 07-27 (manual) | none | ends 07-27, new row inserted at 07-28. Unchanged from today. |

Every intervening period survives **as the closed period the user planned**, with its budgets and
forecast plans intact, rather than being folded into one giant row or deleted.

### ⚠ Operation order is load-bearing

`async_session` is built with **autoflush left on** (`backend/app/database.py:89` sets only
`expire_on_commit=False`). Any statement issued while a `BillingPeriod` INSERT is pending will
flush that INSERT, and a `uq_billing_period_org_start` violation then surfaces from wherever
that statement happens to be — which is how a previous round's bug escaped its `try/except`.
The order below is therefore **normative**, not illustrative:

```
close_period(db, org_id, close_date=None, *, today=None):
 1. current   = await get_current_period(db, org_id)         # uses db.execute
 2. today     = today if today is not None else date.today() # D2
 3. requested = close_date if not None else today - 1 day
 4. if requested > today:                 raise ValidationError    # D1
 5. if requested < current.start_date:    raise ValidationError    # pre-existing
 6. try:    new_period = await _apply_close_step(db, org_id, current, requested)
    except IntegrityError:  <D4 recovery — see D4; may call _apply_close_step once more>
 7. await db.refresh(new_period); return new_period

_apply_close_step(db, org_id, current, requested) -> BillingPeriod:      # NON-RECURSIVE
 a. straddling = (await db.scalars(...)).all()              # D12, pure SELECT, log only
 b. s0 = await _next_period_start(db, org_id, after=current.start_date)   # D9 — db.execute
 c. resolved = s0 - 1 day  if (s0 is not None and s0 <= requested)  else requested
 b'. if resolved != requested:                              # only when a clamp fired
        absorbed_ids = ids WHERE current.start_date < start_date <= requested   # D10
 d. new_start = resolved + 1 day
 e. await reanchor_period_dependents(..., old_start=current.start_date,
                                     new_start=current.start_date,
                                     new_end=resolved)                   # D5, identity
 f. current.end_date = resolved             # FIRST BillingPeriod mutation
 g. existing = await db.scalar(select(BillingPeriod)... start_date == new_start)
 h. if existing:  revived_id, prev_end = existing.id, existing.end_date   # CAPTURE FIRST
                  existing.end_date = None
                  await reanchor_period_dependents(..., old_start=new_start,
                                                   new_start=new_start, new_end=None)  # D5#2
    else:         db.add(BillingPeriod(org_id, start_date=new_start))
 i. await db.commit()                       # bare — IntegrityError propagates to the caller
 j. emit billing.close.clamped / billing.close.revived (D10); return the row
```

**Step h captures `revived_id` and `prev_end` BEFORE nulling `end_date`.** That overwrite is the
design's one irreversible write, and D10 calls `revived_previous_end` its load-bearing recovery
key; reading it after the assignment yields `None` and destroys exactly what the key exists to
preserve. **Emission is at step j, after the commit** — a pre-commit emit would describe a close
that then rolled back. The residual (process death between i and j) is accepted and recorded.

**Steps a and b′ must use `db.execute` / `db.scalars`, and `_next_period_start` (D9) must use
`db.execute`, never `db.scalar`.** See §5: `_next_period_start` compiles to a statement
containing both `"billing_periods"` and `"start_date"`, so a shape-keyed test patch would fire on
it instead of on step g and silently disable the very coverage that patch exists to create.

**The helper contains no recovery and therefore cannot recurse.** *(Revision 2 drew the boundary
at "steps 6-13", which put the `try/except` and the D4 recovery **inside** the helper the
recovery calls — unbounded recursion under sustained contention, and it made D4's "a second
`IntegrityError` propagates" unexpressible. Caught in round 2.)* The `try/except` lives in
`close_period`; the retry calls the helper exactly once more; anything it raises propagates.

**Autoflush safety (verified at source by both round-2 reviewers, on both entry paths).** Steps
a-e issue **no** `BillingPeriod` mutation, so nothing unique-violating is pending when D5's three
statements (`:336`, `:362`, `:384`) autoflush. Verified for the router path
(`settings.py:503-508` issues only SELECTs beforehand) and the scheduler path
(`close_period` commits at step i, and `get_current_period`'s auto-create branch at `:104-118`
commits *and* refreshes, leaving nothing pending). Step f makes only an UPDATE pending —
`end_date` participates in no unique constraint (`models/billing.py:12-14`) — so the autoflush at
step g is harmless, as is D5#2 at step h where only UPDATEs are pending. The only pending INSERT
is created at step h and flushed by the commit at step i. **No statement may be inserted between
steps h and i.**

`requested` and `resolved` are separate names deliberately: the helper takes the requested date
and returns the resolved one, so a retry re-derives the clamp from scratch rather than
re-clamping an already-clamped value.

### Duplicate open rows: this design does NOT repair them

*(Revision 1 claimed the opposite. It was wrong, and both reviewers caught it.)*

`get_current_period` returns the open row with the **greatest** `start_date`
(`billing_service.py:76-83`, `.order_by(start_date.desc())`). The clamp predicate is
`start_date > current.start_date`. A second open row is therefore always at a **strictly
earlier** start and is **structurally invisible** to clamp selection. Chain-close neither repairs
nor worsens the duplicate-open condition; it operates on the newest open row and leaves the
older one alone. Repair is TBD-235.

**Do not "fix" this by changing `get_current_period`'s ordering.** `ensure_future_periods` arm 2's
no-backward-overlap argument depends on `base` being the MAX open start, and says so at
`billing_service.py:216-218`.

### Convergence: one step per call, the job loops

`close_period` performs **exactly one** close per call. Multi-cycle convergence belongs to the
caller:

- **`BillingCloseJob.run` loops** until `is_due` is false, a cap of **24** iterations is hit, or
  a step fails to make progress (`new_period.start_date <= previous`, which all four reviewers
  believe unreachable — the guard is a cheap backstop, and it logs). On success it emits **one**
  audit row and **one** notification for the whole convergence. Rationale: `run` today produces
  one notification per close event to every org member (`billing_close.py:36-41`);
  one-step-per-tick would turn a three-cycle catch-up into three notifications, three audit rows
  and three tick-budget slots (`runner.py` `max_orgs`) spread over 45 minutes. Failure semantics
  are D11 — which, after round 2, is *"do nothing special"*.
  **Every iteration re-passes the same `boundary - 1`**, recomputed once per `run` from `today`.
  Do not re-derive the target from each new open start; the clamp is what advances the roster one
  cycle at a time, and `is_due`'s `current.start_date < boundary` guarantees
  `requested >= current.start_date` on every iteration, so the pre-existing lower-bound check at
  `:412-413` can never fire mid-convergence.
- **The manual route does not loop.** One click closes one period. This is the honest reading of
  a button labelled "Close billing period", it keeps each close individually audited, and the org
  page already displays the current period so progress is legible. With the scheduler on by
  default a lapsed org converges without anyone clicking.

---

## 3. Decisions requiring sign-off

**D1 — Upper bound is `close_date > today` → 400.** Strict, so "close yesterday" and "close
today" both remain legal. Verified this cannot reject the scheduler's own date:
`current_cycle_window` snaps back when the boundary is after today (`:49-51`), so
`close_date = boundary - 1 <= today - 1` unconditionally.

**D2 — Thread `today` in as keyword-only, and forward it from the scheduler.**
`close_period(db, org_id, close_date=None, *, today=None)`.

*Corrected in revision 3.* Revision 2 said "both production callers pass positionally so **no
call site breaks**" — true but incomplete to the point of being wrong, because it also claimed
D2 defuses the date bomb. It cannot do both. `billing_close.py:26-29` receives `today` and calls
`close_period(db, org.id, close_date)` **without forwarding it**, so step 2 would fall back to
`date.today()` and D1 would still reject. **Forwarding `today=today` at `billing_close.py:29` is
therefore a deliverable of this ticket, not an untouched call site.** Only the *router* call site
(`routers/settings.py:547`) stays unchanged — and it must, or the
`_boom(db, org_id, close_date=None)` monkeypatch at
`tests/routers/test_settings_billing_periods.py:591` becomes a confusing `TypeError`.

*Why it matters:* `tests/services/test_scheduler_job_billing_close.py:54-66` hardcodes
`today = date(2026, 8, 3)` → `close_date = 2026-07-31`, which D1 rejects against a real clock of
2026-07-28. **Without the forward, landing D1 turns that green test red on merge.** The bomb is
also only red for a few more days — after that it passes on its own and the requirement silently
looks optional. Build it now, while it is red.

Two honest residuals: (a) the manual path still reads `date.today()` internally, so D2 closes the
midnight-straddle race for the scheduler only — the path where it matters, since a tick can
straddle midnight and a click cannot straddle its own request; (b) `get_current_period`'s
auto-create branch reads `datetime.date.today()` at `billing_service.py:100` and is **not**
threaded by D2, so `today=` is not authoritative when no open row exists. Threading it there is
out of scope; the spec records the gap rather than implying it is closed.

**D3 — The clamp is the deliverable; the bound is hygiene.** Both production harms (§0) arrive
with a `close_date` at or before yesterday, which D1 accepts. A change that ships only the bound
closes the ticket and fixes nothing a user hits. Sign-off should reject any implementation that
lands D1 without §2.

**D4 — Re-entrancy on `IntegrityError`.** *(Rewritten. Revision 1's rule regressed a
currently-passing race into a 400 — see §8.)*

On `IntegrityError` from the commit at step i:

1. `await db.rollback()`.
2. Re-fetch the closing row **by `current_id`** — `select(BillingPeriod).where(id == current_id)`,
   exactly as `:444-446` does today. **Never call `get_current_period` on this path.** It
   auto-creates and commits a period when none is open (`:96-118`), and on a duplicate-open
   roster it can return an *earlier* row and send the scheduler's convergence loop backwards.
3. If that row is `None`, the org's periods were wiped mid-flight —
   `org_data_service.py:144` does `delete(BillingPeriod).where(org_id == ...)`, so this is
   reachable, not theoretical. **Raise `RuntimeError`.** *(Revision 2 wrote "(as today)"; that
   was false. Today's `:447` guard is `if current is not None and current.end_date is None:`,
   which tolerates `None` silently and falls through to the `new_period` lookup. D4 deliberately
   converts a silently-tolerated corrupt state into a loud one; the router audits it as a failure
   and re-raises.)*
4. **If `current.end_date is not None`, a racer closed the same period.** Its new open row is at
   `current.end_date + 1 day` — exact regardless of how the racer clamped. Fetch that row,
   **assert `end_date IS NULL`**, and **return it without writing anything and without
   re-validating `requested`**. The racer's own D5 ran in its own transaction. If the row is
   missing, or is not open → `RuntimeError`; returning a closed row here would make the route
   reply `{"end_date": None}` for a closed period (`settings.py:576-580`).

   *This rests on an invariant, not merely on today's code:* **no writer may set `end_date` on a
   row without leaving a row at `end_date + 1 day`.** Today only `close_period` sets `end_date`
   on an existing row (`POST /billing-period` only INSERTs), so it holds — but state it as an
   invariant so a future writer cannot quietly break the branch.
5. **Otherwise our own write lost to a peer INSERT at `new_start`.** Call `_apply_close_step`
   exactly once more with the original `requested` date — which re-derives the clamp, **re-issues
   D5** (the rollback discarded its UPDATE), and revives-or-inserts. The helper is non-recursive
   (§2), so a second `IntegrityError` propagates and is audited by the router's broad `except`
   (`routers/settings.py:548-566`).

Step 4 is the branch revision 1 got wrong. Its rule ("re-run the whole computation; if the
re-fetched current period is already closed, the racer did our work") was both unreachable —
`get_current_period` returns an open row by definition — and, under the natural reading, a
regression: two admins closing `[2026-06-25, NULL)` on 2026-07-28 would leave the loser
re-deriving `close_date = 07-27` against the racer's new `current.start_date = 07-28`, tripping
`:412-413` → **400 on a close that succeeded**, and on the scheduler path a
`scheduler.billing_close.failure` audit row for a tick that in fact worked.

**D5 — Refresh the closing period's budget snapshot (identity re-anchor), at a pinned call
site.** Call `reanchor_period_dependents(org_id, old_start=current.start_date,
new_start=current.start_date, new_end=resolved)` — the **identity** case, which the helper's own
docstring reserves for "the callers TBD-235/TBD-241 add back" (`:299-310`).

**⚠ `new_end` is `resolved` — never the raw `close_date` parameter.** `close_date` is the
caller's argument and is **`None` on every UI close** (`page.tsx:326` sends no date). Passing it
would drive the identity branch's `new_end is None` path (`:330-331`) and **blank `period_end` on
every budget of the period being closed** — the D5 defect inverted and amplified, since
`_compute_spent` then drops its upper bound (`budget_service.py:62-63`) for a *closed* period and
`_to_response:81` ships `period_end: null` to the client. This is why §2 names `requested` and
`resolved` separately. *(Revision 3 introduced that split but left this decision's text saying
`close_date`; caught in round 3.)*

*Why it matters:* `Budget.period_end` is a stored snapshot, written as `period.end_date` at
creation (`budget_service.py:149`). A budget created while its period was open carries
`period_end = NULL` forever, because nothing refreshes it at close. `update_budget:177` and
`transfer_budget:275` use the **stored** snapshot for any non-current period, and
`_compute_spent` drops its upper bound when `period_end is None` (`:62-63`). So a closed
period's budget response computes spend **unbounded forward**. `_to_response:81` also emits
`period_end` to the client, so the stale NULL is user-visible.

*⚠ Correction to revision 1.* Revision 1 claimed the `ConflictError` at
`billing_service.py:389-396` was "structurally out of reach" because it "only comes from the
horizontal re-anchor". **That is false.** `:383-396` sits in the helper's **shared tail** and
executes for the identity case too: the identity branch either returns at `:339-340` or falls
through into the shared pre-flight (`:349-381`) and the shared UPDATE-with-backstop
(`:383-396`). The genuine safety argument is narrower and has two parts:

1. The identity **pre-flight** cannot raise, because it carries the contradictory pair
   `period_start == old_start` and `period_start != old_start` (`:369-370`), so it matches
   nothing. This part of revision 1 was correct.
2. The shared `except IntegrityError` at `:389-396` is unreachable **only because step e runs
   before any `BillingPeriod` INSERT is pending** (§2, operation order). This is a
   *precondition the implementation must maintain*, not a structural impossibility.

If D5 were placed after `db.add(new_period)` — the naive placement, since D5 needs the clamped
`close_date` — then with a peer racing at `new_start` the autoflush is caught at `:389`,
`await db.rollback()` at `:392` **silently discards the close**, and the caller gets a **409
`budget_period_conflict`** — told a *budget* conflicts when a concurrent *period* close won. On
the **retry** invocation, which has no `try` around it, it escapes as a genuine unhandled **500**.

*⚠ Corrected by the post-implementation code review (§9, F5).* This paragraph used to open with
"the autoflush at `:336` raises `IntegrityError` **outside** `close_period`'s `try` → recovery
never runs → unhandled 500". **That is false**: the `try` wraps the entire `_apply_close_step`
call, so an autoflush from anywhere in steps a-i is caught and drives D4. The two real hazards are
the ones stated above. The distinction matters because test 12 was written against the false
version and therefore did not discriminate at all.

*Symmetry ruling (raised by both reviewers).* The **revived** row has the mirror problem: budgets
created against a stub carry that stub's old non-NULL `period_end`, which becomes stale the
moment the row is reopened, and `_to_response:81` ships it to the client. Step h therefore also
issues the identity re-anchor on the revived row with `new_end=None`; the helper's identity
branch handles the NULL case explicitly at `:330-331`. Safe at that point because only UPDATEs
are pending. Spend is unaffected either way (`list_budgets:112` and `update_budget:177` both use
the live end for the *current* period), so this is a display-correctness fix, not a money change.

*Scope note.* This moves money in a response payload, and TBD-240 was split out precisely to
isolate spend-semantics changes. The counter-argument, which this spec adopts and which one
reviewer explicitly endorsed: the stale snapshot is written **by this function's own incomplete
close**, only ever moves spend **down** to the correct value, is confined to closed periods, and
is **not** fixed by TBD-240 (those two callers read the stored column, not a derived end).
Deferring D5 to TBD-240 remains a legitimate sign-off outcome — rule on it explicitly.

**D6 — Copy.** One shipped string and one code comment become false and must change:
- `frontend/app/settings/organization/page.tsx:317-320` promises "This sets its end date to
  yesterday and opens a new period starting today." Under a clamped close both halves are wrong.
  Replacement must not name a date it cannot know before the call.
- the comment at `page.tsx:311-316` ("the service defaults to closing yesterday … and the
  replacement period opens today, not tomorrow") becomes equally false and must be updated with
  it, or the next reader will restore the wrong copy from it.

*Correcting the architects, and confirmed by both reviewers:* the **success toast** is **not**
false. `:331-335` renders `p.start_date` from a **refetch** of `/billing-period` after the close,
so it already reports the real new start whatever the clamp did. No change needed there.

**D7 — Error mapping.** *(Corrected — revision 1 misread the current mapper.)*
`mapBillingPeriodCloseError` **already has** a `case 400` branch
(`frontend/lib/formErrors.ts:174-178`); a 400 whose message matches neither `/already.*closed/i`
nor `/no.*open/i` returns that branch's `fallback`, never reaching `default`. So:
- add a **third message predicate inside the existing `case 400`** for the D1 future-date
  rejection. **D1's server message is pinned as `"Close date cannot be in the future"`** and the
  predicate matches `/cannot be in the future/i`, so the two are not written against each other
  by guesswork;
- add a **`case 409`** branch, which the mapper currently lacks entirely. This is **defence in
  depth, not a reachable path**: by D5's own argument the identity pre-flight matches nothing
  (`billing_service.py:369-370`), the `IntegrityError` backstop at `:389-396` is unreachable
  under §2's order, and the identity UPDATE changes only `period_end` so it cannot violate
  `uq_budget_org_cat_period` (`models/budget.py:14`). *(Revision 2 claimed a 409 was reachable;
  that overstated it.)* The retained comment on the *cycle* mapper at
  `frontend/lib/formErrors.ts:137-153` names TBD-241 as a future 409 source — re-point or remove
  it.

**D8 — `closed_at` (TBD-233) is NOT a prerequisite.** Both architects and both reviewers agree.
Chain-close never deletes, never moves a `start_date`, and writes at most two rows; that invariant
is checkable without a `closed_at` column. Given a clamped `close_date`, `new_start` equals an
existing row's `start_date`, so the revive is an exact match against a row starting **after** the
open period. *Softening one overstatement:* revision 1 called such a row "structurally a stub".
`POST /billing-period` (`routers/settings.py:436-440`) accepts an arbitrary `end_date`, so an
admin can hand-build a genuinely settled closed period at a later start, and the clamp would
reopen it. Rare, requires a hand-built roster, and the conclusion stands — but the reasoning is
"very likely a stub", not "necessarily".

**D9 — Ship the successor-start primitive as a named helper.**

```python
async def _next_period_start(db, org_id: int, *, after: datetime.date) -> datetime.date | None:
    """MIN(start_date) among the org's periods with start_date > `after`."""
```

**No upper-bound parameter** — the clamp applies `s0 <= close_date` at the call site. This is
deliberate so TBD-240 can consume the same helper unchanged for `effective_end`, which needs the
unbounded form. D9 is the concrete artifact that makes TBD-241-first cheaper for TBD-240.

**D10 — Audit mechanism.** *(New — revision 1 specified two audit keys with no way to produce
them.)* `close_period` returns a bare `BillingPeriod` (`:403`, `:463`), and D2 freezes its
signature so the `_boom` monkeypatch keeps matching. Changing the return type would break
`billing_close.py:33` and that monkeypatch. **Ruling: do not change the return type.** Instead:

- **The service emits structured log events** carrying the full detail:
  `billing.close.clamped`, `billing.close.straddling_row_ignored`,
  `billing.close.convergence_capped`. `billing.close.clamped` carries `org_id`,
  `requested_close_date`, `clamped_to`, `absorbed_period_ids`, **`revived_period_id`** and
  **`revived_previous_end`**.
- **`absorbed_period_ids` means the rows the REQUESTED window would have swallowed** —
  `{p.id : current.start_date < p.start_date <= requested_close_date}`, computed at step b
  before any mutation. *(Revision 2 left this undefined, and under the literal reading it was
  **always empty**: after clamping, every other row starts at `>= s0 > resolved`, i.e. outside
  the window actually closed. The useful quantity is counterfactual, and the spec now says so.)*
- **`revived_previous_end` is the load-bearing recovery key, and it rides its own event.**
  Chain-close's one irreversible write is `existing.end_date = None` on the revived row
  (`billing_service.py:430`). D8 concedes an admin can hand-build a settled closed period that
  gets reopened; without recording the overwritten `end_date`, that value is unrecoverable from
  anywhere. *(Revision 2 claimed `resolved_close_date` alone made the blast radius recoverable.
  It does not — it describes the row being closed, never the row being reopened.)*

  **It must be emitted on ANY revive, not only on a clamped one**, so it gets its own event
  `billing.close.revived` (`org_id`, `revived_period_id`, `revived_previous_end`) rather than
  riding `billing.close.clamped`. *(Round 3 caught this: the unclamped revive — `s0 == new_start`,
  the ordinary stub case in worked-example rows 3 and 4 — overwrites `end_date` too, and no
  `clamped` event fires there. Attaching the key to `clamped` would lose it on the common path.)*
  `revived_previous_end` is `null` for the ordinary case of reviving a stub whose `end_date` was
  already what we expected; a non-null value on an unclamped revive is the signal worth alerting
  on later.
- **The route's audit row** keeps its existing key **`close_date`**, whose value is the local
  `resolved_close_date = new_period.start_date - 1` (`routers/settings.py:568`, written at
  `:572`). It is derived rather than echoed and so **already** reports the clamped date with no
  code change, satisfying the docstring decision at `:489-493`. *(Do not confuse the local
  variable with the audit key, as revisions 2 and 3 did.)*

  It gains one key: **`requested_close_date`** — a **verbatim echo of the raw `close_date`
  parameter, recorded as `null` when absent**. The route must **never** re-derive the service's
  "yesterday" default to fill it; that is exactly what `:489-493` forbids, and under D2 it would
  additionally drift because the route does not pass `today`. **Honest limitation:** since the UI
  sends no date (`page.tsx:326`), this key is `null` for every human close, so the audit row
  alone still cannot distinguish "asked for 07-27, clamped to 05-24" from "asked for 05-24". The
  clamp signal is `billing.close.clamped`; the key earns its place for API and PAT callers, not
  for the UI path. *(Revision 3 overclaimed this; round 3 caught it. Note the failure path's
  existing key at `:561` is `close_date`, not `requested_close_date`.)*
- **The scheduler's audit row gains `steps` (an integer count) and `closed_period_ids` (the ids
  of the rows whose `end_date` this run wrote).** *(Revision 2 used `steps` as a count in one
  place and indexed it as a date sequence in another, making test 20 unwritable. The date now
  lives in `closed_on`.)* **Mechanism:** `close_period`'s return type is frozen, and `is_due`
  fetches the closing row only to discard it (`billing_close.py:21-24`), so the loop must capture
  the closing row's id itself each iteration — `get_current_period(db, org.id)` at the top of the
  iteration, before calling `close_period`. `closed_on` is derived as
  `new_period.start_date - 1 day`, the same derivation the route uses at `settings.py:568`.

**D11 — Convergence failure and cap semantics.** *(Rewritten by subtraction. Revision 2's version
was the round-2 blocker both reviewers led with.)*

`close_period` commits internally, so the loop is N independent transactions, not one. Revision 2
required `run` to emit a partial notification and a partial success audit row before letting a
mid-convergence exception propagate. **That is deleted.** It does not work and it is not needed:

- *It does not work.* The exception D11 exists for is D4 step 5's second `IntegrityError`, which
  escapes `close_period`'s `await db.commit()` and leaves the `AsyncSession` deactivated.
  `dispatch_notification_to_org_members` opens with `await db.execute(...)`
  (`notification_service.py:569-575`) and then `db.begin_nested()` (`:579`) — both raise
  `PendingRollbackError` on such a session. The handler meant to guarantee a notification would
  guarantee its absence, and `runner.py:67-72` would then record the *wrong* error.
- *It is not needed.* `runner.py:67-72` already catches every job exception, rolls back, and
  writes a `scheduler.billing_close.failure` audit row. The steps that committed stay durable,
  `is_due` is still true next tick, and the loop resumes and notifies **900 seconds later**. The
  system self-heals; the only cost is a delayed notification on a rare path.

**Ruling: a mid-convergence failure propagates untouched.** No rollback dance, no partial audit,
no partial notification. Three consequences to record rather than hide:

(a) the org's period has advanced with no notification until the next successful tick;
(b) because `run` raises rather than returning, `runner.py:63-64` never sets `org_did_work`, so a
    partially-successful convergence consumes no `max_orgs` budget — while on the *success* path
    one org can now consume up to 24 closes against a single budget slot, which `runner.py:39-46`
    frames as a burst-size control. Both halves are deliberate trades of the convergence design;
(c) **the self-heal claim is scoped to *mid*-convergence failures only.** If every step commits
    and the failure comes afterwards — at `record_run`, `dispatch_notification_to_org_members`, or
    the final commit (`billing_close.py:35-42`) — then `is_due` is already false, the next tick
    does nothing, and the notification is lost until the next cycle boundary. This is pre-existing
    single-step behaviour, but chain-close widens the window from one close to up to 24 behind a
    single dispatch. Recorded, not fixed here.

Still required of `run`:
- `counts["closed_on"]` must report the **last actually-applied** close date, not
  `boundary - 1` (`billing_close.py:32`). Those differ whenever the final step was clamped or the
  cap fired.
- Hitting the 24-step cap logs `billing.close.convergence_capped` and returns success for the
  steps taken; the next tick continues.
- `await db.commit()` at `billing_close.py:30` moves **outside** the loop. It is a no-op for the
  period writes (`close_period` already committed) but it must not run per iteration.

**D12 — Straddling-row detection is an explicit step with an explicit predicate.** *(New —
revision 2 required the `billing.close.straddling_row_ignored` event and a test for it, but the
"normative" step list had no slot for the SELECT that finds one, and never said whether an open
row counts.)* At step a:

```
straddling = rows WHERE org_id = :org AND id != current.id
                    AND end_date IS NOT NULL
                    AND start_date <= current.start_date
                    AND end_date   >= current.start_date
```

Pure SELECT, no mutation, safe anywhere in steps a-g. **`end_date IS NOT NULL` is documentation
of intent, not protection** — in SQL three-valued logic `end_date >= :start` already fails to
match a NULL `end_date`, so an open row could not satisfy the predicate either way. This is the
same redundancy `ensure_future_periods` arm 2 carries and explains at `billing_service.py:208-210`;
keep it for the same reason and keep test 6b as a cheap fence. *(Revision 3 justified the clause
by claiming a duplicate open row would otherwise match. It would not — round 3 caught it. The
clause and the ruling stand; only the reason was wrong.)*

---

## 4. Ordering hazards

**Compute the clamp before mutating anything, and place D5 before any pending INSERT.** The
normative order is in §2. The reason is **autoflush of a pending `BillingPeriod` INSERT**, not
anything about the clamp predicate itself.

*Revision 1's stated rationale here was wrong* and is corrected: it claimed the clamp must run
first "otherwise `current` itself can satisfy a predicate meant for later rows." `current` can
never satisfy `start_date > current.start_date`, whatever its `end_date`. A reader who checked
that reasoning would conclude the ordering was optional for a `start_date`-only predicate and
place D5 after `db.add` — which is exactly the 500 in D5. The rule is right; only this reason is.

**`ensure_future_periods` arm 1 must be re-verified, and it holds.** The comment at `:183-192`
argues arm 1 (exact start) exists because `close_period` **revives** rather than inserts, so a row
arm 2 matched as closed can become open at a start the loop is still proposing. Chain-close
changes *which* row is revived but not *that* it revives, and no `start_date` moves, so arm 1
covers exactly the window it covered before. Both reviewers confirmed. Any future design that
moves a `start_date` breaks it — note that in the code.

---

## 5. Test plan

House rule: FK-sensitive assertions belong in `tests/routers/test_settings_billing_periods.py`,
whose fixture sets `PRAGMA foreign_keys=ON` (`:75`).
`tests/services/test_billing_service.py` does not, so an FK violation passes there undetected.

**Service — `tests/services/test_billing_service.py`**
1. No later rows → `BillingPeriod` outcomes identical to today (regression fence). *Not* "byte
   identical": D5 now writes `Budget.period_end` where today's code writes nothing.
2. One intervening stub, `close_date` past its start → clamped to `start - 1`; stub revived
   open; stub row count unchanged; no INSERT.
3. Three intervening stubs, `close_date` past all → clamps to the **first**; the other two
   untouched and still closed.
4. `close_date` before the first stub's start → no clamp.
5. Clamp target chosen by `MIN(start_date)`, asserted against a deliberately unordered insert
   order.
6. Row straddling `current.start_date` per D12's predicate → **excluded** from clamp selection;
   no 400; `billing.close.straddling_row_ignored` logged. Guards against re-creating the rejected
   409 wedge. **The test must also pin the residual**: with straddler `X = [06-01, 08-31]`, open
   `C = [06-25, NULL)` and no clamp candidate, the close still INSERTs an open row at 07-28
   *inside* `X`. That is §1 defect 3, unrepaired and deliberately out of scope (TBD-235). Assert
   it, so the limitation is recorded rather than discovered later.
6b. Duplicate open row at an earlier start → `straddling_row_ignored` does **not** fire (D12
   excludes `end_date IS NULL`); only `get_current_period`'s `multiple open billing periods`
   warning does.
7. **Duplicate open rows → non-interference** *(rewritten; revision 1 asserted the opposite and
   was unwritable)*. Two open rows `A=[04-25,∅)` and `B=[06-25,∅)`: the close operates on **B**
   (the MAX-start row `get_current_period` returns), leaves **A** untouched and still open,
   creates no third overlapping row, and the `multiple open billing periods` warning at
   `:84-90` fires. Repair is TBD-235.
8. D1: `close_date = today + 1` → `ValidationError`. `close_date = today` → accepted.
9. D2: `today=` kwarg respected; `close_period` never consults `date.today()` when it is passed.
10. D4 step 5: peer INSERT at `new_start` → `_apply_close_step` re-runs, one retry, commit
    succeeds, and **`Budget.period_end` equals the resolved close date afterwards** (proves D5
    was re-issued rather than lost to the rollback).
11. D4 step 4: racer closed the same period → returns the row at `current.end_date + 1` with
    **no further write** and **no `ValidationError`**. This is the case revision 1 turned into a
    400.
12. **D5 ordering fence:** INSERT branch (no row at `new_start`) **plus** a concurrent peer row
    at `new_start` **plus** D5 enabled → must reach D4's recovery, and must raise neither an
    unhandled `IntegrityError` (500) nor `ConflictError("budget_period_conflict")`. This is the
    regression fence for the autoflush hazard; it must be written against the post-D5 call order.
13. D5: identity re-anchor refreshes `Budget.period_end` at the closing start; no-op when already
    correct; and the revived row's budgets are re-anchored to `period_end = None`.

⚠ **Test-plan item 11 of revision 1 was wrong about `test_billing_service.py:166`, and the truth
is worse.** The gate `call_count["n"] == 2` **never matches today**: `get_current_period` uses
`await db.execute` (`:76`), not `db.scalar`, and returns without entering the auto-create branch
when an open period exists — so it makes **zero** `AsyncSession.scalar` calls, and the existence
check at `:423` is scalar call **#1**. The patch therefore never forces a miss, the **revive**
branch runs, the commit succeeds, and no `IntegrityError` is ever raised. Every assertion still
passes because reviving the peer row and recovering onto the peer row produce identical end
state. **Consequence: `close_period`'s recovery block `:436-460` — the block D4 rewrites — has
zero coverage today.** *(Verified directly, not taken on report.)*

The fix is not to renumber the gate. Re-anchor it **semantically** and make the patch **one-shot**
(see below), and treat the resulting first-ever execution of the recovery path as new coverage
that D4 must be validated against.

*Revision 3 corrects revision 2's account of the ordinal hazard, which was exactly backwards.*
D5 at step e issues `await db.scalar(select(Budget.id)...)` at `billing_service.py:336`, so **the
ordinal shifts by one regardless of how the clamp is implemented**:

- **Clamp via `db.execute`** (the recommendation): D5's Budget SELECT is scalar call #1, and it
  compiles to `budgets.period_start` — matching neither `"billing_periods"` nor `"start_date"` —
  so it falls through. The existence check is call #2 and matches both substrings → **the gate
  fires for the first time ever**, the INSERT branch is taken, and D4's recovery finally executes.
- **Clamp via `db.scalar`**: clamp #1, D5 #2 (fails the substring test), existence check #3 →
  **the gate stays dead** and D4 ships with the same silent non-coverage round 1 found.

An implementer who read revision 2's inverted claim, used `db.execute`, and concluded "the
ordinals did not shift, so I can re-anchor later" would ship precisely that hole. The semantic
re-anchor is therefore **mandatory, not conditional**, and the test must additionally assert that
the recovery path was entered — e.g. a counter incremented inside the `except IntegrityError`
arm — so it cannot silently revert to trivial coverage.

**⚠ The patch must fire exactly once.** Both proposed re-anchors (compiled-statement shape, or an
`.execution_options` tag) key on the *statement*, and step g issues **the same statement** on the
retry. A patch that forces a miss both times sends the retry down the INSERT branch again,
raising the second `IntegrityError` that D4 step 5 propagates — so tests 10, 11 and 12 would
assert "commit succeeds" against a path that by construction cannot. Every such patch needs an
explicit `fired` flag that lets the retry's identical statement through. *(The existing patch is
one-shot only by accident of `call_count["n"] == 2`, which is the very thing being removed.)*

**⚠ And it must fire on the right statement.** A shape-keyed patch matching
`"billing_periods" in compiled and "start_date" in compiled` also matches D9's
`_next_period_start`, which compiles to `SELECT min(billing_periods.start_date) FROM
billing_periods WHERE ... start_date > ...`. If `_next_period_start` used `db.scalar`, it would
consume the single firing, return `None`, suppress the clamp, and let step g find the peer row for
real — revive branch, commit succeeds, **`IntegrityError` never raised, D4 never entered** — and
the obvious "fix" (deleting the recovery-entered counter) restores precisely the silent
non-coverage this whole note exists to eliminate. **Resolution, pinned in §2: `_next_period_start`
and step b′ use `db.execute`/`db.scalars`, so `db.scalar` calls belong only to D5 (`:336`) and
step g.** *(Round 3 caught this; the two re-anchor mechanisms are not interchangeable.)*

**Racer simulation.** Both fixtures use `StaticPool` over a single in-memory SQLite connection
(`tests/services/test_billing_service.py:24-28`,
`tests/routers/test_settings_billing_periods.py:66-70`), so genuine concurrency is impossible and
every racer must be simulated. For tests 10 and 12 the peer row is inserted and committed up
front, and the one-shot patch makes our existence check miss it.

**⚠ Test 11 needs a different mechanism, and revision 3's recipe for it was impossible.** It said
to seed `current.end_date` before `close_period` runs. That cannot work: `get_current_period`
filters `end_date.is_(None)` (`billing_service.py:76-83`), so it would not return that row at all
— with no other open row it takes the auto-create branch (`:96-118`), **commits a different
period**, and `current.id` is never the seeded row's id, so D4 step 4 is unreachable. Seeding the
row *open* does not work either: under `StaticPool` a second session shares one transaction, so
the racer's close is rolled back along with ours.

The only workable construction: seed the row **open**, seed the peer row at `resolved + 1` **open**
(so D4 step 4's `end_date IS NULL` assertion passes), force the step-g miss to reach the
`IntegrityError`, and apply the racer's close — `UPDATE billing_periods SET end_date=<resolved>
WHERE id=<current_id>` plus commit — **after** D4 step 1's `rollback()` and **before** the step-2
re-fetch, where the session is momentarily clean. Patch `AsyncSession.rollback` or the re-fetch
statement itself to inject it. Without this, D4 step 4 — the branch revision 1 got wrong, inside a
recovery block with zero coverage today — ships unverified.

**Router — `tests/routers/test_settings_billing_periods.py`** (FK-enforcing)
14. Close over a stub that **carries a `ForecastPlan`** → 200, plan intact, no 1451. Guard
    against the rejected DELETE design. *(§9 F6: this spec, and the first draft of the comment in
    the test file, claimed 14 and 15 "pass against `main`". Checked during the code review — both
    **fail** against `main`. They are regression fences as well as guards.)*
15. Close over a stub that **carries budgets** → 200, budgets still reachable via `list_budgets`
    at their own period start. Same correction as 14.
16. *(rewritten per D10)* The route's audit `close_date` equals the **clamped** date on a clamped
    close — which `resolved_close_date` already produces — and the service emits
    `billing.close.clamped` with `requested_close_date`, `clamped_to`, `absorbed_period_ids`.
17. `_boom` monkeypatch at `:591` still matches the signature after D2.

**Scheduler — `tests/services/test_scheduler_job_billing_close.py`**
18. Lapsed multi-stub org converges in **one** `run` call: final period correct, **one**
    notification, **one** audit row carrying `steps`. No such off-grid fixture exists today —
    the truth-and-safety spec already recorded that gap.
19. **Global post-condition after convergence:** no two rows **with `end_date IS NOT NULL`** have
    intersecting windows, and exactly one row has `end_date IS NULL`. *(Revision 2 asserted this
    over all rows, which is false as a design invariant and contradicts test 23: the open row
    read as unbounded intersects every later stub, which is the normal, intended state —
    `billing_service.py:196-206` documents exactly that reading and why arm 2 avoids it.)*
20. D11 failure path — **split across two files**, because
    `test_scheduler_job_billing_close.py` calls `job.run(...)` directly and its
    `_silence_side_effects` patches `record_run` in the **job** module namespace, not the
    runner's:
    - *In the job file:* step 3 of 3 raises → `pytest.raises`, steps 1-2 durable, **no**
      notification and **no** partial success row emitted, `is_due` still true, and a second
      `run` converges the remainder and notifies.
    - *In `tests/services/test_scheduler_runner.py`:* driven through `run_all_due` with the house
      patches (`monkeypatch.setattr(R, "async_session", session_factory)`,
      `R.org_settings.get_bool`, and `sched_audit.async_session` so the row is observable rather
      than merely counted), assert `runner.py:67-72` writes `scheduler.billing_close.failure`
      carrying the original `IntegrityError` message.
20b. `revived_previous_end`: a revive that reopens a row whose `end_date` was **non-null** emits
    `billing.close.revived` with the overwritten value and the row id — asserted on **both** a
    clamped and an unclamped revive, since the unclamped one is the common path and emits no
    `clamped` event.
21. Convergence cap: a roster needing more than 24 steps stops, logs
    `billing.close.convergence_capped`, returns success, and `counts["closed_on"]` reports the
    last applied close date rather than `boundary - 1`.
22. **`BillingCloseJob.run` forwards `today=today` to `close_period` (D2).**
    `test_run_closes_and_is_idempotent:54-66` then passes with its hardcoded
    `today = date(2026, 8, 3)` under D1; without the forward it goes **red on merge**.
    `test_cycle_day_25_not_due_early_but_due_on_boundary_and_idempotent:89-91` hardcodes `today`
    too and should be threaded for consistency (its `close_date` of 2026-07-24 survives either
    way).
23. `ensure_future_periods` run **after** a clamped close still creates non-overlapping stubs
    (asserts §4's arm-1 argument rather than only arguing it).

**Frontend** — the confirm-copy assertion at
`frontend/tests/settings/organization-billing-period-polish.test.tsx:318` (which matches
`/sets its end date to yesterday and opens a new period starting today/i`) updated for D6; the
new 400 message predicate and the new 409 branch in `mapBillingPeriodCloseError` (D7).

---

## 6. Audit and observability

**D10 is authoritative for every key; this section is a pointer, not a second definition.**
*(Revisions 2 and 3 restated the key lists here and drifted from D10 both times — round 3 blocked
on it, because §6 is titled "Audit and observability" and is where an implementer looks.)*

- Route audit row: existing key `close_date` (already the clamped date, no code change) plus
  `requested_close_date` — see D10 for the null-on-UI-path limitation.
- Scheduler audit row: `steps` (int) and `closed_period_ids`, with `closed_on` reporting the last
  applied close date. **No `partial` key** — D11 deleted partial audit rows outright, and there is
  no longer any path on which `run` writes one. The only failure row is `runner.py:69-70`'s, whose
  detail is `{"error": str(exc)}` and which `run` cannot influence.
- Service structured events: `billing.close.clamped`, **`billing.close.revived`** (carrying
  `revived_period_id` and `revived_previous_end` — see D10; emitted on *any* revive),
  `billing.close.straddling_row_ignored`, `billing.close.convergence_capped`. Emission point is
  step j, after the commit.

**Open ruling for the implementer to honour, not to decide:** a structlog event is accepted as
the record for `revived_previous_end`. It is not a durable `AuditEvent` row, and that is a
conscious trade — writing one would require the service to open its own session via
`record_audit_event(async_session, ...)` mid-close. If operational experience shows reopened
settled periods actually occur, promoting this to an audit row is the follow-up.

## 7. Out of scope

- The unbounded open-period spend window (`budget_service.py:62`,
  `forecast_plan_service.py:272`, and the third site neither ticket names,
  **`budget_rebalance_service.py:344`**) — TBD-240. Note `effective_end` **does not exist**
  today; TBD-240's description says #587 shipped it, and #587 did not. D9 gives TBD-240 the
  primitive.
- Repairing rosters already corrupted, including duplicate open rows — TBD-235.
- `closed_at` — TBD-233 (D8).
- Re-anchoring `ensure_future_periods` on today rather than the open start — TBD-235 blocker 1.

## 8. Sign-off record

**Round 1 — revision 1: REJECT / REJECT** (two independent grounded reviewers, 2026-07-28).
Both confirmed the core clamp design: the lower bound cannot be violated (strict `>`),
`new_start == s0` exactly so no INSERT-inside-a-row is reachable, convergence terminates, and
`ensure_future_periods` arm 1 survives. Neither could construct a roster where an unclamped close
creates a new overlap. Five defects were folded into revision 2:

1. **"A free repair" was false** — `get_current_period` returns the MAX-start open row, so the
   clamp can never see a second open row. Test 7 was unwritable. → §2 "Duplicate open rows",
   test 7 rewritten as non-interference, plus the do-not-reorder warning.
2. **D5's call site was unpinned and its safety claim false** — `:383-396` is in the helper's
   shared tail and runs for the identity case; placed after `db.add`, autoflush converts to a
   false `budget_period_conflict` 409 after a rollback that discards the close, and escapes as a
   500 from the retry invocation. *(Round 1's own wording — "outside `close_period`'s try" — was
   itself wrong; corrected in §9, F5.)* This is the same class
   of bug a previous round was burned by. → normative operation order in §2, corrected D5,
   corrected §4 rationale, new test 12.
3. **D4's retry regressed a passing race into a 400** — re-running `get_current_period` after
   rollback returns the racer's new period, and `close_date = yesterday` then trips the lower
   bound; the scheduler would write a failure audit row for a successful tick. → D4 rewritten
   around re-fetch-by-`current_id`, with the racer-won branch returning
   `current.end_date + 1 day`; new tests 10 and 11.
4. **D4 dropped D5 on the retry path** — both rollbacks discard the uncommitted budget UPDATE.
   → `_apply_close_step` shared by both paths; test 10 asserts the snapshot.
5. **§6's `clamped_to` / `absorbed_period_ids` had no mechanism** and test 14 was unwritable;
   **convergence partial-failure was undefined** (rows advance, zero notifications). → new D10
   and D11, tests 16 and 20.

Also folded: D7 misread the existing 400 branch and missed the absent 409; D8's "structurally a
stub" overstated; D2 does not close the midnight race on the manual path; the revive-side budget
snapshot asymmetry (now ruled on in D5); tests 14/15 relabelled as guards; citation fixes
(`config.py:258`, `budget_service.py:92-95`).

**⚠ Correction to revision 2's own round-1 record.** It claimed "neither [reviewer] could
construct a roster where an unclamped close creates a new overlap." Round 2 constructed one, and
**the post-implementation code review constructed a second, with two concurrent writers and no
straddler at all — see §9, F1, which is why the implementation takes a row lock.** Round 2's:
straddler `X = [2026-06-01, 2026-08-31]`, open `C = [2026-06-25, NULL)`, today 2026-07-28.

*How that roster is actually reachable — round 3 corrected round 2 here.* **Not** by posting both
rows: #587's containment guard (`routers/settings.py:396-416`) rejects either ordering with a 409.
The reachable path is to post `X` into an org with no other rows and let
`get_current_period`'s auto-create branch (`billing_service.py:96-118`, which has **no** overlap
guard) insert `C` inside it. The distinction matters: a reader who checked the `POST` citation
would conclude TBD-239 already closed this and delete D12.
`_next_period_start(after=06-25)` returns `None` because `X.start_date` is not `> C.start_date`,
so no clamp fires, and the close INSERTs an open row at 07-28 wholly inside `X`. **The design is
unchanged** — D12 excludes straddling rows deliberately and logs them, and repair is TBD-235 —
but the residual is real and test 6 now pins it.

**Round 2 — revision 2: REJECT / REJECT** (two fresh independent reviewers, 2026-07-28).
Both re-verified and confirmed the round-1 folds that mattered most: §2 step e's and step h's
autoflush safety on **both** entry paths, D5's shared-tail correction, D10's `resolved_close_date`
derivation, and D4's re-entrancy. All six round-2 blockers sat in the periphery, and both
reviewers led with the same one:

1. **D11 dispatched on a poisoned session** (both reviewers, independently). The
   `IntegrityError` D11 exists for leaves the session deactivated, so
   `dispatch_notification_to_org_members`' opening `db.execute`
   (`notification_service.py:569-575`) raises `PendingRollbackError` — guaranteeing the absence
   of the notification D11 was written to guarantee, and making `runner.py` record the wrong
   cause. → **D11 deleted by subtraction**, not patched: `runner.py:67-72` already handles it and
   the next tick self-heals.
2. **`_apply_close_step` spanned steps 6-13**, which put the D4 recovery *inside* the helper the
   recovery calls — unbounded recursion under contention, and it made "a second `IntegrityError`
   propagates" unexpressible. → boundary redrawn in §2; helper ends at a bare commit.
3. **D2's "no call site breaks" contradicted D2's own stated benefit.** `billing_close.py:29`
   does not forward `today`, so as written D1 would have turned a green test red on merge. →
   forwarding is now an explicit deliverable.
4. **`absorbed_period_ids` was empty by construction** under the literal reading, and the one
   irreversible write (the revived row's overwritten `end_date`) was recorded nowhere, falsifying
   D10's blast-radius claim. → redefined counterfactually; `revived_period_id` /
   `revived_previous_end` added; `requested_close_date` added to the route's success audit.
5. **§5's ordinal analysis was exactly inverted** — D5's own `db.scalar` at `:336` shifts the
   ordinal regardless of the clamp's implementation, so the gate fires with `db.execute` and
   stays dead with `db.scalar`, the opposite of what revision 2 said. Compounding it, every
   proposed re-anchor keys on the statement and would fire again on the retry, making tests 10-12
   unwritable. → corrected, semantic re-anchor made mandatory, one-shot `fired` flag required,
   racer-simulation mechanism named.
6. **Test 19's post-condition was false as an invariant** (the open row read as unbounded
   intersects every later stub — the normal state, and the one test 23 depends on). → scoped to
   `end_date IS NOT NULL`.

Also folded: `steps` typed as an integer with the date moved to `closed_on`; D12 added with an
explicit predicate and a ruling on `end_date IS NULL`; D4 step 3's "(as today)" corrected
(`org_data_service.py:144` makes the `None` case reachable and today tolerates it silently); D4
step 4 asserts the fetched row is open and states the underlying invariant; D6 extended to the
code comment at `page.tsx:311-316`; D7's 409 reachability softened to defence-in-depth and D1's
server message pinned; test 1 rescoped; the frontend test coordinate cited; `billing_close.py:30`'s
commit pinned outside the loop; the `max_orgs` / `org_did_work` consequence recorded; runner
citation corrected to `:67-72`.

**Round 3 — revision 3: APPROVE-WITH-CHANGES / REJECT (narrow)** (two fresh reviewers,
2026-07-28). **The design is settled.** Reviewer A: *"No design change is required: §2's clamp and
normative operation order, D4's re-entrancy, D11's subtraction ruling, and D12's exclusion of
`end_date IS NULL` are all sound as rulings."* Reviewer B: *"Implementation may begin now on §2,
D1-D9, D12 and the D2 scheduler forwarding — I could not break any of it."* Both independently
re-derived and confirmed the round-2 subtraction: `record_run` writes through its **own** session
(`scheduler/audit.py:20` -> `audit_service.py:117`), so the failure row lands even on a poisoned
session; `is_due` stays true after a partial convergence; `loop.py` is unaffected by `run` raising;
and `_apply_close_step` is safely re-executable after a rollback (the by-id re-fetch repopulates
the expired instance before any attribute is read, which also avoids a `MissingGreenlet`).

Six findings folded into revision 4, none of them a design change:

1. **D5's own text still said `new_end=close_date`** while §2 said `resolved`. Since `close_date`
   is `None` on every UI close, following D5 literally would have driven the identity branch's
   NULL path and **blanked `period_end` on every budget of the closing period** — the D5 defect
   inverted. → `new_end=resolved`, with the reason stated.
2. **`revived_previous_end` had no capture point, no test, and rode the wrong event.** The value
   is destroyed by the assignment that makes it interesting, and the *unclamped* revive (the
   common path) emits no `clamped` event at all. → captured before nulling in step h, moved to its
   own `billing.close.revived` event on any revive, emitted at step j after the commit, test 20b.
3. **§6 still carried revision-2 text** — the deleted `partial` key, and a key list that dropped
   D10's recovery keys. → §6 is now a pointer to D10, not a second definition.
4. **Test 11 was unwritable as specified** (seeding `end_date` before the call makes
   `get_current_period` auto-create a different period; `StaticPool` prevents a second session's
   commit surviving our rollback). → the racer's close is injected between D4's rollback and its
   re-fetch; peer row seeded open.
5. **The two re-anchor mechanisms were presented as interchangeable and are not** —
   `_next_period_start` compiles to a statement containing both substrings a shape-keyed patch
   matches, so with `db.scalar` it would consume the single firing and silently disable D4's
   coverage. → §2 pins `db.execute`/`db.scalars` for steps a, b and b'.
6. **Test 20 was filed where its assertion cannot run** (the job test file patches `record_run` in
   the job namespace, not the runner's). → split across the job file and
   `test_scheduler_runner.py`, with the house patches named.

Also folded: stale step numbers renumbered to the lettered scheme; D12's rationale corrected (SQL
3VL already excludes NULL, so the clause is documentation like arm 2's at `:208-210`); D11's
self-heal claim scoped to mid-convergence failures with the post-loop window recorded; the
`max_orgs` burst trade recorded on the success path too; `requested_close_date`'s null-on-UI-path
limitation stated honestly; `closed_period_ids` and `closed_on` given mechanisms; `absorbed_ids`
given a step; and §8's own `POST /billing-period` reachability claim corrected (#587's guard
rejects it; the roster is reachable via the auto-create branch instead).

**Verification of the remaining items is delegated to the post-implementation code review**, since
all six are assertions the review checks directly against code rather than prose.

## 9. Code-review record (post-implementation)

Two independent reviewers read the implementation against this spec and both returned
**READY AFTER FIXES**. Seven findings were folded; two were Important. No design ruling from
§2-§4 changed. What follows records the one that *did* change the code's shape, and corrects a
claim §8 makes about the design's own safety.

### F1 — the concurrency residual, and the correction to §8

**§8's round-1 record says "neither [reviewer] could construct a roster where an unclamped close
creates a new overlap." Round 2 already narrowed that to a single-writer claim (the straddler
roster). The code review shows it is false with two concurrent writers, in a way that has nothing
to do with the clamp.**

`uq_billing_period_org_start` serialises only writers that compute the **same** `new_start`. The
two production callers routinely compute different ones: `BillingCloseJob` passes
`boundary - 1`, a UI close passes yesterday. When the resolved dates differ, **both closes
commit, neither raises, D4 never runs, and nothing is logged.**

```
org cycle_day 1, single row  P0 = [2026-06-01, NULL),  clock 2026-07-28

scheduler  resolves new_start = 2026-07-01
admin      resolves new_start = 2026-07-28      (concurrently)

result     P0 [06-01, 07-27]   overlapping   X [07-01, NULL)   plus   Y [07-28, NULL)
```

This is **not a regression** — `main` corrupts identically, because it has neither a lock nor a
clamp. But this ticket exists to stop `close_period` producing overlaps, and the convergence loop
widens the exposure from one transaction per tick to up to `MAX_CONVERGENCE_STEPS`.

**Resolution: a row lock on the open period, taken before anything is read or decided.**
`close_period` re-selects the closing row by id with `.with_for_update()` and
`.execution_options(populate_existing=True)` (`_lock_period`), the house pattern at
`budget_service.py:199`, `transaction_service.py:222` and `admin_orgs.py:445`. The lock covers
the whole read-decide-write sequence: it is released only by `_apply_close_step`'s commit at step
i, or by D4's rollback, which immediately re-takes it. Every closer therefore serialises on the
row itself rather than on the date each happened to compute.

If the locked row comes back with `end_date IS NOT NULL`, a racer already closed it. That is
**exactly D4 step 4's situation**, so it reuses D4 step 4's ruling rather than closing a second
time: return the row at `end_date + 1 day`, asserted open, with no write and no re-validation of
`requested`. The shared logic now lives in `_period_after_racer_close`, reached from both places.
The lower-bound check (step 5) moved *after* the lock so the racer branch cannot be reached
through a 400.

`FOR UPDATE` is silently dropped by the SQLite dialect, so every existing fixture is unaffected.
Genuine concurrency is impossible under `StaticPool`, so two tests pin the decision instead: one
asserts a `FOR UPDATE` select is issued against `billing_periods`, and one drives a pre-closed
locked row and asserts it routes to the racer branch with **zero** `_apply_close_step` calls.

**D4 step 4's stated invariant now carries the lock's weight too.** No writer may set `end_date`
on a row without leaving a row at `end_date + 1 day` — with the lock in place, that invariant is
what makes the loser of a race correct rather than merely lucky.

### The other six

- **F2 (`MissingGreenlet`).** `BillingCloseJob.run` bound `closing` as an ORM instance and read
  `.id` / `.start_date` *after* `close_period` returned. D4's `db.rollback()` expires the whole
  identity map and repopulates only the row at `current_id`; when `close_period`'s internal
  `get_current_period` picked a different row, `closing` stays expired and the read fires a sync
  lazy-load inside an async session. Both attributes are now snapshotted into plain values
  immediately — the pattern `routers/settings.py:504-518` already documents.
- **F3 (the convergence audit over-reported).** The racer branch returns a row without writing
  anything, and the loop still counted a step, appended an id and derived `closed_on` from it.
  D10 defines `closed_period_ids` as the rows whose `end_date` *this run* wrote, so `close_period`
  gained an optional keyword-only **out-parameter** `closed_ids` (its return type stays frozen per
  D10) and appends `current_id` only on the paths that actually wrote. The loop counts `steps`
  from that, and counts **iterations** separately for the cap — a run whose every step is absorbed
  by a racer applies nothing, and a cap keyed on `steps` would never fire. `closed_on` is `null`
  when nothing was applied.
- **F4 (D5's headline rule was untested under a clamp).** Tests 10, 12 and 13 all have
  `s0 > requested`, so no clamp fires; router test 15's clamp fires but the closing period carries
  no budget. An implementation passing `requested` instead of `resolved` to step e would have left
  the whole suite green while writing a `Budget.period_end` that overshoots the actual close. A
  test now fires a clamp with a budget on the closing period and asserts `period_end` is the
  clamped `2026-05-24`.
- **F5 (test 12 did not discriminate, and two docstrings were false).** The claim that a misplaced
  D5 "raises `IntegrityError` outside `close_period`'s try (an unhandled 500)" is wrong — that try
  wraps the entire `_apply_close_step` call, so an autoflush from anywhere in steps a-i is caught
  and drives D4. §5's justification for test 12, `_apply_close_step`'s docstring and the test's own
  docstring all said it; all three are corrected. What is actually load-bearing is (a)
  `reanchor_period_dependents`' **own** backstop, which converts an autoflushed `IntegrityError`
  into `ConflictError(budget_period_conflict)` after a `db.rollback()` that discards the close, and
  (b) the **retry** invocation inside the `except` arm, which has no `try` around it and so escapes
  as a genuine 500. Test 12 could not discriminate at all: with D5 misplaced this fixture still
  converges, because the rollback clears the pending INSERT and the retry takes the revive branch.
  It now asserts the invariant directly — D5 is called with **no `BillingPeriod` INSERT pending**,
  on every invocation.
- **F6 (comments and copy).** Four corrections. The router test file claimed tests 14 and 15 "pass
  against `main`"; both **fail** against it (`main` opens 2026-07-25 where test 14 expects
  2026-05-25, and leaves `period_end = 2026-06-24` where test 15 expects `None`) — they are
  regression fences as well as guards. Test 13's `already_correct` assertion was tautological (the
  budget is seeded with exactly the value D5 writes, so the column cannot distinguish "skipped"
  from "rewritten identically"); it now asserts `reanchor_period_dependents`' **rowcount** is 1,
  not 2. The confirm copy's "if a later period already exists, the close stops at the day before
  it starts" is not universally true — a later period beyond the close date stops nothing — so it
  now states only the guarantee that always holds. And §4's arm-1 note ("any future design that
  moves a `start_date` breaks it") is now recorded **in the code**, at
  `ensure_future_periods` arm 1, where a future implementer will read it.
- **F7 (loud-failure branches uncovered).** D4 step 3's `RuntimeError` (the closing row wiped
  mid-flight via `org_data_service.py:144`) and the racer-row-missing-or-closed `RuntimeError`
  both shipped with zero coverage. One test each, both driving the real recovery path.

### Residuals accepted, not fixed

- The clamp still does not repair a **straddling** row (D12, test 6's second half) or a
  **duplicate open** row (test 7). Both remain TBD-235.
- The row lock serialises `close_period` against itself. It does **not** serialise it against
  `POST /billing-period` or `ensure_future_periods`, which insert without taking it — those still
  reach `close_period` as an `IntegrityError` at step i, which is what D4 is for.
