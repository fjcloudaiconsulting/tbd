# TBD-234 — read-only billing period roster, and the anomaly kernel

Status: REVISION 3 — awaiting re-sign-off
Date: 2026-07-29
Jira: TBD-234 (Story, child of TBD-213) — **re-scoped to effort L and SPLIT in two.** **Blocks TBD-233 / TBD-242.**
Predecessors, all merged: TBD-232 (#586), TBD-239 (#587), TBD-241 (#588), TBD-240 (#589)
Related specs: `2026-07-28-open-period-spend-window-design.md`, `2026-07-28-close-period-chain-close-design.md`, `2026-07-28-billing-period-boundary-integrity.md`, `2026-07-27-billing-period-truth-and-safety.md`

**This one document covers two tickets.** Round 2 sign-off ruled the work **L, not M**, and ruled it
must split:

- **TBD-234a — the kernel.** `find_period_anomalies` plus the §2.3 status partition as a
  `period_status` helper, both in `billing_service.py`. Tests 1-11. Ships FIRST.
- **TBD-234b — the route and the page.** Windowing, aggregates, gating, the response contract, the
  page. Tests 12-26. Opens only AFTER 234a merges.

Every deliverable below is labelled **[234a]** or **[234b]**. §8 records the split and freezes the
kernel contract that 234b consumes.

Sequencing settled by two independent architects, unanimous. Revision 1 was **REJECTED 2-0** on nine
blocking findings. Revision 2 was **REJECTED 2-0** again on four more, one of them found
independently by both reviewers. In both rounds **no design ruling was overturned**; only the
mechanics failed. Full record in §7.

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
page does accept `date_from`/`date_to` URL params (`frontend/app/transactions/page.tsx:301-311`,
`DATE_PARAM_RE` at `:49`), so a roster row can deep-link the open period's window directly, which is
how this page should link out.)* The count rule stands on internal consistency regardless (D7).
Making the open period selectable in the dropdown is a recorded follow-up, not fixed here.

⚠ **The deep link must choose an end, and revision 2 did not say which.** §2.1's whole point is that
`effective_end` and `counting_through` differ on a lapsed org, so `/transactions?date_from=…&date_to=…`
is ambiguous as written. **Ruling: link the `counting_through` window.** That is the window
`/budgets` bills against, and the click-through exists so a user can reconcile against **what they
were shown**, not against a derived bound no other surface uses.

**Known side effect, recorded not discovered:** the target writes the parsed params into
`persistedFilters` and clears `filterPeriod` (`frontend/app/transactions/page.tsx:301-311`), and that
store persists to localStorage. So following a roster deep link **mutates the user's saved
transaction filters**. Acceptable for a diagnostic click-through, but it is a real effect and the
page copy should not pretend the link is read-only.

**0.4 — `SettingsLayout` highlighting: right answer, wrong mechanism.** Revision 2 said
`frontend/components/SettingsLayout.tsx:53` "compares `activeTab === tab.href` as a plain string, so
`/settings/organization/periods` highlights the Organization tab". The comparison is real (`:53-54`)
but **`activeTab` is a caller-supplied prop** (`:18`), not `usePathname()`. Nothing anywhere compares
`/settings/organization/periods` against anything. The tab highlights **only because §1 mandates the
page pass the literal string `/settings/organization`.**

This matters because the mis-description invites an implementer to "correct" it by passing the real
pathname, which silently un-highlights **every** tab on the page. Test 23 is promoted from `guard` to
`fence` accordingly (§4): it goes red if the page passes `activeTab="/settings/organization/periods"`.

**0.5 — Verified correct:** `effective_period_date_expr()` is `coalesce(settled_date, date)`
(`transaction_filters.py:132`) and is not sargable; `list_transactions` applies no
`reportable_transaction_filter`, so the unfiltered-count ruling holds; `use-billing-periods.ts`
shares one SWR key and the cold-mount guard is real; `/settings/organization`'s tab is
`minRole: "admin"` (`frontend/components/SettingsLayout.tsx:13`); `SettingsLayout` already renders the
route's single `<h1>` (`:47`).

---

## 1. Scope

One new read-only endpoint, one anomaly kernel, one page, across two tickets.

**[234a]**
- **`find_period_anomalies`** in `billing_service.py` — **the named backend deliverable**, not an
  implied UI feature. Both merged specs already assign it here
  (`2026-07-28-billing-period-boundary-integrity.md:298`). Testable without the route.
- **`period_status`** in `billing_service.py` — the §2.3 partition, as a pure helper.
- The `anomalies` marker payload schema (§2.5), which is the kernel's output contract.

**[234b]**
- **`GET /api/v1/settings/billing-periods/roster`** — new, admin-gated (D9).
- Windowing (D6/D7/D8), the per-row aggregates, and the full §2.5 response body.
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
ships is what it will call.

### 1.1 Layout intent [234b]

