# TBD-234 — read-only billing period roster, and the anomaly kernel

Status: REVISION 2 — awaiting re-sign-off
Date: 2026-07-29
Jira: TBD-234 (Story, Medium, effort M) — child of TBD-213. **Blocks TBD-233 / TBD-242.**
Predecessors, all merged: TBD-232 (#586), TBD-239 (#587), TBD-241 (#588), TBD-240 (#589)
Related specs: `2026-07-28-open-period-spend-window-design.md`, `2026-07-28-close-period-chain-close-design.md`, `2026-07-28-billing-period-boundary-integrity.md`, `2026-07-27-billing-period-truth-and-safety.md`

Sequencing settled by two independent architects, unanimous. Revision 1 was then **REJECTED 2-0**
by independent sign-off, on nine blocking findings. **The most serious was mine and it was
structural: revision 1's derivation rule would have made the anomaly kernel a no-op** (§2.2), and
three of its own tests would have passed vacuously proving it. Full record in §7.

Their design requirements are folded throughout; §3 records the decisions.

---

## 0. Corrections to the ticket

**0.1 — Both stated producers of corruption are already fixed, and the ticket is stronger without
them.** "`PUT /billing-cycle` re-roots a start without touching the predecessor's end" was deleted
by #587 — the route now writes one column and re-anchors no period (`routers/settings.py:200-220`).
*(Precision, per sign-off: it still calls `get_current_period` at `:260` to build its audit payload,
which auto-creates and commits for an org with no open row. So "touches no period row" is not
literally true; "performs no boundary write" is.)*
"`close_period` on a non-stub-aligned date leaves stubs stranded" was clamped by #588
(`billing_service.py:786-798`).

**The real value, restated.** The corruption those produced is still in the data, nothing repairs
it, and **five shipped tickets have now deferred residuals to a detector that does not exist**:

| Deferred to TBD-234 | Where |
|---|---|
| `POST /billing-period`'s overlap check is SELECT-then-INSERT, TOCTOU on real MySQL | `routers/settings.py:417-421` — names "TBD-234's anomaly kernel" verbatim |
| `_apply_close_step` logs `billing.close.straddling_row_ignored` and walks past | `billing_service.py:914-922` |
| `ensure_future_periods`' "known hole: blind to a SECOND open row" | `billing_service.py:226-232` |
| `get_current_period` logs `multiple open billing periods` and nothing else | `billing_service.py:84-92` |
| A skipped stub candidate leaves a **gap**, logged as `billing.stub.skipped_overlap` | `billing_service.py:246-259` |

Today the fleet's roster health is observable only by grepping structlog. And #589 shipped
`period_effective_end` **with no production caller**, its docstring naming this ticket as its only
consumer and telling readers not to prune it.

**0.2 — The index analysis is wrong in a way that changes the query design.** The ticket says "the
only relevant index is `ix_transactions_org_settled_date` and it will not be used." Two errors:
that index is `(org_id, **status**, settled_date)` (`020_add_settled_date_to_transactions.py:26-30`)
— `status` sits *between* the two useful columns, so it is unusable for the **unfiltered count**
(which deliberately drops the status predicate) but perfectly usable for the **settled-net** column
(which pins `status = SETTLED`); and `ix_transactions_org_date` on `(org_id, date)` also exists
(`004_categories_transactions.py:55`), covering the `settled_date IS NULL AND date BETWEEN` arm.
**The two columns have opposite index stories.** See D7.

**0.3 — The open period cannot be selected in the transactions filter at all.**
`transactions/page.tsx:268-273` builds `date_from`/`date_to` only when `end_date !== null`, and the
dropdown is fed by `closedPeriods` (`:332-335`). So the ticket's click-through-parity rationale is
unreachable **via the dropdown** for the row users will click first. *(Corrected in revision 2: the
page does accept `date_from`/`date_to` URL params (`transactions/page.tsx:303-311`, `DATE_PARAM_RE`
at `:49`), so a roster row can deep-link the open period's window directly — which is how this page
should link out.)* The count rule stands on internal consistency regardless (D7). Making the open
period selectable in the dropdown is a recorded follow-up, not fixed here.

**0.4 — Verified correct:** `SettingsLayout.tsx:53` compares `activeTab === tab.href` as a plain
string, so `/settings/organization/periods` highlights the Organization tab with no new top-level
tab; `effective_period_date_expr()` is `coalesce(settled_date, date)`
(`transaction_filters.py:132`) and is not sargable; `list_transactions` applies no
`reportable_transaction_filter`, so the unfiltered-count ruling holds; `use-billing-periods.ts`
shares one SWR key and the cold-mount guard is real.

---

## 1. Scope

One new read-only endpoint, one anomaly kernel, one page.

- **`GET /api/v1/settings/billing-periods/roster`** — new, admin-gated (D9).
- **`find_period_anomalies`** in `billing_service.py` — **the named backend deliverable**, not an
  implied UI feature. Both merged specs already assign it here
  (`2026-07-28-billing-period-boundary-integrity.md:298`). Testable without the route.
- **`/settings/organization/periods`** — a page rendering `SettingsLayout` with
  `activeTab="/settings/organization"`.

**Do NOT widen `GET /billing-periods`.** It is on the cold-mount critical path for transactions,
dashboard and forecast-plans via one shared SWR key (`frontend/lib/hooks/use-billing-periods.ts`),
guarded by `frontend/tests/app/transactions-cold-mount-single-fetch.test.tsx`.

**Descope recorded:** `2026-07-28-billing-period-boundary-integrity.md:298` assigns TBD-234 an
anomaly kernel, a **sweep script**, and the roster page. The sweep script is deliberately **not** in
this ticket — a per-org admin page answers "is *this* org healthy", not "how many orgs are
affected", which is §0.1's actual complaint. It needs operator-authorized prod DB access and is a
different deliverable. **Mint it as a follow-up** rather than letting it disappear; the kernel this
ticket ships is what it will call.

No migration. No schema change. No write path. Nothing existing is edited except adding the route
and the page.

---

## 2. The shape of the data

### 2.1 Two ends, both shown

This is the design's centre of gravity and the ticket does not mention it.

On a lapsed org `period_effective_end` returns a date **in the past** — `ensure_future_periods`
anchors stubs on the open period's `start_date`, not on today (`billing_service.py:171`, docstring
`:159-164`) — while `period_spend_window_end` floors at today (`:600-601`). So a naive roster
renders an "Open" period that *ended in May* while `/budgets` bills spend through July.

