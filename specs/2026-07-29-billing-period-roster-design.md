# TBD-234 — read-only billing period roster, and the anomaly kernel

Status: REVISION 4 — awaiting sign-off round 4
Date: 2026-07-29
Jira: TBD-234 (Story, child of TBD-213) — **re-scoped to effort L and SPLIT in two.** **Blocks TBD-233 / TBD-242.**
Predecessors, all merged: TBD-232 (#586), TBD-239 (#587), TBD-241 (#588), TBD-240 (#589)
Related specs: `2026-07-28-open-period-spend-window-design.md`, `2026-07-28-close-period-chain-close-design.md`, `2026-07-28-billing-period-boundary-integrity.md`, `2026-07-27-billing-period-truth-and-safety.md`

**This one document covers two tickets.** Round 2 sign-off ruled the work **L, not M**, and ruled it
must split:

- **TBD-234a — the kernel.** `load_complete_roster`, `find_period_anomalies` and the §2.3 status
  partition as a `period_status` helper, all in `billing_service.py`. Tests 1-14. Ships FIRST.
- **TBD-234b — the route and the page.** Fetching, aggregates, display windowing, gating, the
  response contract, the page. Tests 15-31. Opens only AFTER 234a merges.

Every deliverable below is labelled **[234a]** or **[234b]**. §8 records the split and freezes the
kernel contract that 234b consumes.

Revision 1 was **REJECTED 2-0** on nine blocking findings. Revision 2 was **REJECTED 2-0** again on
four more. Revision 3 was **REJECTED 2-0** a third time, and this time the frozen kernel contract
itself was found **unbuildable** by both reviewers independently. Full record in §7.

**Revision 4 is a subtraction, ruled unanimously by two fresh architects.**

> **Subtract the WINDOW from the kernel's domain, not the PURITY from its signature.**

**The root cause, stated plainly: the kernel's input domain was defined by the display window rather
than by the invariant being checked.** Every property the kernel tests (contiguity, non-overlap,
exactly-one-open, straddling, lapsed) is a property of a WHOLE roster. A windowed sample of a roster
is not a roster and does not carry those properties. Revisions 1 through 3 fused a display concern
(which rows fit on a page) with an analysis domain (which rows the invariant quantifies over), and
every round-3 finding fell out of that fusion: the out-of-window predecessor, the newest-12 floor,
the open-row union, the `open_row_ids` carve-out, and finally the contract that could not be built.

Roughly a fifth of revision 3 is deleted rather than corrected. See §7 for what went and why.

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
| `ensure_future_periods`' "known hole: blind to a SECOND open row" | `billing_service.py:227-232` |
| `get_current_period` logs `multiple open billing periods` and nothing else | `billing_service.py:84-92` |
| A skipped stub candidate leaves a **gap**, logged as `billing.stub.skipped_overlap` | `billing_service.py:246-259` |

Today the fleet's roster health is observable only by grepping structlog. And #589 shipped
`period_effective_end` **with no production caller**, its docstring naming this ticket as its only
consumer and telling readers not to prune it. ⚠ Under revision 4's subtraction that docstring is now
**wrong**, and correcting it is a 234a deliverable; see §2.2.

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
unreachable **via the dropdown** for the row users will click first. The page *does* accept
`date_from`/`date_to` URL params (`:301-311`, `DATE_PARAM_RE` at `:49`), so a roster row can
deep-link the open period's window directly, which is how this page should link out. Making the open
period selectable in the dropdown is a recorded follow-up.

⚠ **The deep link must choose an end.** §2.1's whole point is that `effective_end` and
`counting_through` differ on a lapsed org. **Ruling: link the `counting_through` window.** That is
the window `/budgets` bills against, and the click-through exists so a user can reconcile against
**what they were shown**, not against a derived bound no other surface uses.

**Known side effect, recorded not discovered:** the target writes the parsed params into
`persistedFilters` and clears `filterPeriod` (`:301-311`), and that store persists to localStorage.
So following a roster deep link **mutates the user's saved transaction filters**. Acceptable for a
diagnostic click-through, but the page copy should not pretend the link is read-only.

**0.4 — `SettingsLayout` highlighting: right answer, wrong mechanism.** `activeTab === tab.href` is a
plain string comparison (`frontend/components/SettingsLayout.tsx:53-54`), but **`activeTab` is a
caller-supplied prop** (`:18`), not `usePathname()`. Nothing anywhere compares
`/settings/organization/periods` against anything. The tab highlights **only because §1 mandates the
page pass the literal string `/settings/organization`.** The mis-description in revision 2 invited an
implementer to "correct" it by passing the real pathname, which silently un-highlights **every** tab.
Test 27 is a `fence` accordingly.

**0.5 — Verified correct:** `effective_period_date_expr()` is `coalesce(settled_date, date)`
(`transaction_filters.py:132`) and is not sargable; `list_transactions` applies no
`reportable_transaction_filter`, so the unfiltered-count ruling holds; `use-billing-periods.ts`
shares one SWR key and the cold-mount guard is real; `/settings/organization`'s tab is
`minRole: "admin"` (`frontend/components/SettingsLayout.tsx:13`).

⚠ **Line-number correction, revisions 2 and 3 both had it wrong:** `SettingsLayout` renders the
route's single `<h1>Settings</h1>` at **`:46`** (and again in its loading branch at `:24`), not
`:47`. See §1.1 for the heading-level ruling that depends on it.

---

## 1. Scope

One new read-only endpoint, one anomaly kernel, one page, across two tickets.

**[234a]**
- **`load_complete_roster`** in `billing_service.py` — the kernel's ONLY constructor (§2.2).
- **`find_period_anomalies`** in `billing_service.py` — **the named backend deliverable**, not an
  implied UI feature. Both merged specs already assign it here
  (`2026-07-28-billing-period-boundary-integrity.md:298`). Testable without the route.
- **`period_status`** in `billing_service.py` — the §2.3 partition, as a pure helper.
- The `PeriodAnomaly` type and its marker payload schema (§2.5), the kernel's output contract.
- **The `period_effective_end` docstring correction** (§2.2, residual R1).

**[234b]**
- **`GET /api/v1/settings/billing-periods/roster`** — new, admin-gated (D9).
- Display windowing (D6/D7/D8), the per-row aggregates, and the full §2.5 response body.
- **`/settings/organization/periods`** — a page rendering `SettingsLayout` with
  `activeTab="/settings/organization"` (the literal parent route string; see §0.4).

**Do NOT widen `GET /billing-periods`.** It is on the cold-mount critical path for transactions,
dashboard and forecast-plans via one shared SWR key (`frontend/lib/hooks/use-billing-periods.ts`),
guarded by `frontend/tests/app/transactions-cold-mount-single-fetch.test.tsx`.

**Descope recorded:** `2026-07-28-billing-period-boundary-integrity.md:298` assigns TBD-234 an
anomaly kernel, a **sweep script**, and the roster page. The sweep script is deliberately **not** in
either ticket — a per-org admin page answers "is *this* org healthy", not "how many orgs are
affected", which is §0.1's actual complaint. It needs operator-authorized prod DB access and is a
different deliverable. **Mint it as a follow-up** rather than letting it disappear; the kernel 234a
ships is what it will call, and under revision 4 it calls it with no window vocabulary at all.

### 1.1 Layout intent [234b]

§2.5's payload maps 1:1 onto a nine-column table, and that is the shape an implementer will reach for
by default. **It is forbidden.** `DESIGN.md` names it as an explicit anti-reference ("if a screen
reads like Google Sheets in a wrapper, redesign before shipping") and it violates `PRODUCT.md`'s
*hierarchy-without-grids*.

**Ruling: 234b owes a design pass and must not ship a raw grid.** The intent, not designed in detail
here: period rows read as a **timeline** with their anomaly markers **inline on the row they
concern**, grouped in `card` surfaces.

⚠ **Inline-on-the-row has no story for off-window markers, and under org-wide analysis those now
exist.** A marker can name a period id the display window does not show (§2.5's
`referenced_periods`). **Ruling: a summary band sits above the timeline** ("3 issues elsewhere in
your roster"), rendering the referenced periods for every marker carrying `off_window: true`. This is
a small additive requirement on the design pass, not a redesign.

**Heading level, ruled explicitly so the design pass cannot add a second `<h1>`:** `SettingsLayout`
already renders the route's only `<h1>` (`frontend/components/SettingsLayout.tsx:46`). The roster
card's heading is an **`<h2>`**, and the summary band's is an `<h2>` sibling, never an `<h1>`.

**Token rulings, so the design pass cannot trip CI:**

- §2.1's "visually differentiated when they diverge" named no token in revision 2. The natural reach
  is `text-accent`, which breaks **The One Brass Rule** on the first roster carrying three lapsed
  rows. **Use `badgeWarning`** (`bg-warning-dim text-warning`, `frontend/lib/styles.ts:69-70`) **plus a
  text label**, which also satisfies "don't rely on color alone".
- There are **eight marker kinds and five badge variants**, and `badgeSuccess` is inappropriate for
  an anomaly. **Ruling: the kind rides the text and icon; the color carries severity only.** Without
  this an implementer invents a hue and trips `frontend/scripts/check-design-tokens.sh`.

No migration. No schema change. No write path. ⚠ **"Nothing existing is edited" was literally false
and is reworded: no existing behaviour changes.** 234a does edit `billing_service.py` (three new
helpers) and `period_effective_end`'s docstring (residual R1).

---

## 2. The shape of the data

### 2.1 Two ends, both shown [234b]

This is the design's centre of gravity and the ticket does not mention it.

On a lapsed org `period_effective_end` returns a date **in the past** — `ensure_future_periods`
anchors stubs on the open period's `start_date`, not on today (`billing_service.py:171`, docstring
`:159-164`) — while `period_spend_window_end` floors at today (`:600-601`). So a naive roster
renders an "Open" period that *ended in May* while `/budgets` bills spend through July.

**Ruling: render both, explicitly labelled.** A "Period ends" column (derived) and a "Counting
through" column (spend window), with the two visually differentiated only when they diverge, via
`badgeWarning` plus a text label (§1.1). Ship one number and this page becomes the one that proves
the app is lying; ship both and the divergence *is* the diagnostic. This is a deliberately-accepted
residual of TBD-240 §7, surfaced rather than introduced.

### 2.2 The complete roster, and the derived end [234a]

⚠ **Revision 1 got the derivation wrong, and the error was structural.** It ruled
`successor.start_date - 1 day` **for every row**, but `period_effective_end` returns `end_date`
**verbatim** for closed rows (`billing_service.py:509-510`) and uses the successor only for the
*open* row. Composed with §2.4's gap rule that made `row.end ≡ successor.start - 1` identically, so
`successor.start > row.end + 1` reduced to `successor.start > successor.start`, **false for every row
on every roster. The gap and overlap detectors would have been dead code.**

⚠ **Revision 2 drew the WRONG lesson from that, and it cost two further rounds.** It read the defect
as "in-memory derivation is unsafe" and converted it into an **IO mandate** (D5: "call
`period_effective_end` per row"). Revision 3 then froze purity into the kernel signature, and a pure
kernel that must perform IO per row is unbuildable, which is what both round-3 reviewers found.

**The defect class, named correctly: "a derivation that does not case-split on `end_date IS NULL`".
Its fence is a DIFFERENTIAL TEST against the helper (test 11), never an IO mandate.** Revision 1's
formula was wrong because it applied the open-row rule to closed rows, not because it ran in memory.

#### The two functions

```python
async def load_complete_roster(db, org_id) -> CompleteRoster:   # the ONLY constructor
    ...  # SELECT id, start_date, end_date WHERE org_id = ? ORDER BY start_date
         # no LIMIT, no date predicate, no branches

def find_period_anomalies(roster: CompleteRoster, *, today: datetime.date) -> list[PeriodAnomaly]:
    ...  # pure, sync, no DB
```

#### The derivation, and why it is a PROOF rather than a hope

`BillingPeriod` carries `UniqueConstraint("org_id", "start_date", name="uq_billing_period_org_start")`
(`backend/app/models/billing.py:12-14`, verified). For a **complete** roster in `start_date` ASC
order with per-org unique starts:

```
effective_end(rows, i) = rows[i].end_date                     if end_date IS NOT NULL
                       = rows[i+1].start_date - 1 day         if open and i+1 exists
                       = None                                 if open and i is the tail
```

- **closed row** → `end_date` verbatim. Identical to `billing_service.py:509-510`.
- **open row with `i+1` present** → `rows[i+1].start_date - 1 day`. Because starts are unique per org
  and the list is complete, `rows[i+1].start_date` **is** `MIN(start_date) WHERE start_date >
  rows[i].start_date`, which is exactly what `_next_period_start` computes
  (`billing_service.py:425-431`, verified). Identical to `:511-514`.
- **open tail** → `None`. Identical to `:512-513`.

This is an **equality, not an approximation**, and it holds **only** under the completeness
precondition. **That precondition replaces the window in the contract.**

#### Enforcement — the load-bearing half, shipped in the SAME PR as the kernel

**A pure kernel can never verify completeness itself.** Completeness is a claim about rows that are
NOT in the list. Purity and self-verification are therefore mutually exclusive: no in-kernel
assertion, length check or invariant guard can work, and anyone proposing one has misunderstood the
shape of the problem.

**Ruling: enforce at CONSTRUCTION.** `CompleteRoster` is a **nominal type whose only constructor is
`load_complete_roster`**. `find_period_anomalies` accepts `CompleteRoster` and nothing else, so
handing it a windowed `list[BillingPeriod]` is a **type error**, not a silent wrong answer.
Enforcement collapses to **one construction site**, auditable by grep and by the type checker.

**This is NOT the rejected two-unit fix in disguise.** `load_complete_roster` **fetches rows**; it
does not **compute ends**. `period_spend_window_end` remains **structurally unreachable everywhere in
234a**, so the prohibition-by-construction property that #589 bought is fully intact.

Three amendments, all normative:

1. **`CompleteRoster` holds row tuples, not ORM entities.** `RosterRow = (id, start_date, end_date)`,
   decoupled from the model, so an unbounded fetch on a 10k-row org is trivial. Test 11 alone needs
   real `BillingPeriod` instances and may hold both.
2. **Belt [234b]:** a contract test asserting the route's roster length equals
   `SELECT COUNT(*) WHERE org_id = ?` (test 22). The type is the braces; this test is the belt.
3. **Residual R1, must not be missed [234a]:** `period_effective_end`'s docstring
   (`billing_service.py:483-490`, verified) currently says TBD-234 is its consumer and "Do not prune
   this as dead code". Under the subtraction **234a does NOT call it** (only test 11 does; 234b
   reaches it transitively via `period_spend_window_end`). **Update that docstring in 234a**, naming
   test 11 and 234b's spend-window path as what keeps it alive, or the next reader prunes the helper
   that test 11's entire fence is differential against.

**Consequence worth stating, because it looks like a bug and is not:** the open row can never
produce a gap or overlap against its *immediate* successor. Its end is *defined* by that successor,
so they abut by construction. (It can still overlap a **non-adjacent** row; see §2.4's all-pairs
ruling.) That is why the healthy shape `[…closed…, OPEN, stub, stub]` yields no markers.

#### Index note, stated rather than discovered

`ix_billing_periods_org` is `(org_id)` only (`014_billing_periods.py`, verified: a single-column
`create_index`), with no `start_date` component. `load_complete_roster`'s `ORDER BY start_date`
therefore **filesorts**. On a roster of hundreds of narrow rows that is cheap and no index change is
proposed here; it is recorded so a later reader does not treat it as an oversight.

### 2.3 The status partition [234a]

Four divergent definitions of "current" already exist in the frontend; the roster must not become a
fifth. **The server computes `status` and it becomes canonical** — the field TBD-242 will later
point every frontend site at. It ships in 234a as a `period_status` helper in `billing_service.py`,
so it is testable and frozen before any route consumes it. It must be a **partition** (every row
gets exactly one), and it must name the shape the frontend disagrees about rather than folding it
into `past` or `current`:

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

`today` is a **required argument**, resolved once by the caller (D8a). Never `date.today()` inside
the helper.

**⚠ `invalid` is UNREACHABLE through shipped code. Revision 2's justification was false; the branch
stays anyway, as a defensive one.** `close_period` rejects `requested < current.start_date` at
`billing_service.py:1034-1039`, **after** taking the row lock, so the two-close sequence revision 2
cited 400s rather than committing an inverted row. Every `BillingPeriod.end_date` writer was then
enumerated and each is provably non-inverting:

| Writer | Why it cannot invert |
|---|---|
| `routers/settings.py:436-439` (`POST /billing-period`) | `BillingPeriodCreate` has a `model_validator` rejecting `end_date < start_date` (`schemas/settings.py:32-35`) |
| `billing_service.py:261` (stub insert) | stub end is `snap(next_start + 1mo) − 1`, strictly `> next_start` (`:175`) |
| `billing_service.py:866` (`_apply_close_step`) | `resolved` is either `requested` (≥ `start_date` by the `:1034` guard) or `s0 − 1` where `s0 > current.start_date` strictly |
| `billing_service.py:104`, `:888`, `:902` | write `end_date` NULL |

**Ruling: keep branch 1, rewrite the justification as defensive.** `models/billing.py:12-14` carries
no CHECK constraint, so the database **will accept** an inverted row written by a direct DB edit,
by operator prod access (`reference_prod_db_readonly_access.md` describes the shell that reaches it),
or by a future writer that skips the schema layer. A diagnostic page whose subject is data corruption
must classify corruption it did not cause. Without branch 1 such a row matches **both** `upcoming`
and `past`, which breaks the partition. It is also a **fifth anomaly shape** (§2.4). Test 10 inserts
the row directly, so it is not vacuous.

`current_by_calendar` is the disputed shape. On the dashboard such a row is *neither* current, past,
nor future: `dashboard/page.tsx:271-273` computes `isCurrent = end_date === null` (false),
`isPast = end_date < today` (false, it contains today), `isFuture = start_date > today` (false), and
it falls through all three. On Forecasts it is *Current*
(`frontend/app/forecast-plans/ForecastPlansClient.tsx:253-256`, calendar containment). Naming it
explicitly is what lets TBD-242 resolve this against a tested definition instead of inventing a fifth.

**What `status` does NOT do:** it classifies rows, it does not *select* one. On a lapsed roster the
open row is `open` while a stub is `current_by_calendar`; on a corrupt roster two rows can both be
`current_by_calendar`. Choosing which row a screen should display remains TBD-242's problem, and
this spec deliberately does not pre-empt it.

### 2.4 The anomaly kernel [234a]

**The kernel quantifies over the COMPLETE roster (§2.2). There is no window here and no window
vocabulary in this section.**

**Four shapes, not two.** The ticket names gaps and overlaps; the code defers two more here. Both
pair rules are written in terms of `effective_end(rows, i)`, never the literal `end_date` column: on
an open row the column is `None` and `None + timedelta` raises `TypeError`.

1. **gap** — **ADJACENT-PAIR.** For each consecutive pair `(rows[i], rows[i+1])`:
   `rows[i+1].start_date > effective_end(rows, i) + 1 day`. An all-pairs gap rule is meaningless
   (every non-neighbour pair has something between them), so the domain is normative: **gap is
   evaluated on adjacent pairs only.**
2. **overlap** — **ALL-PAIRS.** For every `i < j`: `rows[j].start_date <= effective_end(rows, i)`.
   **The domain is normative.**
3. **duplicate open rows** — more than one `end_date IS NULL`. **The most damaging shape**: every
   frontend `findIndex(p => p.end_date === null)` silently picks the first, so two rows both claim
   "current" and different screens can pick differently. Detected-and-only-logged at
   `billing_service.py:84-92`, a known hole at `:227-232`, reachable through `POST /billing-period`.
4. **no open row** — zero rows with `end_date IS NULL`. Every consumer calls `get_current_period`,
   which would *auto-create and commit* one.
5. **inverted row** — `end_date < start_date` (§2.3 branch 1). Revision 1 omitted it entirely.

Plus one informational marker: **straddling row**, see the anchoring ruling below.

⚠ **`no_open` and `duplicate_open` are computed from the complete roster like every other marker.**
Revision 3 passed them in as a separate `open_row_ids` argument, an org-wide carve-out that existed
only because the rest of the kernel was windowed. **That carve-out is deleted: org-wide is now the
general rule.**

#### ⚠ Why overlap is ALL-PAIRS (revision 2's rule was adjacent-pair and under-reported)

Revision 2 phrased both rules against "successor", which is `_next_period_start`'s **immediate** next
row (`billing_service.py:425-431`). That makes the overlap rule adjacent-pair, and adjacent-pair
overlap detection is unsound.

**Attack vector.** Roster `A [2026-01-01 → 2026-12-31]`, `B [2026-02-01 → 2026-02-28]`,
`C [2026-03-01 → 2026-03-31]`, all closed. Adjacent pairs give `(A,B)` → one overlap, and `(B,C)` →
contiguous, no marker. **`A` overlaps `C` completely and no marker is emitted.** The roster reports
one overlap where there are two, and renders `C` clean. This is the nested-containment corruption
class that `routers/settings.py:417-421`'s TOCTOU hole admits, on the page that exists to find it.

**Second consequence: adjacent-pair semantics falsify the precedence ruling below.** On
`A [Jan 1 → Jun 30]`, `B [Feb 1 → Feb 28]`, `OPEN [Mar 1, NULL)`, `A` straddles `OPEN`, but `A`'s
successor is `B`, so no overlap ever fires for A↔OPEN. **Cost:** all-pairs is the only `O(n²)` rule,
and §2.4a's cap is set where that cost stops being free.

#### ⚠ The `straddling` anchor (undefined in revision 2 for zero or multiple open rows)

**Attack vector.** Roster `C = [2022-01-01, 2022-05-31]` closed, `A = [2022-03-01, NULL)` open,
`B = [2026-07-01, NULL)` open. Anchoring on the **first** open row by start gives
`C.start <= A.start` and `C.end >= A.start` → straddling emitted. Anchoring on `get_current_period`'s
choice (`ORDER BY start_date DESC` → `B`) gives `C.end >= 2026-07-01` false → **not** emitted. Same
input, two spec-conformant responses, on exactly the duplicate-open roster this page targets.
Separately, with **zero** open rows `open.start_date` is undefined and the naive implementation
raises `AttributeError`, so the page **500s on the exact org it exists for**.

**Ruling: `straddling` is anchored on the open row `get_current_period` would select, that is the
row with MAX `start_date` among rows where `end_date IS NULL`.** That is the row `_apply_close_step`
will actually evaluate, so the marker predicts real behaviour rather than a hypothetical. **When
there are zero open rows the marker is not computed at all** — `no_open` already carries that signal
and there is nothing to straddle. Under revision 4 the anchor is drawn from the complete roster, so
it is unambiguously present whenever any open row exists.

The predicate, with the anchor pinned:

```
straddling(rows, i) = rows[i].start_date <= anchor.start_date
                      AND effective_end(rows, i) >= anchor.start_date
```

⚠ **`>=`, at or after — matching `_apply_close_step`'s own predicate at `billing_service.py:776-777`.**
Revision 1 said "ends after it", which would silently under-report exactly the shape whose deferral
(`:914-922`) created this marker.

**Marker precedence, normative.** A straddling row is by construction also an overlap under rule 2,
now true by construction because rule 2 is all-pairs. **Ruling: a row may carry multiple markers, and
`straddling` is emitted *in addition to* `overlap`, not instead of it.** Suppressing the overlap
would hide genuine overlaps on any roster containing a straddler, precisely the rosters this page
targets. Test 9's fixture pins a **non-adjacent** straddler with a normative marker-id assertion,
so the ruling is fenced against a regression to adjacent-pair semantics.

#### Two output sets, because one of these is not clock-free

| set | markers | clock |
|---|---|---|
| **structural** | gap, overlap, duplicate_open, no_open, inverted, straddling | none |
| **temporal** | lapsed_open (derived end in the past) | reads the injected `today` |

Revision 1 put `lapsed_open` in one undifferentiated set and then asserted the whole set was
clock-independent, a direct contradiction, since "in the past" is a comparison against `today`.
`lapsed_open` is computed on the **anchored** open row, the same anchor `straddling` uses.

**⚠ The kernel's derived end is `period_effective_end`'s semantics, never
`period_spend_window_end`'s.** This is the entire reason #589 split one helper into two (its §2.1):
a clock-dependent end paints phantom overlaps between the open row's floored window and the historic
stubs on *every* lapsed org, verbatim the failure
`reference_billing_period_boundary_model.md` exists to prevent. Under revision 4 the wrong helper is
**structurally unreachable** from the kernel (it is `async`; the kernel is sync and takes no session),
so this is enforced by construction rather than by a test. Test 16 fences the route, where both
helpers *are* reachable.

#### Row-level suppression rules, both normative

**Tail rows.** `effective_end` is `None` for the roster tail. ⚠ **Revision 3 wrote "a row whose
`effective_end` is `None` participates in no pair, on either side." That was a NEW defect.** Both
pair rules only ever read the **LEFT** row's end, so excluding a row as the RIGHT member is lossy: it
suppresses real gaps and real overlaps measured against a tail open row.

**Ruling: a row whose `effective_end` is `None` is never the LEFT member of a pair; it may be the
RIGHT member of either rule.** With that, the intended shape
`[…closed…, OPEN(end=NULL), stub, stub, stub]` still yields no markers, and a genuine gap ending at
the tail row is still reported.

**Invalid rows.** An `invalid` row (`end < start`) makes the adjacent-pair gap rule fire spuriously
on **both** sides: its end precedes its own start, so the pair to its left overlaps and the pair to
its right gaps, neither of which describes anything a reader can act on. **Ruling: `gap` and
`overlap` are SUPPRESSED on `invalid` rows, as either member. `inverted` carries the signal.**

### 2.4a — The analysis cap [234a]

> **Named rule: truncation for DISPLAY is legitimate; truncation for ANALYSIS must be refused, never
> silently applied.**

- **Display truncates** (D6): newest-first, `LIMIT 200`, `truncated: true`. Unchanged.
- **Analysis refuses.** But scope the refusal correctly. **Only `overlap` is `O(n²)`.** `gap` is
  adjacent-pair `O(n)`; `straddling` is `O(n·k)` in open rows; `inverted`, `no_open`,
  `duplicate_open` are `O(n)`.

**Ruling: past the cap, suppress `overlap` ALONE** and emit an **`overlap_analysis_skipped`** marker
carrying the row count and the cap. Suppressing every structural marker would kill `duplicate_open`
on 1000+ row orgs, which is precisely where that corruption hides.

**Cap = 2000 rows.** 2M pair comparisons is sub-second in Python; the real cliff is around 5000. At
2000 the refusal path should never fire in practice, which is what a refusal path should look like.

⚠ **Never return an empty `anomalies` list when analysis was skipped.** The skipped marker is itself
an anomaly, and `roster.analyzed` (§2.5) reports the same fact at the scope level.

### 2.5 Response contract

**The `PeriodAnomaly` type and its marker payload schema are [234a]** — the kernel's output contract,
which 234b consumes verbatim. **Everything else in this section is [234b].**

#### The kernel's types [234a]

⚠ **Named `PeriodAnomaly`, NOT `Anomaly`.** `backend/app/schemas/ai_forecast.py:38` already owns
`AnomalyFlag`, with `anomalies` fields at `:59` and `:106`, in an unrelated AI-forecast sense
(verified). A bare `Anomaly` in `billing_service.py` would collide in every reader's head and in
every grep.

**Shape: a frozen dataclass with a `kind: Literal[...]` tag and optional fields.** That matches the
repo's service-layer convention (verified: `cc_cycle_service.py:31`, `budget_rebalance_service.py:112`,
`loan_service.py:103` all return `@dataclass(frozen=True)`, and there are **no** `NamedTuple` or
`TypedDict` declarations anywhere in `backend/app/services/`).

A **discriminated Pydantic union** is the house pattern only at the **wire boundary**
(`backend/app/schemas/dashboard.py` uses `Literal` tags at `:96-141` plus `Field(discriminator="type")`
at `:173`). That is **234b's response model**, built over the kernel's dataclasses without the kernel
importing Pydantic at all.

```python
@dataclass(frozen=True)
class RosterRow:
    id: int
    start_date: datetime.date
    end_date: datetime.date | None

@dataclass(frozen=True)
class CompleteRoster:
    org_id: int
    rows: tuple[RosterRow, ...]      # start_date ASC; EVERY row the org has

@dataclass(frozen=True)
class PeriodAnomaly:
    kind: Literal["gap", "overlap", "duplicate_open", "no_open", "inverted",
                  "straddling", "lapsed_open", "overlap_analysis_skipped"]
    # populated per kind, per the table below
    from_period_id: int | None = None
    to_period_id: int | None = None
    period_id: int | None = None
    period_ids: tuple[int, ...] | None = None
    anchor_period_id: int | None = None
    from_date: datetime.date | None = None
    to_date: datetime.date | None = None
    effective_end: datetime.date | None = None
    period_count: int | None = None
    cap: int | None = None
```

#### Marker payloads, one row per kind, all eight, frozen in 234a

Revision 3 exemplified three of them and left the rest to be invented at render time. The schema is
frozen in 234a, so it cannot be left to 234b.

| kind | fields | semantics |
|---|---|---|
| `gap` | `from_period_id`, `to_period_id`, `from_date`, `to_date` | ⚠ **pinned:** `from_date = effective_end(rows, i) + 1 day`, `to_date = rows[i+1].start_date − 1 day`. The uncovered interval itself, both bounds inclusive |
| `overlap` | `from_period_id`, `to_period_id`, `from_date`, `to_date` | ⚠ **pinned:** `from_date = rows[j].start_date`, `to_date = effective_end(rows, i)`, **the LEFT row's end**. Not `min(effective_end(i), effective_end(j))`, not the intersection |
| `duplicate_open` | `period_ids` | every open row's id, `start_date` ASC. **Ids, never a count** |
| `no_open` | `period_ids` | always `[]`; the field is present for schema uniformity |
| `inverted` | `period_id` | §2.3 branch 1 |
| `straddling` | `period_id`, `anchor_period_id` | the straddler and the MAX-start open row it straddles |
| `lapsed_open` | `period_id`, `effective_end` | the anchored open row and its derived end, which is `< today` |
| `overlap_analysis_skipped` | `period_count`, `cap` | §2.4a; `period_count` is the org's true row count, `cap` is 2000 |

#### The response body [234b]

```jsonc
{
  "roster":  { "period_count": 37, "first_start": "2023-01-01",
               "last_start": "2026-09-01", "analyzed": true },
  "window":  { "from": "2025-08-01", "to": null, "displayed_count": 12, "truncated": false },
  "periods": [
    {
      "id": 41,
      "start_date": "2026-07-25",
      "end_date": null,                  // raw column, null for the open row
      "effective_end": "2026-08-24",     // derived; null only for the roster tail
      "counting_through": "2026-08-24",  // period_spend_window_end; null only for the tail
      "status": "open",                  // open | upcoming | current_by_calendar | past | invalid
      "length_days": 31,                 // null when effective_end is null OR status is "invalid"
      "transaction_count": 42,           // unfiltered (D7)
      "settled_net": "-1240.55"          // string, per the repo's Decimal convention
    }
  ],
  "anomalies": [
    { "kind": "gap", "from_period_id": 40, "to_period_id": 41,
      "from_date": "2026-07-01", "to_date": "2026-07-24", "off_window": false },
    { "kind": "overlap", "from_period_id": 12, "to_period_id": 17,
      "from_date": "2023-04-01", "to_date": "2023-09-30", "off_window": true }
  ],
  "referenced_periods": {              // one entry per id ANY marker names
    "12": { "id": 12, "start_date": "2023-01-01", "end_date": "2023-09-30",
            "effective_end": "2023-09-30", "status": "past" }
  }
}
```

**⚠ Two scopes, never conflated. This is the response's central contract.**

| scope | meaning | fields |
|---|---|---|
| `roster` | **org-wide**, the anomaly domain | `period_count`, `first_start`, `last_start`, `analyzed` |
| `window` | **display only** | `from` (min displayed `start_date`), `to` (null, no upper bound), `displayed_count`, `truncated` |

**`referenced_periods` is required, not optional.** With org-wide analysis a marker can name a period
id the displayed page does not carry. Every id any marker references appears here, keyed by id as a
string. **`effective_end` is mandatory on each entry**, or the page cannot render an off-window open
row's gap bounds without recomputing what the kernel already knew. **`off_window` is emitted per
marker**, true when any id it references is absent from `periods`; a client could derive it by
set-difference, and the field exists so it does not have to.

**Copy that states the guarantee**, and it must appear on the page: *"Checks cover your entire
roster. The timeline below shows the last N months."*

⚠ **Recorded upgrade.** Revision 3's D8 carried a bolted-on caveat, "the response states the window
bounds so a marker's absence is never mistaken for health", which was an admission the page could not
be trusted. **It is deleted.** Org-wide analysis lets the page make the **strong** claim (*absence of
markers means the roster is healthy*) where the windowed design could only make a weak one. That
upgrade, not the caveat, is what the subtraction bought.

Remaining rules:

- `status` values are the snake_case literals above. `invalid` is branch 1 of §2.3.
- **Ordering: `start_date` ASC** in `periods`. ⚠ This is the **response** ordering, not the query's;
  see D6 on truncation direction. `list_periods`' DESC ordering is for a different consumer.
- **`length_days` is `null` on an `invalid` row**, where `effective_end − start_date + 1` is negative;
  the `invalid` status carries the signal.
- `settled_net` serializes as a **string** (`specs/tech-debt-frontend-decimal-typing.md`). Clients
  must tolerate unknown `kind` values.
- **Empty roster** (org with zero periods): `200`, `periods: []`, `referenced_periods: {}`, and
  `anomalies: [{"kind": "no_open", "period_ids": []}]`. D10 forbids manufacturing a row to avoid this.
- **`months` out of range is clamped, not rejected.** ⚠ Revision 2's justification ("a diagnostic page
  should not 422 on a URL typo") is unachievable: FastAPI coerces `months: int`, so `?months=abc`
  422s before any handler code runs. **Drop the claim, keep the clamp**, stated precisely as
  *out-of-range integers are clamped*. A non-integer 422 is correct and this spec does not fight it.

---

## 3. Decisions

**D1 [234b] — New endpoint, not a widened one.** §1. `GET /settings/billing-periods/roster`.

**D2 [234a] — Server-computed `status`, canonical, a partition.** §2.3. Ships as `period_status`.

**D3 [234b] — Both ends rendered, labelled.** §2.1, tokens per §1.1.

**D4 [234a] — The anomaly kernel is the named backend deliverable**, lives in `billing_service.py`
beside the two helpers, is testable without the route, and is **pure and sync over a
`CompleteRoster`**. §2.4. Its signature and the `CompleteRoster` type are frozen in §8. Fenced in
234b by test 16.

**D5 [234a] — Ends are derived IN-MEMORY from the complete roster, and the equivalence is proved in
§2.2.** ⚠ **This REVERSES revision 2's D5 IO mandate, which is deleted.** The mandate turned a
formula bug into an IO requirement, and an IO requirement is what made revision 3's pure-kernel
contract unbuildable. **The fence is the differential test (test 11), not a mandate.** The
completeness precondition is enforced by the `CompleteRoster` construction site (§2.2), which is the
only place the rows can come from.

**D6 [234b] — Per-row bounded aggregates, over a hard-capped N. Reject the single grouped `CASE`
query outright.** The ticket correctly warns that a `CASE` returns the first match, and then still
frames the work as one grouped query. On the page whose *purpose* is exposing overlaps, a transaction
must be attributable to **every** period that contains it. Overlapped transactions appear in more
than one row's count, and the UI carries an explicit note that counts may exceed the org total where
periods overlap. **Columns that sum to the org total would be a lie on precisely the corrupt rosters
this page targets.**

⚠ **N must be capped explicitly.** `POST /billing-period` accepts arbitrary starts, so an org can
hold hundreds of rows. Ruling: **`months` is clamped to 1..60** (house pattern at
`routers/settings.py:467`), and the **display** query carries **`LIMIT 200`**, reported as
`window.truncated`.

⚠ **The cap's truncation DIRECTION is normative: the cap selects the NEWEST rows.** The naive
composition of `LIMIT 200` with §2.5's ASC ordering is `ORDER BY start_date ASC LIMIT 200`, returning
the **oldest** 200 and discarding the open row, every stub and every recent boundary. The query is
`ORDER BY start_date DESC LIMIT 200`, re-sorted ASC for the body; `window.from` reports the
**truncated** lower bound, not the requested lookback bound.

⚠ **Truncation is now display-only and cannot hide an anomaly.** Under revision 3 this cap was also
the analysis domain, which is why its direction was round 2's only unanimous blocker. Under revision
4 the anomalies come from `load_complete_roster`, so a truncated page still reports every marker,
with the off-window ones in `referenced_periods`. The direction ruling is kept because a timeline
starting five years ago is still a bad page, not because correctness depends on it.

**Query budget, accepted not hidden.** At `LIMIT 200` with two aggregates per row the worst case is
**~400 round trips**, plus one `load_complete_roster` fetch. ⚠ DO App Platform applies a request
timeout, so an org near the cap hits it first; the fix is the alternative below, not a raised cap.
**Recorded alternative, legitimate but not mandated:** a single
`JOIN billing_periods ON <bucketing date> BETWEEN start_date AND effective_end` emits one row per
(period, transaction) pair, **natively satisfying D6's every-containing-period requirement** in one
query rather than 400, so it is not the rejected `CASE` shape. It **must be measured before adoption**
(the join predicate is not sargable against either named index) and must reproduce the pinned numbers.

**D7 [234b] — Two columns, two filters, two *different* predicate shapes.** ⚠ Revision 1 named the
right indexes and then mandated one predicate shape for both columns, which **defeats the very index
it claims each column uses**. Both reviewers caught it. Corrected:

- **Settled net — `reportable_transaction_filter()` + `status = SETTLED` + plain
  `settled_date BETWEEN a AND b`.** No `OR`, no `coalesce`. That is a clean three-column range on
  `ix_transactions_org_settled_date` = `(org_id, status, settled_date)`. The `settled_date IS NULL`
  disjunct revision 1 mandated is **dead code here**, and adding it removes the range from the
  trailing key part, collapsing the plan to `(org_id, status)`.

  ⚠ **Both round-2 reviewers filed a non-blocking correction saying "there is no DB CHECK; the
  invariant is code/ORM-enforced only". Both are wrong.** Migration
  `036_settled_implies_settled_date.py` adds a real CHECK named
  `ck_transactions_settled_implies_settled_date`, SQL `status <> 'settled' OR settled_date IS NOT
  NULL` (`:55-56`), mirrored at flush time by `_enforce_settled_implies_settled_date`
  (`backend/app/models/transaction.py:168-186`). **Therefore reviewer B's derived warning is FALSE
  and is recorded as such:** "a non-ORM write path can produce a settled row with NULL `settled_date`
  that `settled_net` silently drops" cannot happen, regardless of write path.
- **Transaction count — UNFILTERED**, because `list_transactions` applies no
  `reportable_transaction_filter` and a filtered count would not match click-through. With no status
  predicate, `ix_transactions_org_settled_date` is unreachable (its `status` column sits in the
  middle) and a top-level `OR` across two columns degrades to an `org_id` prefix scan. **Ruling: a
  two-branch `UNION ALL`** — one branch `settled_date BETWEEN …`, one branch
  `settled_date IS NULL AND date BETWEEN …` (which does use `ix_transactions_org_date`). If the
  implementer measures that a plain `coalesce(...) BETWEEN` scan is acceptable at the capped N, that
  is a legitimate alternative — **but it must be recorded as an accepted org-scan, with the cost
  stated, not presented as sargable.**

  **Supporting citation, surfaced by both round-2 reviewers and adopted:** `_apply_transaction_filters`
  runs `date_from`/`date_to` through `effective_period_date_expr()`
  (`transaction_service.py:1994-1997`), that is `coalesce(settled_date, date)`. The two-branch
  `UNION ALL` is therefore not an approximation of the click-through set, it is **exactly** that set,
  decomposed into its two sargable halves. That is what makes the count and the deep link agree by
  construction rather than by coincidence.

**D8 [234b] — Display window semantics, and nothing else.** `?months` (default 12, clamped 1..60) is
a **calendar lookback from today**, with **no upper bound**, so future stubs always appear as
`upcoming`. Then `LIMIT 200`, newest-first (D6).

⚠ **Revision 3's set-union window is DELETED in all its parts:** the newest-12 floor, the
`end_date IS NULL` union term, and the `floored` response field. Every one of them existed to drag
analysis-relevant rows into a display window, which is a problem revision 4 does not have. The window
is now purely a display concern and carries no correctness weight.

**Consequence, accepted rather than patched:** on a maximally lapsed org whose every row predates the
lookback, `periods` can legitimately be **empty**. That is no longer a failure. `roster.period_count`
is non-zero, every marker is still emitted, and §1.1's summary band renders them from
`referenced_periods`. A user reaching for history that the default window does not reach widens
`?months`; a user reaching for *problems* does not have to.

`list_periods` caps at 24 with no window parameter (`billing_service.py:144-151`) — **do not route
through it**; neither the roster display nor `load_complete_roster` may use it.

**D8a [234b] — Resolve the clock ONCE, in the route.** The route resolves `today = date.today()` a
single time and passes that concrete date to `period_status`, to `find_period_anomalies` (for
`lapsed_open`), and to `period_spend_window_end`. This is not stylistic:
`period_spend_window_end`'s own docstring mandates it (`billing_service.py:572-577`, "**Callers that
resolve a window AND do any other date arithmetic must resolve the clock once themselves and pass a
concrete date to both**"). Without it, a request straddling UTC midnight can classify a row `past`
against day D while computing its `counting_through` against day D+1, producing a self-contradictory
row on the page whose entire job is catching self-contradictory rows.

**D9 [234b] — Admin-gated.** Both existing billing-period reads are ungated (`routers/settings.py:314`,
`:327`) while every mutation calls `_require_admin`. The roster exposes org-wide transaction counts
and settled net, and its page sits under `/settings/organization`, whose `SettingsLayout` tab is
`minRole: "admin"` (`frontend/components/SettingsLayout.tsx:13`). Gate it, or the page renders for a
user the API refuses.

**D10 [234b] — Never call `get_current_period` on this route.** It auto-creates **and commits** a
`BillingPeriod` when none is open (`billing_service.py:96-120`). A read-only diagnostic that
manufactures the row it reports on is disqualifying, and "no open row" is one of the anomalies this
page exists to *report*. (Note the existing `GET /billing-period` route does exactly this today; that
is out of scope here and is a recorded follow-up.)

**D11 — No writes, no migration, no schema change**, in either ticket. The page reports; TBD-235
repairs.

---

## 4. Test plan

House rules: anchor dates relative to `date.today()`; FK-sensitive assertions belong in the router
suite (`PRAGMA foreign_keys=ON`); **every service-level test names its public entry point.**

⚠ **Each item below is labelled `fence` (fails against an implementation missing the rule) or
`guard` (passes either way, kept as a regression net), and carries its owning ticket.** Revision 1
labelled none, and three of its "fences" could not fail. Revision 2 labelled them and three were
still wrong. Revision 3 fixed those and shipped a **new** vacuous one. This programme's vacuous-test
defect has now been caught **TEN times**.

### 4a. Kernel — `tests/services/test_period_anomalies.py` [TBD-234a]

Entry points: `load_complete_roster`, `find_period_anomalies`, `period_status`.

| # | Ticket | Test | Kind |
|---|---|---|---|
| 1 | 234a | Clean contiguous roster → no anomalies | guard |
| 2 | 234a | The healthy shape `[…closed…, OPEN, stub, stub]` → **no anomalies**. ⚠ Assert the **structural** set only, or pin the fixture converged relative to the injected `today` — `lapsed_open` is temporal and an unscoped "no anomalies" is a wall-clock date bomb (`reference_wall_clock_date_bomb_tests`) | **fence** — fails if the open row's end is read as unbounded, and fails if an interior open row is mis-paired |
| 3 | 234a | Gap between two **closed** rows → one `gap` with **both dates pinned** per §2.5 (`effective_end + 1` and `next.start − 1`) | **fence** — red against revision 1's dead-detector derivation, and the date pins make it falsifiable |
| 4 | 234a | Overlap between two **closed** rows → one `overlap`, dates pinned to `(rows[j].start_date, effective_end(rows, i))` | **fence** |
| 5 | 234a | **All-pairs overlap.** `A[2026-01-01→2026-12-31]`, `B[2026-02-01→2026-02-28]`, `C[2026-03-01→2026-03-31]`, all closed → **two** overlaps, `(A,B)` and `(A,C)` | **fence** — red against any adjacent-pair implementation |
| 6 | 234a | Duplicate open rows → `duplicate_open` naming **both ids** in `period_ids`, derived from the roster with no `open_row_ids` argument in sight | **fence** |
| 7 | 234a | Zero open rows → `no_open`; **no `straddling` marker is computed** and no exception is raised on an org with closed rows but no open row | **fence** |
| 8 | 234a | Roster tail (open row, no successor) → **not** a gap, **and** a genuine gap whose RIGHT member is that tail row **IS** reported | **fence** — ⚠ the second clause is red against revision 3's "participates in no pair, on either side" clause; the first clause alone is a guard (a pairwise iterator never makes `rows[-1]` a LEFT member) |
| 9 | 234a | **Straddling, non-adjacent, with two open rows.** A straddler `S` separated from anchor `O` by an intervening row `X` → `straddling(S)` naming `O` as `anchor_period_id`, **and** an `overlap` with **`from_period_id == S.id` and `to_period_id == O.id`** | **fence** — ⚠ **the id assertion is normative and is what makes this non-vacuous**: `overlap(S, X)` also holds and an adjacent-pair implementation emits it, so "an overlap marker is present" goes green against the very implementation all-pairs exists to kill |
| 10 | 234a | Row with `end_date < start_date`, **inserted directly** → `inverted`; `period_status` returns `invalid`; `length_days` is `null`; **and no `gap` or `overlap` is emitted on either side of it** | **fence** — not vacuous: the fixture bypasses every writer §2.3 proves non-inverting |
| 11 | 234a | **⭐ The differential fence.** For **every** row: `kernel_derived_end(roster, i) == await period_effective_end(db, org_id, row)`. The kernel does **not** call the helper, so this is a genuine differential and kills any divergence, including a regression to revision 1's formula. The kernel stays pure; only the TEST holds a session | **fence** — 234a's flagship |
| 12 | 234a | **Status partition**: an `invalid` row, a `current_by_calendar` row on a lapsed roster, and an open row starting tomorrow each get the documented status from `period_status`, with `today` injected | **fence** |
| 13 | 234a | **Analysis cap** (§2.4a): a roster past 2000 rows carrying a duplicate open pair → `overlap_analysis_skipped` with `period_count` and `cap`, **`duplicate_open` still emitted**, and `anomalies` is **not** empty | **fence** — red against a cap that suppresses every structural marker, and red against a silent empty list |
| 14 | 234a | `load_complete_roster` returns **every** row for the org, `start_date` ASC, on a roster larger than `list_periods`' 24 and larger than any lookback; and it is the only symbol that constructs a `CompleteRoster` | guard — the construction-site enforcement's own net; the type checker is the primary gate |

**⚠ Test 11's fixture is normative, and without it the test re-vacuums.** On a clean contiguous
roster `end_date == successor.start − 1` by construction, so the wrong derivation and the right one
agree and the test proves nothing. That is exactly why revision 1's version failed to catch its own
structural defect. **The fixture must contain, at minimum:**

- **(a)** a closed row where `end_date != successor.start − 1` — the row that kills revision 1's
  formula;
- **(b)** a closed row where `end_date >= successor.start` (an overlap);
- **(c)** an open **interior** row, asserting `successor.start − 1`;
- **(d)** an open **tail** row, asserting `None`.

**Without (a) and (b) both formulas agree and test 11 is vacuous.** This test alone needs real
`BillingPeriod` instances (to call the async helper) alongside the `CompleteRoster` row tuples.

### 4b. Endpoint — `tests/routers/test_billing_period_roster.py` [TBD-234b]

| # | Ticket | Test | Kind |
|---|---|---|---|
| 15 | 234b | The route **emits** `status` on every period, with the values `period_status` returns | guard — thin; the partition itself is fenced by test 12 |
| 16 | 234b | **⭐ The D4 fence.** `effective_end` and `counting_through` **diverge** on a lapsed roster and **agree** on a converged one. See the mechanism note below; this test is load-bearing three times over | **fence** |
| 17 | 234b | Overlapping periods: one transaction appears in the count of **every** row containing it | **fence** — kills the `CASE` shape |
| 18 | 234b | Count is unfiltered (a transfer leg counts); settled net is filtered (it does not) | **fence** |
| 19 | 234b | Future stubs render as `upcoming` (no upper bound) | **fence** |
| 20 | 234b | **Off-window markers.** An org whose corruption sits entirely **outside** the display window still reports it: the marker is present, `off_window` is `true`, and `referenced_periods` carries every named id **including `effective_end`** | **fence** — red against any residue of window-scoped analysis, and red against dropping `effective_end` from the referenced entries |
| 21 | 234b | `months=0` and `months=999` are clamped, not rejected; past `LIMIT 200`, `window.truncated` is true and the surviving rows are the **newest** ones (`window.from` equals the truncated lower bound, not the lookback bound); **`roster.period_count` still reports the full count and the anomaly set is unchanged by truncation** | **fence** — the last clause is what proves display truncation no longer touches analysis |
| 22 | 234b | **Scope separation belt** (§2.2 amendment 2): `roster.period_count` equals `SELECT COUNT(*) WHERE org_id = ?`, and `window.displayed_count` equals `len(periods)`, on an org where the two differ | **fence** — red if the route ever hands the kernel a windowed list |
| 23 | 234b | Non-admin → 403 | **fence** |
| 24 | 234b | The route creates **no** `BillingPeriod` on an org with no open row; period count unchanged; response reports `no_open` | **fence** — fails if anyone reaches for `get_current_period` |
| 25 | 234b | Org with zero periods → 200, `periods: []`, `referenced_periods: {}`, `no_open` | **fence** |
| 26 | 234b | `GET /billing-periods` response shape unchanged | guard (regression net; nothing here touches it) |

**⚠ Test 16's mechanism is normative. It absorbs, and replaces, revision 3's test 9.**

Revision 3 carried a separate kernel-level "clock independence of the structural set" test that was
**vacuous BY CONSTRUCTION**: the kernel is sync and holds no session, so `period_spend_window_end` is
unreachable from it and a kernel-level test can no longer fail. **Test 16 already IS the
clock-independence fence**, and it lives in 234b where both helpers are genuinely reachable. Three
mechanisms are folded into it, all normative:

1. **Straddle the derived end.** Two frozen clocks `T1`, `T2` with
   `T1 <= period_effective_end(open) < T2`. Under the prohibited wiring the floored helper yields
   **no** overlap at `T1` (`max(E, T1) = E`, and `S1.start = E + 1 > E`) and **an** overlap at `T2`
   (`max(E, T2) = T2 >= E + 1`), so the payloads differ and the test is RED. Under the correct wiring
   `effective_end` is identical at both clocks and it is GREEN.
2. **Compare full payloads including dates.** Combined with §2.5's pinned overlap semantics (the LEFT
   row's end) this is a genuine second independent red, not a restatement of mechanism 1.
3. **Assert the concrete expected value**, never merely "the two responses are equal". Equality-only
   assertions are the family this programme keeps getting burned by.

It must also **freeze the clock**, not only pass a `today=` kwarg: `period_spend_window_end` defaults
`today` to `date.today()` internally (`billing_service.py:600`), so a route that wires in the floored
helper and does not forward `today` is unaffected by a kwarg alone.

**Net effect of the deletion: one test deleted, one strengthened, zero coverage lost.**

### 4c. Frontend [TBD-234b]

| # | Ticket | Test | Kind |
|---|---|---|---|
| 27 | 234b | Page renders under `SettingsLayout` with the Organization tab active, by passing the literal `activeTab="/settings/organization"` | **fence** — ⚠ (§0.4) red if the page passes `activeTab="/settings/organization/periods"`, which un-highlights every tab |
| 28 | 234b | Anomaly markers render inline; the overlap note shows when any row overlaps | **fence** |
| 29 | 234b | Both end columns render; the divergence is visually distinguished only when they differ, via `badgeWarning` **plus a text label** (§1.1) | **fence** |
| 30 | 234b | **The summary band** (§1.1) renders every `off_window: true` marker from `referenced_periods`, on a response whose `periods` array does not contain the referenced ids; and the page renders exactly **one** `<h1>`, `SettingsLayout`'s | **fence** — red against inline-only marker rendering, which would silently drop off-window issues |
| 31 | 234b | A non-admin deep-linking the page is redirected, matching `settings/organization/page.tsx:106,128` | **fence** |

---

## 5. Rollout

Additive: one new GET, one new page, in that order across two tickets. No existing endpoint, query or
component changes, so nothing that works today can move. Worst case the new page renders wrong numbers
on a surface nobody depended on yesterday.

**234a merges caller-less, for exactly one ticket.** Three service helpers plus their tests, invisible
to users, so the kernel contract (§8) is merged, reviewed and frozen before any consumer exists to
constrain it. ⚠ **This is tolerable ONLY because 234b is committed, not optional.** The repo already
carries one uncalled helper defended by a docstring (`period_effective_end`,
`billing_service.py:483-490`), which is exactly why residual R1 keeps that docstring truthful rather
than letting a second stale one accumulate beside it.

**Known residual, recorded rather than discovered in review:** this page's numbers will disagree with
`/budgets` on lapsed orgs by construction. That is TBD-240 §7's accepted residual, and §2.1 is the
deliberate decision to *show* it rather than hide it.

No release note needed (new surface). No flag: `routers/settings.py` carries no `require_feature`
and this is a diagnostic, not a product surface.

---

## 6. Out of scope

- **Repairing** anything the page reports — TBD-235.
- The `closed_at` column — TBD-233, now blocked by this ticket and possibly droppable.
- The frontend "current period" unification — TBD-242, which adopts D2's status.
- The bound-separation refactor at the three fallback sites — TBD-243.
- The fleet-wide **sweep script** (§1) — follow-up; it calls 234a's kernel with no window vocabulary.
- Making the open period selectable in the transactions filter (§0.3) — follow-up.
- `GET /billing-period`'s auto-create side effect (D10) — follow-up.
- A `(org_id, start_date)` index for `load_complete_roster`'s sort (§2.2) — not proposed; recorded.

---

## 7. Sign-off record

**Architect sequencing round — 2 independent architects, unanimous, 2026-07-29.** Both chose
TBD-234 over TBD-233 and both ruled TBD-233 the wrong shape. Decisive argument, reached
independently: TBD-233 makes `end_date` always-populated, so a real end becomes indistinguishable
from a backfilled one and **the census this ticket exists to perform becomes impossible**, a one-way
door. Both also found that TBD-233 would silently revert TBD-240 (its `if period.end_date is not
None` branch becomes unconditionally true) with every TBD-240 test still green.

**Sign-off round 1 — revision 1: REJECT / REJECT** (two independent reviewers, 2026-07-29). Nine
blocking findings, converging almost entirely. **No design ruling was overturned; the mechanics
failed, and one of them badly.**

1. **The derivation rule was structurally wrong and would have shipped a no-op** (both reviewers,
   independently). It defined the derived end as `successor.start − 1` for *every* row; composed with
   the gap rule it reduced to `successor.start > successor.start`, **false always**. ⚠ **D5 was
   reversed into an IO mandate, and that reversal was the wrong fix; see round 3.**
2. **And the change that caused it was never needed.** `period_effective_end` returns before touching
   the DB for closed rows, so per-row calls cost ~1 extra query on a healthy roster, not N.
3. **The status partition was not disjoint** → ordered rules, `invalid` branch. **`straddling` and
   `overlap` fire on the same row**, and the test asserted the opposite → explicit precedence, and the
   straddle boundary corrected to the code's `>=`. **The clock-independence claim contradicted
   `lapsed_open`** → structural vs temporal sets. **D7's predicate defeated both indexes it named** →
   per-column shapes. **`N ≤ 24` had no basis** → `months` clamped 1..60, `LIMIT 200`, `truncated`.
   **No response contract existed at all** → §2.5.
4. Two findings whose subjects round 4 **deletes outright**: the out-of-window predecessor test could
   not fail (still vacuous after its fix; the whole convention is gone), and D8 returned an empty
   roster on the most lapsed orgs (the floor is gone; the real problem was that analysis was windowed
   at all).

**Sign-off round 2 — revision 2: REJECT / REJECT** (two independent reviewers, 2026-07-29). Four
blocking findings plus a scope ruling. **Again no design ruling was overturned; only mechanics.**
**Scope: ruled L, not M, and SPLIT** (recorded in §8; ⚠ round 4 confirms the split).

| # | Finding | Disposition |
|---|---|---|
| F1 | The overlap rule was adjacent-pair, so a fully-nested row emits no marker and renders clean | **ADOPTED.** gap adjacent-pair, overlap **all-pairs**, both normative |
| F2 | `straddling`'s anchor undefined for zero or multiple open rows: two conformant answers, and `AttributeError` → 500 on an org with no open row | **ADOPTED.** MAX-start open row; not computed when none |
| F3 | `LIMIT 200` truncation direction unstated (**unanimous**) | **ADOPTED.** Newest-first |
| F4 | The clock-independence test was vacuous as fixtured | **ADOPTED, three belts.** ⚠ Those belts now live in test 16; see round 3 |
| F5 | The predecessor test was vacuous for two independent reasons | **ADOPTED.** *(Test DELETED in round 4 with its subject.)* |

**Two reviewer errors recorded so they cannot resurface.** (C1) Both reviewers were **wrong** that
there is no DB CHECK behind D7's settled-net predicate; see D7. (C2) Round 1's item 3 reachability
claim was **disproved**: `invalid` is unreachable through shipped code, and the branch is kept as
**defensive** (§2.3).

### Sign-off round 3 — revision 3: REJECT / REJECT, and the contract found unbuildable

Two independent reviewers, 2026-07-29. This round did not find mechanics; it found the **contract**.

1. **⚠ The frozen kernel contract was UNBUILDABLE, found independently by both reviewers.** §8.1
   froze `find_period_anomalies` as **pure**, "it must NOT fetch rows itself", while D5 mandated
   **"call `period_effective_end` per row"**, an `async` function requiring a session. The two
   clauses were mutually exclusive, and every downstream mechanism (the predecessor convention, the
   `open_row_ids` carve-out, the union window) existed to prop up the impossible middle.
2. **⚠ The clock-independence test was VACUOUS BY CONSTRUCTION. This is instance TEN, and it landed
   in the revision that folded the ninth.** Whatever the fixture, a kernel test could not distinguish
   the two helpers once the kernel could not reach either.
3. **⚠ Revision 3 introduced THREE NEW DEFECTS while folding nine.** All three:
   - the **"participates in no pair, on either side"** clause, which suppressed real gaps and
     overlaps whose RIGHT member was a tail open row (both rules only ever read the LEFT row's end);
   - the union window's **interior holes**, since `(lookback) ∪ (newest 12) ∪ (all open rows)` is not
     a contiguous run and the adjacent-pair gap rule fires spuriously across every hole;
   - the **union-versus-truncation collision**, where the union pulls rows in below the lookback and
     `LIMIT 200` newest-first then discards them again, so the guaranteed-present open row is not
     guaranteed.

   ⚠ **Two of those three came from folding round-2 NON-BLOCKING items. Folding findings introduced
   findings.** That is the round's most transferable lesson and it is why round 4 is a subtraction.

**The fresh-architect subtraction ruling, unanimous.** Two architects who had not written any prior
revision were asked what the kernel's input domain *should* be, and both answered the whole roster,
independently, before seeing round 3's findings. **Subtract the WINDOW from the kernel's domain, not
the PURITY from its signature.** Every round-3 finding is downstream of the fusion of a display
concern with an analysis domain, and removing the fusion removes the findings rather than patching
them.

**The tie-break, recorded because it settles a two-round dispute.** The architect who authored **both
the D5 IO mandate (round 1, finding 1) and the frozen contract (round 2, §8.1)** was given the
subtraction and **conceded all three contested points**:

- **D5's IO mandate was the wrong lesson.** ⚠ **The defect class is "a derivation that does not
  case-split on `end_date IS NULL`", NOT "in-memory derivation".** Revision 1's formula was wrong;
  in-memory derivation was never the problem. **The correct fence is a DIFFERENTIAL TEST against the
  helper (test 11), never an IO mandate.**
- **The window belonged to display, never to analysis.** `open_row_ids` was an admission of that in
  miniature: an org-wide carve-out inside a windowed kernel. Org-wide is now the general rule and the
  carve-out is deleted.
- **The strong claim was available all along.** Revision 3's D8 caveat ("the response states the
  window bounds so a marker's absence is never mistaken for health") conceded the page could not be
  trusted. Org-wide analysis lets it say *absence of markers means the roster is healthy*.

**What round 4 deleted, and it is roughly a fifth of the document:** D5's IO mandate; D8's set-union
window in all three parts (the newest-12 floor, the open-row union term, the `floored` field); the
`open_row_ids` kernel parameter; the out-of-window predecessor convention and its "flagged
non-displayable" clause; §2.2's entire window-edge subsection; the predecessor test; the kernel-level
clock-independence test (absorbed into test 16); and the "either side" tail clause. **No design
ruling was overturned in any of the three rounds.** The two-ends display, the server-canonical
status, the kernel-as-deliverable, all-pairs overlap, the MAX-start anchor, admin gating, the
no-`get_current_period` rule and the additive rollout have now survived **three** independent
rejection rounds unchanged.

**Running tally.** This programme's vacuous-test defect (a test green against unmodified `main` while
claiming to fence the fix) has now been caught **TEN times**: six across TBD-232/239/241/240, three
in this document's rounds 1 and 2, and a tenth in round 3 — **in the revision that folded the ninth**.
`reference_vacuous_test_pattern.md` stands: **revert the fix and confirm red is the only reliable
gate.**

**Sign-off round 4 — revision 4:** _(pending)_

---

## 8. The split and the frozen kernel contract

**⚠ The split is CONFIRMED and KEPT. Both round-4 architects ruled the split itself was not the
error; the CUT LINE was**, because it ran through the end-derivation and through the window. The new
seam:

> **234a = "given a complete roster and a clock, what is wrong with it."**
> **234b = "fetch the roster, aggregate the money, window the display, render it."**

**Zero window vocabulary crosses the line.** That is the test for whether a future edit belongs on
one side or the other.

| | TBD-234a — the kernel | TBD-234b — the route and page |
|---|---|---|
| **Deliverables** | `load_complete_roster`, `find_period_anomalies`, `period_status`, the `PeriodAnomaly` / `CompleteRoster` types, the marker payload schema, residual R1 | the route, D6/D7/D8/D8a display windowing and aggregates, D9 gating, the §2.5 response body, `referenced_periods`, the page |
| **Sections** | §2.2, §2.3, §2.4, §2.4a, §2.5's kernel types and marker table, D2, D4, D5 | §1.1, §2.1, §2.5's response body, D1, D3, D6, D7, D8, D8a, D9, D10 |
| **Tests** | 1-14 | 15-31 |
| **Contains** | no route, no page, no display window, no aggregates, **no window vocabulary** | no changes to the kernel |
| **Order** | ships FIRST | opens only AFTER 234a merges |

**⚠ Never run the two in parallel.** 234b's tests consume 234a's contract as frozen. Parallel work
against an unfrozen signature is exactly how a contract drifts, and a drifted kernel signature is
what forces a rewrite.

**Test 12 (the status partition) lives in 234a**, as a service-level test of `period_status` with
`today` injected. 234b keeps only test 15, a thin assertion that the route emits the field. The
partition is a kernel fact and should not first be tested through an HTTP round trip.

### 8.1 The frozen kernel contract

Four items are **frozen by 234a** and 234b consumes them unchanged.

1. **The two signatures** (§2.2). `find_period_anomalies` is **PURE and SYNC**: it takes no session,
   so `period_spend_window_end` and `period_effective_end` are both structurally unreachable from it
   and the boundary model's prohibition is enforced by the type system rather than by a test.
2. **The `CompleteRoster` type and the completeness precondition it carries** (§2.2). Nominal, one
   constructor, row tuples not ORM entities; a windowed `list[BillingPeriod]` is a **type error**.
   Enforcement is the single construction site, with test 22 as the belt.
3. **`today` is injectable and required.** No `date.today()` inside the kernel or `period_status`.
   The route resolves the clock once and passes it down (D8a).
4. **The marker payload schema** (§2.5's eight-kind table), including `gap`'s and `overlap`'s pinned
   dates.

**⚠ The freeze NO LONGER covers `open_row_ids` or the out-of-window predecessor convention. Both are
deleted.** `no_open` and `duplicate_open` derive from the complete roster like every other marker,
and there is no predecessor to fetch because there is no window to be outside of.

**Rationale.** Without these four, 234b would be forced to **rewrite the kernel**: a
fetching-and-windowing kernel cannot be reused by the sweep script, a roster-shaped input that is not
nominally complete silently degrades to whatever the caller passes, a `date.today()` kernel cannot be
clock-injected, and an unspecified payload gets reshaped by whoever renders it. A forced downstream
rewrite of an already-merged unit is precisely the failure mode the architects used to **reject
TBD-233**, and revision 3 came one sign-off away from repeating it inside this ticket's own split.