§2.5's payload maps 1:1 onto a nine-column table, and that is the shape an implementer will reach for
by default. **It is forbidden.** `DESIGN.md` names it as an explicit anti-reference ("if a screen
reads like Google Sheets in a wrapper, redesign before shipping") and it violates `PRODUCT.md`'s
*hierarchy-without-grids*.

**Ruling: 234b owes a design pass and must not ship a raw grid.** The intent, not designed in detail
here: period rows read as a **timeline** with their anomaly markers **inline on the row they
concern**, grouped in `card` surfaces, under one `pageTitle`. Note `SettingsLayout` already renders
the route's single `<h1>` (`frontend/components/SettingsLayout.tsx:47`), so the page adds no second one.

**Token rulings, so the design pass cannot trip CI:**

- §2.1's "visually differentiated when they diverge" named no token in revision 2. The natural reach
  is `text-accent`, which breaks **The One Brass Rule** on the first roster carrying three lapsed
  rows. **Use `badgeWarning`** (`bg-warning-dim text-warning`, `frontend/lib/styles.ts:69-70`) **plus a
  text label**, which also satisfies "don't rely on color alone".
- There are **seven marker kinds and five badge variants**, and `badgeSuccess` is inappropriate for
  an anomaly. **Ruling: the kind rides the text and icon; the color carries severity only.** Without
  this an implementer invents a hue and trips `frontend/scripts/check-design-tokens.sh`.

No migration. No schema change. No write path. Nothing existing is edited except adding the route
and the page.

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

### 2.2 The derived end — one definition, stated once [234a]

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
produce a gap or overlap against its *immediate* successor — its end is *defined* by that successor,
so they abut by construction. (It can still overlap a **non-adjacent** row; see §2.4's all-pairs
ruling.) That is why the healthy shape `[…closed…, OPEN, stub, stub]` yields no markers.

**Window edges.** The window's lower boundary still needs **one row older** than it, used only to
compute the leading gap/overlap marker, never for display, and never for an end derivation (no
displayed row's end depends on it). Revision 1 also mandated "one newer than its upper", which
contradicts D8's no-upper-bound; deleted.

⚠ **"One row older than the window's lower boundary" means one row older than the lowest row
ACTUALLY RETURNED** — post-floor (D8) and post-truncation (D6) — not one row older than the raw
lookback bound. With the raw bound the fetched predecessor is frequently already on display, because
D8's newest-12 floor and the open-row union both pull rows in below the lookback. The leading marker
would then double-fire: once from the ordinary pairwise loop and once from the predecessor pass. The
predecessor is included in the kernel's input list flagged **non-displayable** (§8).

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

`today` is a **required argument**, resolved once by the caller (§3, non-blocking item 1). Never
`date.today()` inside the helper.

**⚠ `invalid` is UNREACHABLE through shipped code. Revision 2's justification was false; the branch
stays anyway, as a defensive one.**

Revision 2 asserted "Reviewer B walked a two-close sequence that commits one." Round 2 disproved it:
`close_period` rejects `requested < current.start_date` at `billing_service.py:1034-1039` with a
`ValidationError`, **after** taking the row lock, so the second close in that sequence 400s rather
than committing an inverted row. Every `BillingPeriod.end_date` writer was then enumerated and each
is provably non-inverting:

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
and `past`, which breaks the partition. It is also a **fifth anomaly shape** (§2.4). Test 8b inserts
the row directly, so it is not vacuous.

`current_by_calendar` is the disputed shape, and the divergence is **sharper** than revision 1
said. Corrected: on the dashboard such a row is *neither* current, past, nor future —
`dashboard/page.tsx:271-273` computes `isCurrent = end_date === null` (false),
`isPast = end_date < today` (false, it contains today), `isFuture = start_date > today` (false). It
falls through all three. On Forecasts it is *Current*
(`frontend/app/forecast-plans/ForecastPlansClient.tsx:253-256`, calendar containment). Naming it
explicitly is what lets TBD-242 resolve this against a tested definition instead of inventing a fifth.

**What `status` does NOT do:** it classifies rows, it does not *select* one. On a lapsed roster the
open row is `open` while a stub is `current_by_calendar`; on a corrupt roster two rows can both be
`current_by_calendar`. Choosing which row a screen should display remains TBD-242's problem, and
this spec deliberately does not pre-empt it.

### 2.4 The anomaly kernel [234a]

**Four shapes, not two.** The ticket names gaps and overlaps; the code defers two more here. ⚠
Revision 2 wrote rules 1 and 2 against `row.end_date`, which is wrong twice over: §2.2 and D5
mandate `period_effective_end`, and on the open row the literal column is `None`, so
`None + timedelta` raises `TypeError`. **Both rules are restated in terms of `effective_end(row)`.**

1. **gap** — **ADJACENT-PAIR.** For each consecutive pair `(rows[i], rows[i+1])` in `start_date` ASC
   order: `rows[i+1].start_date > effective_end(rows[i]) + 1 day`. An all-pairs gap rule is
   meaningless (every non-neighbour pair has something between them), so the domain is normative:
   **gap is evaluated on adjacent pairs only.**
2. **overlap** — **ALL-PAIRS.** For every `i < j` in the window:
   `rows[j].start_date <= effective_end(rows[i])`. **The domain is normative.**
3. **duplicate open rows** — more than one `end_date IS NULL`. **The most damaging shape**: every
   frontend `findIndex(p => p.end_date === null)` silently picks the first, so two rows both claim
   "current" and different screens can pick differently. Already detected-and-only-logged at
   `billing_service.py:84-92`, named as a known hole at `:227-232`, and reachable through
   `POST /billing-period` (`seed.py` does it).
4. **no open row** — zero rows with `end_date IS NULL`. Every consumer calls `get_current_period`,
   which would *auto-create and commit* one.
5. **inverted row** — `end_date < start_date` (§2.3 branch 1). Revision 1 omitted it entirely.

Plus one informational marker: **straddling row** — see the anchoring ruling below.

#### ⚠ Why overlap is ALL-PAIRS (revision 2's rule was adjacent-pair and under-reported)

Revision 2 phrased both rules against "successor", and §2.2 defines the successor via
`_next_period_start`, which is `MIN(start_date) WHERE start_date > period.start_date`
(`billing_service.py:425-431`): the **immediate** next row. That makes the overlap rule
adjacent-pair, and adjacent-pair overlap detection is unsound.

**Attack vector.** Roster `A [2026-01-01 → 2026-12-31]`, `B [2026-02-01 → 2026-02-28]`,
`C [2026-03-01 → 2026-03-31]`, all closed. Adjacent pairs give `(A,B)` → one overlap, and `(B,C)` →
contiguous, no marker. **`A` overlaps `C` completely and no marker is emitted.** The roster reports
one overlap where there are two, and renders `C` clean. This is the nested-containment corruption
class that `routers/settings.py:417-421`'s TOCTOU hole admits, on the page that exists to find it.

**Second consequence: adjacent-pair semantics falsify §2.4's own precedence ruling.** On
`A [Jan 1 → Jun 30]`, `B [Feb 1 → Feb 28]`, `OPEN [Mar 1, NULL)`, `stub [Apr 1 → …]`, `A` straddles
`OPEN`, but `A`'s successor is `B`, so no overlap ever fires for A↔OPEN and "straddling is emitted in
addition to overlap" is unachievable.

**Cost.** At `LIMIT 200` (D6) all-pairs is at most `200 × 199 / 2 = 19,900` in-Python comparisons on
data already in memory. That is free. There is no performance argument for the unsound rule.

#### ⚠ The `straddling` anchor (undefined in revision 2 for zero or multiple open rows)

Revision 2 wrote `start_date <= open.start_date AND end_date >= open.start_date` without saying
which `open`.

**Attack vector.** Roster `C = [2022-01-01, 2022-05-31]` closed, `A = [2022-03-01, NULL)` open,
`B = [2026-07-01, NULL)` open. Anchoring on the **first** open row by start gives
`C.start <= A.start` and `C.end >= A.start` → straddling emitted. Anchoring on `get_current_period`'s
choice (`ORDER BY start_date DESC` → `B`) gives `C.end (2022-05-31) >= 2026-07-01` false → **not**
emitted. Same input, two spec-conformant responses, on exactly the duplicate-open roster this page
targets. Separately, on an org with rows but **zero** open rows, `open.start_date` is undefined and
the naive implementation raises `AttributeError`, so the page **500s on the exact org it exists for**.

**Ruling: `straddling` is anchored on the open row `get_current_period` would select, that is the
row with MAX `start_date` among rows where `end_date IS NULL`.** That is the row `_apply_close_step`
will actually evaluate, so the marker predicts real behaviour rather than a hypothetical. **When
there are zero open rows the marker is not computed at all** — `no_open` already carries that signal
and there is nothing to straddle.

The predicate, with the anchor pinned:

```
straddling(row) = row.start_date <= anchor.start_date AND effective_end(row) >= anchor.start_date
```

⚠ **`>=`, at or after — matching `_apply_close_step`'s own predicate at `billing_service.py:776-777`.**
Revision 1 said "ends after it", which would silently under-report exactly the shape whose deferral
(`:914-922`) created this marker.

**Marker precedence, normative.** A straddling row is by construction also an overlap under rule 2
(**now true by construction, because rule 2 is all-pairs**; under revision 2's adjacent-pair rule it
was not). Revision 1's test 8 asserted "straddling → not an overlap" while its rules emitted both.
**Ruling: a row may carry multiple markers, and `straddling` is emitted *in addition to* `overlap`,
not instead of it.** Suppressing the overlap would hide genuine overlaps on any roster containing a
straddler, precisely the rosters this page targets. Test 8's fixture pins a **non-adjacent**
straddler, so the ruling is fenced against a regression to adjacent-pair semantics.

**Two output sets, because one of these is not clock-free:**

| set | markers | clock |
|---|---|---|
| **structural** | gap, overlap, duplicate_open, no_open, inverted, straddling | none |
| **temporal** | lapsed_open (derived end in the past) | reads the injected `today` |

Revision 1 put `lapsed_open` in one undifferentiated set and then asserted the whole set was
clock-independent — a direct contradiction, since "in the past" is a comparison against `today`.
The split is what makes the D4 fence (§4 test 9) both true and able to fail.

`lapsed_open` is computed on the **anchored** open row (MAX `start_date` among open rows), the same
anchor `straddling` uses. D8's open-row union guarantees that row is in the window.

**⚠ The kernel consumes `period_effective_end`, never `period_spend_window_end`.** This is the
entire reason #589 split one helper into two (its §2.1): handing a gap/overlap detector a
clock-dependent end paints phantom overlaps between the open row's floored window and the historic
stubs on *every* lapsed org — verbatim the failure
`reference_billing_period_boundary_model.md` exists to prevent. **Fence it with test 9**, whose
fixture is load-bearing in a specific way spelled out in §4.

**Gap versus tail.** `period_effective_end` returns `None` for the roster tail
(`billing_service.py:512-513`). An open row with no successor is not a gap, and a row whose
`effective_end` is `None` participates in **no** pair, on either side, under either rule. And the
intended shape `[…closed…, OPEN(end=NULL), stub, stub, stub]` must not read as an overlap — that is
the exact false positive the boundary model warns about.

### 2.5 Response contract

**The `anomalies` marker payload schema is [234a]** — it is the kernel's output contract and 234b
consumes it verbatim. **Everything else in this section is [234b].**

Revision 1 specified none, and TBD-242 is contracted to adopt `status` **by name**. Normative:

```jsonc
{
  "window": { "from": "2025-08-01", "to": null, "truncated": false, "floored": false },
  "periods": [
    {
      "id": 41,
      "start_date": "2026-07-25",
      "end_date": null,                  // raw column, null for the open row
      "effective_end": "2026-08-24",     // period_effective_end; null only for the roster tail
      "counting_through": "2026-08-24",  // period_spend_window_end; null only for the tail
      "status": "open",                  // open | upcoming | current_by_calendar | past | invalid
      "length_days": 31,                 // null when effective_end is null OR status is "invalid"
      "transaction_count": 42,           // unfiltered (D7)
      "settled_net": "-1240.55"          // string, per the repo's Decimal convention
    }
  ],
  "anomalies": [
    { "kind": "gap",      "from_period_id": 40, "to_period_id": 41,
      "from_date": "2026-07-01", "to_date": "2026-07-24" },
    { "kind": "overlap",  "from_period_id": 40, "to_period_id": 44,
      "from_date": "2026-07-25", "to_date": "2026-09-30" },
    { "kind": "duplicate_open", "period_ids": [41, 44] }
  ]
}
```

- `status` values are the snake_case literals above. `invalid` is branch 1 of §2.3.
- **Ordering: `start_date` ASC** in the response body. A roster is read as a timeline; `list_periods`'
  DESC ordering is for a different consumer. ⚠ This is the **response** ordering, not the query's;
  see D6 on truncation direction.
- **`length_days` is `null` on an `invalid` row.** `effective_end − start_date + 1` is negative there
  and a negative length is meaningless; the `invalid` status carries the signal.
- `settled_net` serializes as a **string**, matching the repo's existing Decimal convention
  (`specs/tech-debt-frontend-decimal-typing.md`).
- `anomalies` carries the **structural** set plus `lapsed_open`; each marker names the period ids it
  concerns and, for `gap`/`overlap`, the date range. Clients must tolerate unknown `kind` values.
- **⚠ `overlap`'s date semantics are pinned, normative:** `from_date = rows[j].start_date` and
  **`to_date = effective_end(rows[i])`, the LEFT row's end**. Not `min(effective_end(i),
  effective_end(j))`, not the intersection. Revision 2 left this unspecified, and the intersection
  reading is what let test 9 stay green against the prohibited helper at full assertion depth (§4).
- `duplicate_open` and `no_open` carry **`period_ids`**, not a count (§8).
- **Empty roster** (org with zero periods): `200`, `periods: []`, and `anomalies: [{"kind":
  "no_open", "period_ids": []}]`. D10 forbids manufacturing a row to avoid this.
- **`months` out of range is clamped, not rejected.** ⚠ Revision 2 justified this as "a diagnostic
  page should not 422 on a URL typo", which is **unachievable as written**: FastAPI coerces
  `months: int`, so `?months=abc` 422s before any handler code runs. **Ruling: drop the claim, keep
  the clamp**, and state it precisely as *out-of-range integers are clamped, not rejected*. A
  non-integer 422 is correct FastAPI behaviour and this spec does not fight it.

---

## 3. Decisions

**D1 [234b] — New endpoint, not a widened one.** §1. `GET /settings/billing-periods/roster`.

**D2 [234a] — Server-computed `status`, canonical, a partition.** §2.3. Ships as `period_status`.

**D3 [234b] — Both ends rendered, labelled.** §2.1, tokens per §1.1.

**D4 [234a] — The anomaly kernel is the named backend deliverable**, lives in `billing_service.py`
beside the two helpers, is testable without the route, and consumes only the pure helper. §2.4. Its
signature is frozen in §8.

**D5 [234a] — Call `period_effective_end` per row; do NOT reimplement it.** §2.2. Fetch one row older
than the **lowest row actually returned** for the leading marker only. *(Revision 1 ruled the
opposite and that is what produced the dead-detector defect.)*

**D6 [234b] — Per-row bounded aggregates, over a hard-capped N. Reject the single grouped `CASE`
query outright.** The ticket correctly warns that a `CASE` returns the first match — and then still
frames the work as one grouped query. On the page whose *purpose* is exposing overlaps, a transaction
must be attributable to **every** period that contains it. Overlapped transactions appear in more
than one row's count, and the UI carries an explicit note that counts may exceed the org total where
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

⚠ **The cap's truncation DIRECTION is normative, and revision 2 omitted it. This was the round's only
unanimous blocker, found independently by both reviewers.**

**Attack vector.** An org with 250-400 period rows (D6 itself argues this is reachable through
`POST /billing-period`). The literal composition of revision 2's D6 with §2.5's single normative
"`start_date` ASC" ordering is `ORDER BY start_date ASC LIMIT 200`, which returns the **OLDEST 200**
rows and discards the open row, every stub, and every recent boundary. Since gap, overlap, inverted,
straddling and lapsed_open are all window-scoped, **genuine recent anomalies are silently omitted**.
The response is internally consistent-looking: an admin diagnosing a broken roster sees
`anomalies: []` beside five-year-old rows and concludes the org is healthy. Test 18 asserted only
that `truncated` is true and could not catch it.

**Ruling: the cap selects the NEWEST rows.** The query is `ORDER BY start_date DESC LIMIT 200`; the
result set is re-sorted `start_date` ASC for the response body (§2.5). This is consistent with D8's
newest-12 floor, which already encodes "recency is what a diagnostic needs". Consequences, all
normative:

- **`window.from` reports the TRUNCATED lower bound**, that is the minimum `start_date` actually
  returned, not the requested lookback bound. Reporting the requested bound would claim coverage the
  response does not have.
- **The §2.2 one-row-older predecessor fetch is taken relative to the TRUNCATED bound**, so the
  leading marker is computed against the row that really precedes the display, not against a row 200
  periods away.

**Query budget at the cap, recorded as an accepted cost.** At `LIMIT 200` with two aggregates per row
the worst case is **~400 round trips**, and up to **~600** if an implementer adds a third per-row
query (for example a per-row `period_effective_end` call on a roster with many open rows). This is
accepted, not hidden: the cap exists precisely so the number is bounded and statable. ⚠ Note DO App
Platform applies a request timeout, so an org near the cap is the one that will hit it first; if it
does, the fix is the alternative below, not raising the cap.

**Recorded alternative, legitimate but not mandated.** A single
`JOIN billing_periods ON <bucketing date> BETWEEN start_date AND effective_end` emits one row per
(period, transaction) pair, which **natively satisfies D6's every-containing-period requirement** in
one query rather than 400, and is therefore not the rejected `CASE` shape. It is a legitimate
implementer option. **It must be measured before adoption** (the join predicate is not sargable
against either named index) and it must reproduce the per-row numbers the tests pin. Recorded here so
a later implementer does not think D6 forbids it.

**D7 [234b] — Two columns, two filters, two *different* predicate shapes.** ⚠ Revision 1 named the
right indexes and then mandated one predicate shape for both columns — which **defeats the very index
it claims each column uses**. Both reviewers caught it. Corrected:

- **Settled net — `reportable_transaction_filter()` + `status = SETTLED` + plain
  `settled_date BETWEEN a AND b`.** No `OR`, no `coalesce`. That is a clean three-column range on
  `ix_transactions_org_settled_date` = `(org_id, status, settled_date)`. The `settled_date IS NULL`
  disjunct revision 1 mandated is **dead code here**, and adding it removes the range from the
  trailing key part, collapsing the plan to `(org_id, status)`.

  ⚠ **Citation corrected, and the conclusion is now more strongly justified than revision 2 claimed.**
  Both round-2 reviewers filed a non-blocking correction saying "there is no DB CHECK constraint; the
  invariant is code/ORM-enforced only". **Both reviewers are wrong.** Migration
  `036_settled_implies_settled_date.py` adds a **real DB CHECK** named
  `ck_transactions_settled_implies_settled_date` with SQL `status <> 'settled' OR settled_date IS NOT
  NULL` (`:55-56`), preceded by a backfill of pre-existing orphan rows. It is mirrored at flush time
  by `_enforce_settled_implies_settled_date`, a `before_insert`/`before_update` event listener
  (`backend/app/models/transaction.py:168-186`) whose own docstring says it "Mirrors the DB CHECK
  constraint added in migration 036". Revision 2's citations (`transaction_service.py:582`,
  `:594-599`, migration `020:21-24`) point at call sites and the additive backfill, not at the
  constraint; **replaced with migration 036 and the model listener.**

  **Therefore reviewer B's warning is FALSE and is recorded as such:** "a non-ORM write path can
  produce a settled row with NULL `settled_date` that `settled_net` silently drops" cannot happen.
  The DB CHECK blocks it at the storage layer regardless of write path.
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

**D8 [234b] — Window semantics, with a floor so the page cannot render empty.** `?months` (default 12,
clamped 1..60) is a **calendar lookback from today**, with **no upper bound**, so future stubs
always appear as `upcoming`.

⚠ **Revision 1 stopped there, and that returns an empty roster on the most lapsed orgs** — an org
whose open row is `[2022-03-01, NULL)` with stubs through 2022-06 has *zero* rows inside a 12-month
lookback, so the page renders blank for precisely the roster it exists to diagnose. Reviewer B
called this strictly worse than the alternative revision 1 rejected.

⚠ **And revision 2's floor does not actually guarantee the open row is present.** On an org holding
12 or more rows **newer** than a lapsed open row, both the lookback and the newest-12 floor miss it,
so `lapsed_open` and `straddling` have no anchor in the window and `duplicate_open` cannot name its
ids from the window at all.

**Ruling: the window is `(rows within the lookback) ∪ (the newest 12 rows) ∪ (all rows with
`end_date IS NULL`)`.** The third term is what makes the anchored open row (§2.4) reachable by
construction. Then `LIMIT 200`, newest-first (D6).

**`window.from` under the floor.** If the newest-12 floor or the open-row union pulls in rows older
than the lookback bound, `window.from` reports the **actual minimum returned `start_date`**, and the
response carries **`"floored": true`** (§2.5) so a client can tell "12 months of history" from "we
reached back further to keep the page honest". Combined with D6, `window.from` is always the real
lower edge of what was returned, under truncation and under flooring alike.

**Anomalies are computed over the returned window, not the whole roster**, and the response states
the window bounds so a marker's absence is never mistaken for health. One exception: `no_open` and
`duplicate_open` are computed over **all** of the org's rows, via a cheap
`SELECT id WHERE end_date IS NULL` (ids, **not** `COUNT(*)`; §2.5 emits `period_ids`, and §8 freezes
this as the kernel's `open_row_ids` argument). Both are org-level facts that a windowed view would
report falsely.

`list_periods` caps at 24 with no window parameter (`billing_service.py:144-151`) — **do not route
through it**; the roster needs its own query.

**D8a [234b] — Resolve the clock ONCE, in the route.** The route resolves `today = date.today()` a
single time and passes that concrete date to the status partition, to `find_period_anomalies` (for
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
manufactures the row it reports on is disqualifying — and "no open row" is one of the four
anomalies this page exists to *report*. (Note the existing `GET /billing-period` route does exactly
this today; that is out of scope here and is a recorded follow-up.)

**D11 — No writes, no migration, no schema change**, in either ticket. The page reports; TBD-235
repairs.

---

## 4. Test plan

House rules: anchor dates relative to `date.today()`; FK-sensitive assertions belong in the router
suite (`PRAGMA foreign_keys=ON`); **every service-level test names its public entry point.**

⚠ **Each item below is labelled `fence` (fails against an implementation missing the rule) or
`guard` (passes either way, kept as a regression net), and carries its owning ticket.** Revision 1
labelled none, and three of its "fences" could not fail. Revision 2 labelled them and **three were
still wrong** (7, 9, 15). This programme's vacuous-test defect has now been caught **nine times**.

### 4a. Kernel — `tests/services/test_period_anomalies.py` [TBD-234a]

Entry points: `find_period_anomalies`, `period_status`.

| # | Ticket | Test | Kind |
|---|---|---|---|
| 1 | 234a | Clean contiguous roster → no anomalies | guard |
| 2 | 234a | The healthy shape `[…closed…, OPEN, stub, stub]` → **no anomalies**. The false-positive fence the boundary model exists to prevent | **fence** — fails if the open row's end is read as unbounded, and fails if an interior open row is mis-paired |
| 3 | 234a | Gap between two **closed** rows → one `gap` with correct bounds | **fence** — fails against revision 1's dead-detector derivation |
| 4 | 234a | Overlap between two **closed** rows → one `overlap` | **fence**, same |
| 4b | 234a | **All-pairs overlap.** The §2.4 fixture `A[2026-01-01→2026-12-31]`, `B[2026-02-01→2026-02-28]`, `C[2026-03-01→2026-03-31]`, all closed → **two** overlaps, `(A,B)` and `(A,C)` | **fence** — red against any adjacent-pair implementation; this is F1's direct fence |
| 5 | 234a | Duplicate open rows → `duplicate_open` naming **both ids** in `period_ids` | **fence** |
| 6 | 234a | Zero open rows → `no_open`; and **no `straddling` marker is computed** and no exception is raised on an org that has closed rows but no open row | **fence** — red against the `AttributeError` path in F2 |
| 7 | 234a | Roster tail (open row, no successor) → **not** a gap | **guard** — ⚠ **relabelled from `fence`.** Both round-2 reviewers found independently that it cannot fail: the tail is `rows[-1]`, so a pairwise iterator never makes it the LEFT member of any pair and no candidate implementation can emit a gap there. The real hazard, an **interior** open row, is fenced by test 2. Kept as a regression net |
| 8 | 234a | **Straddling, non-adjacent, with two open rows.** A straddler separated from the anchor by at least one intervening row → `straddling` **and** `overlap` (§2.4 precedence); the anchor is the MAX-start open row, not the first | **fence** — red against adjacent-pair overlap (the precedence half) and red against first-open-row anchoring (the anchor half) |
| 8b | 234a | Row with `end_date < start_date`, **inserted directly** → `invalid`, and `period_status` returns `invalid` not `upcoming`/`past`; `length_days` is `null` | **fence** — not vacuous: the fixture bypasses every writer §2.3 proves non-inverting |
| 9 | 234a | **Clock independence of the structural set.** See the mechanism note below; this test is load-bearing three times over | **fence** |
| 10 | 234a | `effective_end` matches `period_effective_end` row by row, on a fixture containing **at least one gapped and one overlapped closed row** | **guard** — ⚠ **value stated honestly:** if the implementation honours D5 and calls `period_effective_end`, this compares the helper to itself and can never fail. Its only real target is an in-Python reimplementation. Kept as a regression net against exactly that |
| 11 | 234a | **Status partition** (⚠ **re-homed from the router suite**): an `invalid` row, a `current_by_calendar` row on a lapsed roster, and an open row starting tomorrow each get the documented status from `period_status`, with `today` injected | **fence** |

**⚠ Test 9's fixture is load-bearing, and revision 2's version was VACUOUS.** Recorded in full
because the fix is coupled to F1 and a later revision must not unpick either half.

Revision 2 mandated the open row have `effective_end < today` at both frozen dates, which forces both
dates **above** the derived end `E`. Under the prohibited wiring (`period_spend_window_end`),
`max(E, T) = T` at both dates, so `S1.start = E + 1 <= T` holds at both, giving the structural set
`{overlap(O→S1)}` at both. **Identical. The test goes green against the exact wiring it forbids.**

Reviewer B disputed this, arguing the test passes because "the floored end crosses different stubs, so
the markers differ". **Adjudicated in favour of the finding:** under revision 2's adjacent-pair
semantics `O` is only ever compared to `S1`, so no frozen date can cross a *different* stub. **B's
mechanism only becomes real once F1 (all-pairs) is folded.** The 3↔9 coupling is recorded here
deliberately: **unfolding the all-pairs ruling re-vacuums test 9.**

Second escape, independent of the first: revision 2 left overlap's date semantics unspecified, and a
natural implementation defining the overlapping interval as
`[rows[j].start_date, min(effective_end(i), effective_end(j))]` clamps to `S1.end` at both dates,
producing **identical payloads** and staying green even at full-payload assertion depth.

**Ruling: three belts, all adopted.**

1. **Load-bearing (works at every assertion depth, and under both adjacent-pair and all-pairs):** the
   two frozen dates must **STRADDLE the derived end**, that is `T1 <= period_effective_end(open) < T2`.
   Then the wrong helper yields **no** overlap at `T1` (`max(E,T1) = E`, and `S1.start = E+1 > E`) and
   **an** overlap at `T2` (`max(E,T2) = T2 >= E+1`), so the sets differ and the test is RED. The
   correct helper yields the empty set at both dates and the test is GREEN.
2. **Strengthening:** assert **full marker payloads including dates**, and simultaneously **pin
   overlap's date semantics in §2.5** to `[rows[j].start_date, effective_end(rows[i])]`, the LEFT
   row's end. Pinning the left row's end is what makes the payload assertion a genuine second
   independent red rather than a restatement of belt 1.
3. **Third belt:** assert the **concrete expected value** (the empty structural set at both dates),
   never merely "the two sets are equal". Equality-only assertions are the family this programme keeps
   getting burned by.

The test must also **freeze the clock** rather than only passing a `today=` kwarg:
`period_spend_window_end` defaults `today` to `date.today()` internally (`billing_service.py:600`), so
a kernel that wrongly wires in the floored helper and does not forward `today` is unaffected by a
kwarg alone.

**⚠ Test 10's fixture is load-bearing** for what it is worth (see its honest label). On a clean
contiguous roster `end_date == successor.start - 1` by construction, so the wrong derivation and the
right one agree and the test proves nothing. That is exactly why revision 1's version failed to catch
its own structural defect. The fixture must contain rows where they differ.

### 4b. Endpoint — `tests/routers/test_billing_period_roster.py` [TBD-234b]

| # | Ticket | Test | Kind |
|---|---|---|---|
| 11b | 234b | The route **emits** `status` on every period, with the values `period_status` returns | guard — thin; the partition itself is fenced by test 11 in 234a |
| 12 | 234b | `effective_end` and `counting_through` diverge on a lapsed roster, agree on a converged one | **fence** |
| 13 | 234b | Overlapping periods: one transaction appears in the count of **every** row containing it | **fence** — kills the `CASE` shape |
| 14 | 234b | Count is unfiltered (a transfer leg counts); settled net is filtered (it does not) | **fence** |
| 15 | 234b | A gap between the **out-of-window predecessor** and the lowest displayed row **is reported**, naming the predecessor's id. **Fixture pinned normatively below.** | **fence** — the only assertion that fails if the one-row-older fetch is dropped |
| 16 | 234b | Future stubs render as `upcoming` (no upper bound) | **fence** |
| 17 | 234b | A maximally-lapsed org whose every row predates the lookback still returns rows (D8's floor), and `floored` is `true` with `window.from` at the actual minimum returned `start_date` | **fence** |
| 17b | 234b | An org with **12+ rows newer than a lapsed open row** still returns the open row (D8's open-row union), and `lapsed_open` fires | **fence** — red against revision 2's newest-12-only floor |
| 18 | 234b | `months=0` and `months=999` are clamped, not rejected; past `LIMIT 200`, `truncated` is true, **the open row IS present**, and the surviving rows are the **newest** ones (`window.from` equals the truncated lower bound, not the lookback bound) | **fence** — ⚠ **revision 2's version was PARTIAL**: asserting only `truncated == true` passes against the oldest-200 truncation of F3. Repaired by the three added assertions |
| 19 | 234b | Non-admin → 403 | **fence** |
| 20 | 234b | The route creates **no** `BillingPeriod` on an org with no open row; period count unchanged; response reports `no_open` | **fence** — fails if anyone reaches for `get_current_period` |
| 21 | 234b | Org with zero periods → 200, `periods: []`, `no_open` | **fence** |
| 22 | 234b | `GET /billing-periods` response shape unchanged | guard (regression net; nothing here touches it) |

**⚠ Test 15's fixture is normative, because revision 2's was VACUOUS for TWO independent reasons.**

Reason 1: D8's newest-12 floor pulls the "out-of-window" predecessor **into** the window on any
fixture holding 12 or fewer rows, so the ordinary adjacent-pair loop emits the leading gap with the
one-row-older fetch **entirely removed**.

Reason 2: if the predecessor happens to be the **open** row, its `effective_end` is
`successor.start - 1` by §2.2, so it **abuts by construction** and no gap can ever be reported,
whatever the implementation does.

**Ruling, all three clauses normative:**

- the org must hold **more than 12 periods**;
- the predecessor must be a **closed row with a real `end_date`**, older than **both** the lookback
  bound **and** the 12 newest rows (and, post-D6, older than the truncated bound if truncation is
  also in play);
- the assertion is that the leading `gap` **names the predecessor's id** in `from_period_id`.

### 4c. Frontend [TBD-234b]

| # | Ticket | Test | Kind |
|---|---|---|---|
| 23 | 234b | Page renders under `SettingsLayout` with the Organization tab active, by passing the literal `activeTab="/settings/organization"` | **fence** — ⚠ **promoted from `guard`** (§0.4): red if the page passes `activeTab="/settings/organization/periods"`, which un-highlights every tab |
| 24 | 234b | Anomaly markers render; the overlap note shows when any row overlaps | **fence** |
| 25 | 234b | Both end columns render; the divergence is visually distinguished only when they differ, via `badgeWarning` **plus a text label** (§1.1) | **fence** |
| 26 | 234b | A non-admin deep-linking the page is redirected, matching `settings/organization/page.tsx:106,128` | **fence** |

---

## 5. Rollout

Additive: one new GET, one new page, in that order across two tickets. No existing endpoint, query or
component changes, so nothing that works today can move. Worst case the new page renders wrong numbers
on a surface nobody depended on yesterday.

234a ships alone and is invisible to users: a service helper plus its tests, with no caller. That is
deliberate. It means the kernel contract (§8) is merged, reviewed and frozen before any consumer
exists to constrain it.

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
- The fleet-wide **sweep script** (§1) — follow-up; it calls 234a's kernel.
- Making the open period selectable in the transactions filter (§0.3) — follow-up.
- `GET /billing-period`'s auto-create side effect (D10) — follow-up.

---

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
3. **The status partition was not disjoint**, and reviewer B walked a two-close sequence claimed to
   commit an `end_date < start_date` row. → ordered rules, `invalid` branch, fifth anomaly shape.
   *(The reachability half of this claim was DISPROVED in round 2; see below.)*
4. **`straddling` and `overlap` fire on the same row**, and test 8 asserted the opposite; the
   straddle boundary also drifted from the code's `>=`. → explicit precedence, boundary corrected.
5. **The clock-independence claim contradicted the `lapsed_open` marker.** → structural vs temporal
   marker sets; test 9 rewritten to freeze the clock (a `today=` kwarg cannot catch the failure it
   fences, because `period_spend_window_end` defaults internally).
6. **Test 15 could not fail** — no displayed row's end depends on the out-of-window predecessor. →
   re-targeted at the leading gap marker. The "one row newer" half contradicted D8; deleted.
   *(Still vacuous after this fix; repaired properly in round 2.)*
7. **D7's mandated predicate defeated both indexes it named.** → per-column shapes; plain
   `settled_date BETWEEN` for settled net, `UNION ALL` or an explicitly-accepted scan for the count.
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

**Sign-off round 2 — revision 2: REJECT / REJECT** (two independent reviewers, 2026-07-29). Four
blocking findings, plus a scope ruling. **As in round 1, no design ruling was overturned.** The
two-ends display, the server-canonical status, the kernel-as-deliverable, admin gating, the
no-`get_current_period` rule, the all-of-it-is-additive rollout: all survived a second independent
pass unchanged. Only mechanics failed, again.

**Scope: ruled L, not M, and SPLIT.** Both architects ruled the ticket too large to review or ship as
one unit. The split (kernel first, route and page second, never in parallel) is adopted and recorded
in §8.

**The unanimous blocker.** Exactly one finding was reached **independently by both reviewers**: F3,
`LIMIT 200` with no stated truncation direction, which composes with §2.5's ASC ordering to return
the **oldest** 200 rows and silently discard the open row, every stub, and every recent anomaly, on
the orgs large enough to trip the cap. → D6 now pins newest-first truncation, `window.from` reports
the truncated bound, the predecessor fetch is relative to it, and test 18 gained three assertions.

**The four blocking findings and their dispositions:**

| # | Finding | Disposition |
|---|---|---|
| F1 | §2.4's overlap rule was adjacent-pair (via `_next_period_start`, `billing_service.py:425-431`), so a fully-nested row emits no marker and renders clean; it also made §2.4's own straddling/overlap precedence unachievable | **ADOPTED.** gap stays adjacent-pair, overlap becomes **all-pairs**, both domains normative. ≤19,900 comparisons at the cap. Test 4b added; test 8 re-fixtured non-adjacent |
| F2 | §2.4's `straddling` anchor was undefined for zero or multiple open rows: two spec-conformant answers on the same input, and `AttributeError` → **500 on an org with no open row** | **ADOPTED.** Anchor is the MAX-start open row (what `get_current_period` selects and `_apply_close_step` evaluates); with zero open rows the marker is not computed. Test 6 and test 8 extended |
| F3 | `LIMIT 200` truncation direction unstated (**unanimous**) | **ADOPTED.** See above |
| F4 | Test 9 was vacuous as fixtured, and its fix is **coupled to F1** | **ADOPTED, three belts.** Straddle the derived end; assert full payloads with overlap's date semantics pinned to the LEFT row's end; assert the concrete expected value, not equality |
| F5 | Test 15 was vacuous for **two independent reasons** (the newest-12 floor pulls the predecessor into the window; an open predecessor abuts by construction) | **ADOPTED.** Fixture pinned normatively; §2.2 clarified that "one row older" means older than the **lowest row actually returned** |

**The test-9 dispute, and how it was adjudicated.** Reviewer A filed test 9 as blocking; reviewer B
disputed it, arguing the test does fail against the wrong helper because "the floored end crosses
different stubs, producing different markers". **Adjudicated in favour of A.** Under revision 2's
adjacent-pair semantics the open row is only ever compared with its immediate successor, so no frozen
date can cross a *different* stub, and the mechanism B described does not exist in revision 2. **B's
mechanism becomes real only once F1 is folded** — which is why §4 records the 3↔9 coupling
explicitly: unfolding all-pairs would re-vacuum test 9.

**⚠ Both reviewers were WRONG about the DB CHECK (C1).** Both filed a non-blocking correction
claiming the SETTLED-implies-settled_date invariant "is code/ORM-enforced only, there is no DB CHECK".
Verified against the repo: migration `036_settled_implies_settled_date.py` adds a real CHECK named
`ck_transactions_settled_implies_settled_date` (`:55-56`), mirrored at flush time by a
`before_insert`/`before_update` listener (`backend/app/models/transaction.py:168-186`). **D7's
conclusion stands and is now more strongly justified**; only its citations were wrong and are fixed.
Reviewer B's derived warning ("a non-ORM write path can produce a settled row with NULL
`settled_date` that `settled_net` silently drops") is **false** and is recorded as false in D7 so it
cannot resurface.

**⚠ One reviewer's round-1 claim was DISPROVED in round 2 (C2).** Round 1 item 3 credited reviewer B
with a two-close sequence that commits an `end_date < start_date` row. `close_period` rejects
`requested < current.start_date` at `billing_service.py:1034-1039`, and an enumeration of every
`end_date` writer (§2.3) shows each is provably non-inverting. **`invalid` is unreachable through
shipped code.** The branch and test 8b are **kept**, rewritten as **defensive** against direct DB
edits, operator prod access and future writers, since `models/billing.py:12-14` carries no CHECK.
Round-1 item 3 above is corrected accordingly.

**Also folded from round 2 (non-blocking):** clock resolved once in the route (D8a, mandated by
`period_spend_window_end`'s own docstring at `billing_service.py:572-577`); D8's floor widened with
the open-row union so the anchor is always present; `floored` added to `window`; §2.4 rules 1 and 2
rewritten in terms of `effective_end(row)` (the literal `end_date` gives `None + timedelta` →
`TypeError`); `length_days` is `null` on an `invalid` row; the "should not 422 on a URL typo" claim
**dropped** as unachievable under FastAPI's `int` coercion and narrowed to "out-of-range integers are
clamped"; D6's query budget stated at ~400-600 round trips with DO's request timeout noted, plus
reviewer B's single-`JOIN` alternative recorded as a measurable option, not mandated; the design-system
rulings (`badgeWarning` plus a text label instead of `text-accent`, which would break The One Brass
Rule; kind on text/icon, colour for severity only; and §1.1's layout intent, since a nine-column grid
is `DESIGN.md`'s explicit anti-reference); §0.3's deep link pinned to the `counting_through` window
with the localStorage side effect recorded; §0.4's `SettingsLayout` mechanism corrected (`activeTab`
is a **prop**, not `usePathname()`) with test 23 promoted to `fence`; test 7 demoted to `guard`
(both reviewers found independently that it cannot fail); test 10 relabelled `guard` with its value
stated honestly; D7 gained the `_apply_transaction_filters` citation
(`transaction_service.py:1994-1997`) that makes the `UNION ALL` exactly the click-through set; and
four file-path / line-number corrections (`frontend/components/SettingsLayout.tsx`,
`frontend/app/forecast-plans/ForecastPlansClient.tsx`, `billing_service.py:227-232`,
migration `020:21-24`, `transactions/page.tsx:301`).

**Running tally.** This programme's vacuous-test defect (a test green against unmodified `main` while
claiming to fence the fix) has now been caught **NINE times**: six across TBD-232/239/241/240, and
three more here (tests 7, 9 and 15). Three of those three were in a revision whose own §4 opens with a
warning about the pattern. `reference_vacuous_test_pattern.md` stands: **revert the fix and confirm
red is the only reliable gate.**

**Sign-off round 3 — revision 3:** _(pending)_

---

## 8. The split and the frozen kernel contract

**Ruled by two independent architects in round 2: TBD-234 is effort L and must ship as two tickets.**

| | TBD-234a — the kernel | TBD-234b — the route and page |
|---|---|---|
| **Deliverables** | `find_period_anomalies`, `period_status`, the `anomalies` marker payload schema | the route, D6/D7/D8/D8a windowing and aggregates, D9 gating, the §2.5 response body, the page |
| **Sections** | §2.2, §2.3, §2.4, §2.5's marker schema, D2, D4, D5 | §1.1, §2.1, §2.5 (everything else), D1, D3, D6, D7, D8, D8a, D9, D10 |
| **Tests** | 1-11 | 11b-26 |
| **Contains** | no route, no page, no windowing, no aggregates | no changes to the kernel |
| **Order** | ships FIRST | opens only AFTER 234a merges |

**⚠ Never run the two in parallel.** 234b's tests consume 234a's contract as frozen. Parallel work
against an unfrozen signature is exactly how a contract drifts, and a drifted kernel signature is
what forces a rewrite.

**Two amendments to the cut, both ruled in round 2:**

1. **Test 11 (the status partition) re-homes to 234a**, as a service-level test of `period_status`
   with `today` injected. 234b keeps only test 11b, a thin assertion that the route emits the field.
   The partition is a kernel fact and should not first be tested through an HTTP round trip.
2. **§2.5's `anomalies` marker schema belongs to 234a, not 234b.** It is the kernel's **output**
   contract. Putting it in 234b would mean the kernel merges without its own output shape specified,
   and 234b would then be free to reshape it.

### 8.1 The frozen kernel contract

All four items below are **frozen by 234a** and 234b consumes them unchanged.

**1. Signature.**

```python
def find_period_anomalies(
    periods: list[BillingPeriod],
    *,
    open_row_ids: list[int],
    today: datetime.date,
) -> list[Anomaly]:
    ...
```

It is **PURE** over a caller-supplied list **already ordered `start_date` ASC**, with the
out-of-window predecessor (§2.2) included as the list's **first element and flagged
non-displayable**. **It must NOT fetch rows itself.** A kernel that queries cannot be tested without a
route, cannot be reused by the sweep script (§6), and cannot be given a synthetic corrupt roster.

**2. `no_open` and `duplicate_open` derive from `open_row_ids`, never from `periods`.** They are
**org-wide** facts (D8) and the window can legitimately omit an open row's siblings. The caller
supplies the ids. ⚠ This also corrects revision 2's D8 wording, which said a cheap `COUNT(*)`: it must
be **ids, not a count**, because §2.5 emits `period_ids` on both markers and a count cannot produce
them.

**3. `today` is injectable and required.** Demanded by the temporal marker set (§2.4) and by test 9,
which cannot fence the prohibited helper without it. No `date.today()` inside the kernel. The route
resolves the clock once and passes it down (D8a).

**4. The marker payload schema is frozen in 234a** (§2.5's `anomalies` array), including overlap's
pinned date semantics `[rows[j].start_date, effective_end(rows[i])]`.

**Rationale, stated because it is the whole point of freezing them.** Without these four, 234b would
be forced to **rewrite the kernel**: a fetching kernel cannot be windowed by the route, a `periods`-derived
`no_open` is wrong the moment the window narrows, a `date.today()` kernel cannot be clock-injected,
and an unspecified payload gets reshaped by whoever renders it. A forced downstream rewrite of an
already-merged unit is precisely the failure mode the architects used to **reject TBD-233**. Repeating
it inside this ticket's own split would be indefensible.