**Ruling: render both, explicitly labelled.** A "Period ends" column (derived) and a "Counting
through" column (spend window), with the two visually differentiated only when they diverge. Ship
one number and this page becomes the one that proves the app is lying; ship both and the divergence
*is* the diagnostic. This is a deliberately-accepted residual of TBD-240 §7, surfaced rather than
introduced.

### 2.2 The derived end — one definition, stated once

⚠ **Revision 1 got this wrong, and the error was structural.** It ruled "derive each end as
`successor.start_date - 1 day` in Python" **for every row**. That is not what
`period_effective_end` does: it returns `end_date` **verbatim** for closed rows
(`billing_service.py:509-510`) and uses the successor only for the *open* row. Composed with §2.4's
gap rule, revision 1's version made `row.end ≡ successor.start - 1` identically, so
`successor.start > row.end + 1` reduced to `successor.start > successor.start` — **false for every
row on every roster. The gap and overlap detectors would have been dead code**, and revision 1's
tests 1, 2, 7 and 10 would have passed vacuously proving it. Both reviewers caught it independently.

**The rule, which is exactly `period_effective_end`'s semantics:**

```
effective_end(row, successor) = row.end_date                     if end_date IS NOT NULL
                              = successor.start_date - 1 day     if open and a successor exists
                              = None                             if open and no successor (tail)
```

**Ruling: call `period_effective_end` per row. Do not reimplement it in Python.** Revision 1's N+1
justification was also wrong: the helper **returns before touching the DB** for any closed row
(`billing_service.py:509-510`), so on a healthy roster with one open row it costs exactly **one**
extra query, not N. The in-Python rewrite bought nothing and caused the defect above.

**Consequence worth stating, because it looks like a bug and is not:** the open row can never
produce a gap or overlap against its successor — its end is *defined* by that successor, so they
abut by construction. Gaps and overlaps are detected between rows with a real `end_date`. That is
why the healthy shape `[…closed…, OPEN, stub, stub]` yields no markers (§2.4).

**Window edges.** The window's lower boundary still needs **one row older** than it, used only to
compute the leading gap/overlap marker — never for display, and never for an end derivation (no
displayed row's end depends on it). Revision 1 also mandated "one newer than its upper", which
contradicts D8's no-upper-bound; deleted.

### 2.3 The status partition

Four divergent definitions of "current" already exist in the frontend; the roster must not become a
fifth. **The server computes `status` and it becomes canonical** — the field TBD-242 will later
point every frontend site at. It must be a **partition** (every row gets exactly one), and it must
name the shape the frontend disagrees about rather than folding it into `past` or `current`:

**Evaluated in this order; first match wins. The order is normative** — revision 1 gave four
unordered predicates that were *not* disjoint, and two implementers writing the `if/elif` in
different orders would both have satisfied it while producing different canonical answers.

```
1. invalid              end_date IS NOT NULL  AND  end_date < start_date
2. open                 end_date IS NULL
3. upcoming             start_date > today
4. current_by_calendar  start_date <= today <= end_date
5. past                 end_date < today
```

**`invalid` is not hypothetical.** `models/billing.py:12-14` carries no CHECK constraint, and
`close_period` has no `end_date >= start_date` guard: `_apply_close_step` writes
`current.end_date = resolved` (`billing_service.py:866`) and the only date guard is
`requested > today` (`:995`). Reviewer B walked a two-close sequence that commits one — close with
`close_date = today`, which legally leaves the open row starting **tomorrow**
(`billing_service.py:607-610` documents this state), then close again from the UI, which sends no
date so `requested = yesterday`. Result: `start = tomorrow`, `end = yesterday`. Without branch 1
such a row matches **both** `upcoming` and `past`. It is also a **fifth anomaly shape** (§2.4).

`current_by_calendar` is the disputed shape, and the divergence is **sharper** than revision 1
said. Corrected: on the dashboard such a row is *neither* current, past, nor future —
`dashboard/page.tsx:271-273` computes `isCurrent = end_date === null` (false),
`isPast = end_date < today` (false, it contains today), `isFuture = start_date > today` (false). It
falls through all three. On Forecasts it is *Current* (`ForecastPlansClient.tsx:253-256`, calendar
containment). Naming it explicitly is what lets TBD-242 resolve this against a tested definition
instead of inventing a fifth.

**What `status` does NOT do:** it classifies rows, it does not *select* one. On a lapsed roster the
open row is `open` while a stub is `current_by_calendar`; on a corrupt roster two rows can both be
`current_by_calendar`. Choosing which row a screen should display remains TBD-242's problem, and
this spec deliberately does not pre-empt it.

### 2.4 The anomaly kernel

**Four shapes, not two.** The ticket names gaps and overlaps; the code defers two more here:

1. **gap** — `successor.start_date > row.end_date + 1 day`.
2. **overlap** — `successor.start_date <= row.end_date`.
3. **duplicate open rows** — more than one `end_date IS NULL`. **The most damaging shape**: every
   frontend `findIndex(p => p.end_date === null)` silently picks the first, so two rows both claim
   "current" and different screens can pick differently. Already detected-and-only-logged at
   `billing_service.py:84-92`, named as a known hole at `:226-232`, and reachable through
   `POST /billing-period` (`seed.py` does it).
4. **no open row** — zero rows with `end_date IS NULL`. Every consumer calls `get_current_period`,
   which would *auto-create and commit* one.

5. **inverted row** — `end_date < start_date` (§2.3 branch 1). Production-reachable; revision 1
   omitted it entirely.

Plus one informational marker: **straddling row** — `start_date <= open.start_date AND end_date >=
open.start_date`. ⚠ **`>=`, at or after — matching `_apply_close_step`'s own predicate at
`billing_service.py:776-777`.** Revision 1 said "ends after it", which would silently under-report
exactly the shape whose deferral (`:914-922`) created this marker.

**Marker precedence, normative.** A straddling row is by construction also an overlap under rule 2.
Revision 1's test 8 asserted "straddling → not an overlap" while its rules emitted both. **Ruling:
a row may carry multiple markers, and `straddling` is emitted *in addition to* `overlap`, not
instead of it.** Suppressing the overlap would hide genuine overlaps on any roster containing a
straddler — precisely the rosters this page targets.

**Two output sets, because one of these is not clock-free:**

| set | markers | clock |
|---|---|---|
| **structural** | gap, overlap, duplicate_open, no_open, inverted, straddling | none |
| **temporal** | lapsed_open (derived end in the past) | reads `today` |

Revision 1 put `lapsed_open` in one undifferentiated set and then asserted the whole set was
clock-independent — a direct contradiction, since "in the past" is a comparison against `today`.
The split is what makes the D4 fence (§4 test 9) both true and able to fail.

**⚠ The kernel consumes `period_effective_end`, never `period_spend_window_end`.** This is the
entire reason #589 split one helper into two (its §2.1): handing a gap/overlap detector a
clock-dependent end paints phantom overlaps between the open row's floored window and the historic
stubs on *every* lapsed org — verbatim the failure
`reference_billing_period_boundary_model.md` exists to prevent. **Fence it with a test: same
roster, two different injected `today` values, identical marker set.**

**Gap versus tail.** `period_effective_end` returns `None` for the roster tail
(`billing_service.py:512-513`). An open row with no successor is not a gap. And the intended shape
`[…closed…, OPEN(end=NULL), stub, stub, stub]` must not read as an overlap — that is the exact
false positive the boundary model warns about.

### 2.5 Response contract

Revision 1 specified none, and TBD-242 is contracted to adopt `status` **by name**. Normative:

```jsonc
{
  "window": { "from": "2025-08-01", "to": null, "truncated": false },
  "periods": [
    {
      "id": 41,
      "start_date": "2026-07-25",
      "end_date": null,                  // raw column, null for the open row
      "effective_end": "2026-08-24",     // period_effective_end; null only for the roster tail
      "counting_through": "2026-08-24",  // period_spend_window_end; null only for the tail
      "status": "open",                  // open | upcoming | current_by_calendar | past | invalid
      "length_days": 31,                 // null when effective_end is null
      "transaction_count": 42,           // unfiltered (D7)
      "settled_net": "-1240.55"          // string, per the repo's Decimal convention
    }
  ],
  "anomalies": [
    { "kind": "gap",      "from_period_id": 40, "to_period_id": 41,
      "from_date": "2026-07-01", "to_date": "2026-07-24" },
    { "kind": "duplicate_open", "period_ids": [41, 44] }
  ]
}
```

- `status` values are the snake_case literals above. `invalid` is branch 1 of §2.3.
- **Ordering: `start_date` ASC.** A roster is read as a timeline; `list_periods`' DESC ordering is
  for a different consumer.
- `settled_net` serializes as a **string**, matching the repo's existing Decimal convention
  (`specs/tech-debt-frontend-decimal-typing.md`).
- `anomalies` carries the **structural** set plus `lapsed_open`; each marker names the period ids it
  concerns and, for `gap`/`overlap`, the date range. Clients must tolerate unknown `kind` values.
- **Empty roster** (org with zero periods): `200`, `periods: []`, and `anomalies: [{"kind":
  "no_open"}]`. D10 forbids manufacturing a row to avoid this.
- `months` outside 1..60 is clamped, not rejected — a diagnostic page should not 422 on a URL typo.

---

## 3. Decisions

**D1 — New endpoint, not a widened one.** §1. `GET /settings/billing-periods/roster`.

**D2 — Server-computed `status`, canonical, a partition.** §2.3.

**D3 — Both ends rendered, labelled.** §2.1.

**D4 — The anomaly kernel is the named backend deliverable**, lives in `billing_service.py` beside
the two helpers, is testable without the route, and consumes only the pure helper. §2.4.

**D5 — Call `period_effective_end` per row; do NOT reimplement it.** §2.2. Fetch one row older
than the window's lower bound for the leading marker only. *(Revision 1 ruled the opposite and that
is what produced the dead-detector defect.)*

**D6 — Per-row bounded aggregates, over a hard-capped N. Reject the single grouped `CASE` query
outright.** The ticket correctly warns that a `CASE` returns the first match — and then still frames
the work as one grouped query. On the page whose *purpose* is exposing overlaps, a transaction must
be attributable to **every** period that contains it. Overlapped transactions appear in more than
one row's count, and the UI carries an explicit note that counts may exceed the org total where
periods overlap. **Columns that sum to the org total would be a lie on precisely the corrupt rosters
this page targets.**

⚠ **N must be capped explicitly.** Revision 1 asserted "N ≤ 24" — but the only 24 in the codebase is
`list_periods`' `LIMIT` (`billing_service.py:149`), which D8 forbids routing through, and D8 removes
the upper bound entirely. `POST /billing-period` accepts arbitrary starts, so an org can hold
hundreds of rows and revision 1's design would have issued **two aggregate queries per row,
unbounded**. Ruling: **`months` is clamped to 1..60** (following the house pattern at
`routers/settings.py:467`, `count = min(max(count, 1), 6)`), and the roster query carries
**`LIMIT 200`**. When the cap truncates, the response says so (§2.5) rather than silently showing a
partial roster on the page whose subject is over-populated rosters.

**D7 — Two columns, two filters, two *different* predicate shapes.** ⚠ Revision 1 named the right
indexes and then mandated one predicate shape for both columns — which **defeats the very index it
claims each column uses**. Both reviewers caught it. Corrected:

- **Settled net — `reportable_transaction_filter()` + `status = SETTLED` + plain
  `settled_date BETWEEN a AND b`.** No `OR`, no `coalesce`. That is a clean three-column range on
  `ix_transactions_org_settled_date` = `(org_id, status, settled_date)`. The `settled_date IS NULL`
  disjunct revision 1 mandated is **dead code here** — `SETTLED ⇒ settled_date NOT NULL` is a model
  and DB invariant (`transaction_service.py:582`, enforced `:594-599`, backfilled by migration
  `020:20-23`) — and adding it removes the range from the trailing key part, collapsing the plan to
  `(org_id, status)`.
- **Transaction count — UNFILTERED**, because `list_transactions` applies no
  `reportable_transaction_filter` and a filtered count would not match click-through. With no status
  predicate, `ix_transactions_org_settled_date` is unreachable (its `status` column sits in the
  middle) and a top-level `OR` across two columns degrades to an `org_id` prefix scan. **Ruling: a
  two-branch `UNION ALL`** — one branch `settled_date BETWEEN …` , one branch
  `settled_date IS NULL AND date BETWEEN …` (which does use `ix_transactions_org_date`). If the
  implementer measures that a plain `coalesce(...) BETWEEN` scan is acceptable at the capped N, that
  is a legitimate alternative — **but it must be recorded as an accepted org-scan, with the cost
  stated, not presented as sargable.**

**D8 — Window semantics, with a floor so the page cannot render empty.** `?months` (default 12,
clamped 1..60) is a **calendar lookback from today**, with **no upper bound**, so future stubs
always appear as `upcoming`.

⚠ **Revision 1 stopped there, and that returns an empty roster on the most lapsed orgs** — an org
whose open row is `[2022-03-01, NULL)` with stubs through 2022-06 has *zero* rows inside a 12-month
lookback, so the page renders blank for precisely the roster it exists to diagnose. Reviewer B
called this strictly worse than the alternative revision 1 rejected. **Ruling: the window is
`(rows within the lookback) ∪ (the newest 12 rows)`**, so a lapsed roster always renders. Then
`LIMIT 200` (D6).

**Anomalies are computed over the returned window, not the whole roster**, and the response states
the window bounds so a marker's absence is never mistaken for health. One exception: `no_open` and
`duplicate_open` are computed over **all** of the org's rows via a cheap `COUNT(*) WHERE end_date IS
NULL`, because both are org-level facts that a windowed view would report falsely — a lapsed org's
open row can sit outside the lookback, which would make `no_open` fire on an org that has one.

`list_periods` caps at 24 with no window parameter (`billing_service.py:144-151`) — **do not route
through it**; the roster needs its own query.

**D9 — Admin-gated.** Both existing billing-period reads are ungated (`routers/settings.py:314`,
`:327`) while every mutation calls `_require_admin`. The roster exposes org-wide transaction counts
and settled net, and its page sits under `/settings/organization`, whose `SettingsLayout` tab is
`minRole: "admin"` (`SettingsLayout.tsx:13`). Gate it, or the page renders for a user the API
refuses.

**D10 — Never call `get_current_period` on this route.** It auto-creates **and commits** a
`BillingPeriod` when none is open (`billing_service.py:96-120`). A read-only diagnostic that
manufactures the row it reports on is disqualifying — and "no open row" is one of the four
anomalies this page exists to *report*. (Note the existing `GET /billing-period` route does exactly
this today; that is out of scope here but worth a follow-up.)

**D11 — No writes, no migration, no schema change.** The page reports; TBD-235 repairs.

---

## 4. Test plan

House rules: anchor dates relative to `date.today()`; FK-sensitive assertions belong in the router
suite (`PRAGMA foreign_keys=ON`); **every service-level test names its public entry point.**

⚠ **Each item below is labelled `fence` (fails against an implementation missing the rule) or
`guard` (passes either way, kept as a regression net).** Revision 1 labelled none, and three of its
"fences" could not fail — the fourth, fifth and sixth instances of this programme's signature
defect, in a spec whose own §4 warned about it.

**Kernel — `tests/services/test_period_anomalies.py`** (entry point: `find_period_anomalies`)

| # | Test | Kind |
|---|---|---|
| 1 | Clean contiguous roster → no anomalies | guard |
| 2 | The healthy shape `[…closed…, OPEN, stub, stub]` → **no anomalies**. The false-positive fence the boundary model exists to prevent | **fence** — fails if the open row's end is read as unbounded |
| 3 | Gap between two **closed** rows → one `gap` with correct bounds | **fence** — fails against revision 1's dead-detector derivation |
| 4 | Overlap between two **closed** rows → one `overlap` | **fence**, same |
| 5 | Duplicate open rows → `duplicate_open` | **fence** |
| 6 | Zero open rows → `no_open` | **fence** |
| 7 | Roster tail (open row, no successor) → **not** a gap | **fence** |
| 8 | Straddling row → `straddling` **and** `overlap` (§2.4 precedence) | **fence** |
| 8b | Row with `end_date < start_date` → `invalid`, and §2.3 assigns status `invalid` not `upcoming`/`past` | **fence** |
| 9 | **Clock independence of the structural set.** One roster, clock **frozen** at two different dates → identical *structural* markers; `lapsed_open` may differ | **fence** — see below |
| 10 | `effective_end` matches `period_effective_end` row by row, on a fixture containing **at least one gapped and one overlapped closed row** | **fence** — see below |

**⚠ Test 9's mechanism is load-bearing.** It must **freeze the clock**, not pass a `today=` kwarg.
`period_spend_window_end` defaults `today` to `date.today()` internally (`billing_service.py:600`),
so a kernel that wrongly wires in the floored helper and does not forward `today` is *unaffected* by
a kwarg — the test would go green with the wrong helper installed. The fixture must also have
`effective_end < today` on the open row, or flooring is a no-op and the test passes against either
helper.

**⚠ Test 10's fixture is load-bearing.** On a clean contiguous roster
`end_date == successor.start - 1` by construction, so the wrong derivation and the right one agree
and the test proves nothing. That is exactly why revision 1's version failed to catch its own
structural defect. The fixture must contain rows where they differ.

**Endpoint — `tests/routers/test_billing_period_roster.py`**

| # | Test | Kind |
|---|---|---|
| 11 | Status partition: an `invalid` row, a `current_by_calendar` row on a lapsed roster, and an open row starting tomorrow each get the documented status | **fence** |
| 12 | `effective_end` and `counting_through` diverge on a lapsed roster, agree on a converged one | **fence** |
| 13 | Overlapping periods: one transaction appears in the count of **every** row containing it | **fence** — kills the `CASE` shape |
| 14 | Count is unfiltered (a transfer leg counts); settled net is filtered (it does not) | **fence** |
| 15 | A gap between the **out-of-window predecessor** and the first displayed row **is reported** | **fence** — the only assertion that fails if the one-row-older fetch is dropped |
| 16 | Future stubs render as `upcoming` (no upper bound) | **fence** |
| 17 | A maximally-lapsed org whose every row predates the lookback still returns rows (D8's floor) | **fence** |
| 18 | `months=0` and `months=999` are clamped, not rejected; `truncated` is true past `LIMIT 200` | **fence** |
| 19 | Non-admin → 403 | **fence** |
| 20 | The route creates **no** `BillingPeriod` on an org with no open row; period count unchanged; response reports `no_open` | **fence** — fails if anyone reaches for `get_current_period` |
| 21 | Org with zero periods → 200, `periods: []`, `no_open` | **fence** |
| 22 | `GET /billing-periods` response shape unchanged | guard (regression net; nothing here touches it) |

**Frontend**

| # | Test | Kind |
|---|---|---|
| 23 | Page renders under `SettingsLayout` with the Organization tab active | guard |
| 24 | Anomaly markers render; the overlap note shows when any row overlaps | **fence** |
| 25 | Both end columns render; the divergence is visually distinguished only when they differ | **fence** |
| 26 | A non-admin deep-linking the page is redirected, matching `settings/organization/page.tsx:106,128` | **fence** |

## 5. Rollout

Additive: one new GET, one new page. No existing endpoint, query or component changes, so nothing
that works today can move. Worst case the new page renders wrong numbers on a surface nobody
depended on yesterday.

**Known residual, recorded rather than discovered in review:** this page's numbers will disagree
with `/budgets` on lapsed orgs by construction — that is TBD-240 §7's accepted residual, and §2.1
is the deliberate decision to *show* it rather than hide it. It will be the first thing anyone
notices.

No release note needed (new surface). No flag: `routers/settings.py` carries no `require_feature`
and this is a diagnostic, not a product surface.

---

## 6. Out of scope

- **Repairing** anything the page reports — TBD-235.
- The `closed_at` column — TBD-233, now blocked by this ticket and possibly droppable.
- The frontend "current period" unification — TBD-242, which adopts D2's status.
- The bound-separation refactor at the three fallback sites — TBD-243.
- Making the open period selectable in the transactions filter (§0.3) — follow-up.
- `GET /billing-period`'s auto-create side effect (D10) — follow-up.

## 7. Sign-off record

**Architect sequencing round — 2 independent architects, unanimous, 2026-07-29.** Both chose
TBD-234 over TBD-233 and both ruled TBD-233 the wrong shape. Decisive argument, reached
independently: TBD-233 makes `end_date` always-populated, so a real end becomes indistinguishable
from a backfilled one and **the census this ticket exists to perform becomes impossible** — a
one-way door. Both also found that TBD-233 would silently revert TBD-240 (its
`if period.end_date is not None` branch becomes unconditionally true) with every TBD-240 test still
green.

**Sign-off round 1 — revision 1: REJECT / REJECT** (two independent reviewers, 2026-07-29). Nine
blocking findings, converging almost entirely. **No design ruling was overturned** — the two-ends
display, the server-canonical status, the anomaly-kernel-as-deliverable, admin gating and the
no-`get_current_period` rule all survived. What failed was the mechanics, and one of them badly.

1. **The derivation rule was structurally wrong and would have shipped a no-op** (both reviewers,
   independently). Revision 1 defined the derived end as `successor.start - 1` for *every* row;
   `period_effective_end` uses that only for the open row. Composed with the gap rule it reduced to
   `successor.start > successor.start` — **false always**. Gap and overlap detection would have been
   dead code on every roster, and tests 1, 2, 7 and 10 would have passed vacuously proving it.
   → §2.2 rewritten; D5 **reversed**.
2. **And the change that caused it was never needed.** `period_effective_end` returns before
   touching the DB for closed rows, so per-row calls cost ~1 extra query on a healthy roster, not N.
   Revision 1's N+1 justification was overstated. → D5 now calls the helper.
3. **The status partition was not disjoint**, and reviewer B walked a two-close sequence that
   commits an `end_date < start_date` row through shipped code. → ordered rules, `invalid` branch,
   fifth anomaly shape.
4. **`straddling` and `overlap` fire on the same row**, and test 8 asserted the opposite; the
   straddle boundary also drifted from the code's `>=`. → explicit precedence, boundary corrected.
5. **The clock-independence claim contradicted the `lapsed_open` marker.** → structural vs temporal
   marker sets; test 9 rewritten to freeze the clock (a `today=` kwarg cannot catch the failure it
   fences, because `period_spend_window_end` defaults internally).
6. **Test 15 could not fail** — no displayed row's end depends on the out-of-window predecessor. →
   re-targeted at the leading gap marker. The "one row newer" half contradicted D8; deleted.
7. **D7's mandated predicate defeated both indexes it named.** → per-column shapes; plain
   `settled_date BETWEEN` for settled net (justified by the `SETTLED ⇒ settled_date NOT NULL`
   invariant), `UNION ALL` or an explicitly-accepted scan for the count.
8. **`N ≤ 24` had no basis** once D8 forbade the only capped query. → `months` clamped 1..60,
   `LIMIT 200`, `truncated` in the response.
9. **D8 returned an empty roster on the most lapsed orgs** — the exact orgs the page exists for. →
   window floored with the newest 12 rows; `no_open`/`duplicate_open` computed org-wide.
10. **No response contract existed at all.** → §2.5.

Also folded: §0.1's "touches no period row" (the route still calls `get_current_period` for its
audit payload), §0.3 (the page *does* accept `date_from`/`date_to` URL params, so deep-linking
works — only the dropdown excludes the open row), §2.3's dashboard claim (a
`current_by_calendar` row is *none* of current/past/future there, not "past"), the sweep-script
descope now recorded, and every test labelled fence or guard.

**Sign-off round 2 — revision 2:** _(pending)_
