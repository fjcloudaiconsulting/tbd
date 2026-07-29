# TBD-234 — read-only billing period roster, and the anomaly kernel

Status: REVISION 7 — 234a SHIPPED (PR #590); 234b ready to build
Date: 2026-07-29
Jira: TBD-234 (Story, child of TBD-213) — **re-scoped to effort L and SPLIT in two.** **Blocks TBD-233 / TBD-242.**

⚠ **234a is effort-s, not effort-m** (round 4, correction C2). A reviewer built the kernel and 12 of
its 14 tests inside the review: roughly 130 lines of pure code (the dataclasses, the helpers and one
SELECT), with the tests reusing an existing fixture block. The house-style docstrings are the largest
single cost. 234b's sizing is unchanged.
Predecessors, all merged: TBD-232 (#586), TBD-239 (#587), TBD-241 (#588), TBD-240 (#589)
Related specs: `2026-07-28-open-period-spend-window-design.md`, `2026-07-28-close-period-chain-close-design.md`, `2026-07-28-billing-period-boundary-integrity.md`, `2026-07-27-billing-period-truth-and-safety.md`

**This one document covers two tickets.** Round 2 sign-off ruled the work **L, not M**, and ruled it
must split:

- **TBD-234a — the kernel. ✅ SHIPPED.** `load_complete_roster`, `find_period_anomalies` and the §2.3
  status partition as a `period_status` helper, all in `billing_service.py`. Tests 1-14b. **Merged as
  `15faa922` (PR #590).** Every section labelled **[234a]** below describes CODE THAT EXISTS; read it
  as a record, never as work to do.
- **TBD-234b — the route and the page.** Fetching, aggregates, display windowing, gating, the
  response contract, the page. Tests 15-34. **This is the only buildable half.**

Every deliverable below is labelled **[234a]** or **[234b]**. §8 records the split and freezes the
kernel contract that 234b consumes.

Revision 1 was **REJECTED 2-0** on nine blocking findings. Revision 2 was **REJECTED 2-0** again on
four more. Revision 3 was **REJECTED 2-0** a third time, and this time the frozen kernel contract
itself was found **unbuildable** by both reviewers independently. Revision 4 was **REJECTED 2-0** a
fourth time, by a round in which one reviewer **built the kernel and ran this document's own tests
against it**. Full record in §7.

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

**Revision 5 is a targeted fold, not a restructure. No design ruling changed, and neither round-4
reviewer challenged one.** Round 4's five blocking findings were all mechanical: a `straddling`
predicate that cannot be built as written (§2.4), a fence label that was demonstrably false (test 2),
a completeness precondition guarded by a tool this repo does not have (§2.2), an unfenced
reimplementation path for the wrong end semantics (test 11 clause (e)), and two markers with no
rendering home (§1.1). Two corrections to statements this document had been repeating since revision 1
are folded alongside them (§0.1 C1, and the effort label above).

**Revision 6 folds the PR-review findings on 234a's implementation (PR #590), audited independently
by three reviewers.** No design ruling changed here either; what changed is that several of THIS
DOCUMENT'S claims were shown to be empirically false and are corrected in place:

- **§4a's claim that test 11 clause (e) is "F4's only fence inside 234a" is FALSE** and is corrected
  in §2.4 and §4a. The actual fences are test 2 and test 14a's signature assertions (§4a, finding
  R-S1).
- **Test 2's fixture alternative silently killed a fence** and is deleted; test 2 is relabelled
  `fence` (§4a, R-S2).
- **Four test gaps, each proven by a GREEN defect injection**, are recorded with their injections and
  closed by new tests 13c, 13d, 13e and by fixture corrections to tests 14 and 14a (§4a, R-S3).
- **§2.4a's emission-ceiling payload could not carry information** (`emitted_count` was always the
  cap); it is now `overlap_count`, and the ceiling's boundary direction is pinned like the analysis
  cap's (§2.4a, §2.5).
- **§2.5's ordering claim of totality was false** as implemented and is now true by construction
  (§2.5).
- **§2.5 and §8.1 contradicted each other** over who owns the ordering ruling; resolved in favour of
  §8.1 (§2.5).
- **§8.1's freeze omitted four shipped symbols and `period_status`' signature**; both added.

**Revision 7 readies 234b for build, and it changes only 234b sections.** A readiness round run
against the MERGED kernel found **five blocking defects, all of them in this document, all of them
before a single line of 234b existed**. None is a design reversal; each is a place where the spec
mandated something unbuildable, self-contradictory, or satisfiable three incompatible ways:

- **B1 — D7's aggregate window had no bounds and the tail row had no upper bound.** Both columns now
  bound on `[start_date, counting_through]`, matching §0.3's deep link, one-sided when
  `counting_through` is `None` (D7).
- **B2 — D6 said "one fetch, sliced in Python" and then described two SQL sentences from revision 4.**
  The SQL sentences are deleted and a query-counting fence (test 32) replaces the prose.
- **B3 — test 16's mechanism 2 was RED against a CORRECT implementation**, and its mechanism 1 became
  vacuous the moment 234a merged. Both rewritten (§4b).
- **B4 — `counting_through`'s computation path was unspecified** and the shipped signature admits
  three incompatible answers, one of which reintroduces B2's two-sources problem (§2.1).
- **B5 — D8's lookback named neither a column nor an anchor** (D8).

**Revision 7 also folds the 234b design pass** into §1.1, replacing the placeholder layout intent.
The page is a rail, not a grid; the rulings and their constraints are recorded there, deliberately
without wireframes.

⚠ **Line citations in this document are computed against `main` as it stood BEFORE 234a merged.**
234a adds two imports and eleven docstring lines above `period_effective_end`, shifting every
`billing_service.py:NNN` citation below it by **+13**. Resolve citations **by symbol name**, which is
shift-proof; the numbers are kept only because §7's round records quote them. The kernel's own
in-file citations were converted to symbol references in the fold, for exactly this reason.

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

Today the fleet's roster health is observable only by grepping structlog.

⚠ **Correction C1, round 4. Revisions 1 through 4 all said #589 shipped `period_effective_end` "with
no production caller". That is FALSE, and it had been repeated unchallenged since #589 merged.**
The helper is called at `billing_service.py:594` by `period_spend_window_end`, which has six live
production call sites (`budget_service.py:142,194,242,355`, `forecast_plan_service.py:322`,
`budget_rebalance_service.py:540`, all verified). **It has never been at prune risk.** What its
docstring at `:483-490` actually claims is narrower: no caller *other than* `period_spend_window_end`
and the tests, with TBD-234 named as the consumer that would call it **directly**. Under revision 4's
subtraction 234a does not call it directly, so residual R1 (updating that docstring) still stands, but
**R1's replacement text must not restate the false premise**; see §2.2.

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

**[234a] — ✅ ALL SHIPPED in `15faa922` (PR #590). Nothing in this list is open work.**
- **`load_complete_roster`** in `billing_service.py` — the kernel's ONLY constructor (§2.2).
- **`find_period_anomalies`** in `billing_service.py` — **the named backend deliverable**, not an
  implied UI feature. Both merged specs already assign it here
  (`2026-07-28-billing-period-boundary-integrity.md:298`). Testable without the route.
- **`period_status`** in `billing_service.py` — the §2.3 partition, as a pure helper.
- **`kernel_derived_end`** in `billing_service.py` — ⚠ **added in revision 5.** The §2.2 derivation, as
  a **module-level** pure function `kernel_derived_end(roster, i) -> datetime.date | None`. Rounds 1
  through 4 named it only as `effective_end(rows, i)` inside prose, yet **test 11, 234a's flagship
  fence, asserts on it by name**. Written as a closure inside `find_period_anomalies` it is not
  reachable from a test, so the fence is unwritable. It is a named deliverable and is frozen in §8.1.
- The `PeriodAnomaly` type and its marker payload schema (§2.5), the kernel's output contract.
- **The `CompleteRoster` AST guard test** (§2.2 enforcement), the precondition's only mechanism.
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

### 1.1 The page [234b]

§2.5's payload maps 1:1 onto a nine-column table, and that is the shape an implementer will reach for
by default. **It is forbidden.** `DESIGN.md` names it as an explicit anti-reference ("if a screen
reads like Google Sheets in a wrapper, redesign before shipping") and it violates `PRODUCT.md`'s
*hierarchy-without-grids*.

**Revision 7 replaces revision 6's placeholder intent with the design pass's rulings**, run against
`PRODUCT.md`, `DESIGN.md`, `frontend/lib/styles.ts` and `frontend/components/SettingsLayout.tsx`.
What follows is normative and deliberately compressed: the rulings and the constraints, not the
wireframes.

**The page is a RAIL, not a grid.** One vertical hairline; each period hangs a node on it; **exactly
one alignment axis, not nine**. Row heights are deliberately non-uniform, a healthy row short and a
broken row tall, so roster health is legible from the silhouette alone. **No column headers** (every
value is self-labelling), no zebra striping, no cell borders, and **no `overflow-x-auto` anywhere**.
If an implementer adds horizontal scrolling, the design has been rebuilt as the table this section
forbids.

**A gap renders as a BREAK IN THE RAIL**, an interstitial `<li>` between the two named rows with the
spine visibly stopping and restarting, never as a badge on a row. `gap` is the only adjacent-pair
marker (§2.4), which is what makes it the one marker expressible as geometry.

**Three cards, all `<h2>`** (the layout owns the single `<h1>`): **"Roster health"** (the verdict, the
guarantee sentence, and a roster-facts `<dl>`), a conditional **"Issues not shown on the timeline"**
band, and **"Timeline"**.

⚠ **Inline-on-the-row has no story for off-window markers, and under org-wide analysis those now
exist.** A marker can name a period id the display window does not show (§2.5's
`referenced_periods`). **Ruling: a summary band sits above the timeline** ("3 issues elsewhere in
your roster"), rendering the referenced periods for every marker carrying `off_window: true`. This is
a small additive requirement on the design pass, not a redesign.

⚠ **Round-4 finding F5: two markers had no rendering home at all, and one of them is the marker D10
and test 24 exist to produce.** Revision 4 gave markers exactly two homes, inline on the row they
concern and the summary band, and scoped the band to `off_window: true`. §2.5 defines `off_window` as
"true when any id it references is absent from `periods`". **`no_open` carries `period_ids: []` and
`overlap_analysis_skipped` carries no ids at all, so both evaluate `off_window: false` vacuously**,
and neither concerns any row. Attack: an org with 400 periods, all predating the lookback, none open.
The route returns `periods: []`, `roster.period_count: 400`, `anomalies: [{"kind": "no_open",
"period_ids": []}]`. The timeline is empty, the band filters to nothing, and **the page renders "no
issues" under copy reading "Checks cover your entire roster."** The same erasure hits
`overlap_analysis_skipped` on every roster past the cap.

**Ruling: there are THREE marker classes, not two, and the third is normative.**

| class | markers | home |
|---|---|---|
| **row-scoped** | `gap`, `overlap`, `inverted`, `straddling`, `lapsed_open`, `duplicate_open` | inline on the row(s) they name; in the summary band when `off_window: true` |
| **off-window** | any row-scoped marker whose `off_window` is `true` | the summary band, rendered from `referenced_periods` |
| **roster-scoped** | `no_open`, `overlap_analysis_skipped`, `overlap_emission_capped` | ⚠ **the summary band, ALWAYS, independent of `off_window`.** They describe the roster, not a row, so there is no row to sit on and no id to be off-window |

Test 30 fences the first two classes and is extended in revision 5 to fence the third.

⚠ **The three roster-scoped kinds NEVER consult `off_window`, and revision 7 states the mechanism
rather than the intent.** `off_window` is vacuously `false` on all three (§2.5), so an implementer
writing `anomalies.filter(a => a.off_window)` for the band **erases `no_open` on the exact org this
page exists for**. **Normative: the band is driven by an explicit `ROSTER_SCOPED` set of the three
kinds, unioned with the `off_window === true` markers. Never a truthiness check.**

**Heading level, ruled explicitly so the design cannot add a second `<h1>`:** `SettingsLayout`
already renders the route's only `<h1>` (`frontend/components/SettingsLayout.tsx:46`). All three
cards' headings are `<h2>` siblings, never an `<h1>`.

#### Severity, and where colour is allowed to mean anything

**Four tiers over four variants. `badgeSuccess` is excluded from markers entirely**, because no
marker is good news:

| tier | variant | kinds |
|---|---|---|
| error | `badgeError` | `inverted`, `overlap`, `duplicate_open` |
| warning | `badgeWarning` | `gap`, `lapsed_open`, `no_open`, `overlap_analysis_skipped` |
| info | `badgeInfo` | `straddling` |
| neutral | `badgeNeutral` | `overlap_emission_capped` |

**Colour carries severity and nothing else. The KIND rides a mandatory, never-abbreviated text
label**, plus **one icon per tier** (four glyphs, not nine). If a reviewer insists on the literal
nine-kinds-nine-signals reading, the recorded escalation is **per-kind icons over the same four
colour pairs**, never a fifth colour: `frontend/scripts/check-design-tokens.sh` CI-blocks an invented
hue, and there is no unused semantic token left to take.

⚠ **`overlap_analysis_skipped` is WARNING, not neutral.** It means the overlap check did not run,
which is the one condition that falsifies the guarantee sentence. ⚠ **`overlap_emission_capped` is
NEUTRAL**: detection was complete and only the listing is truncated, so nothing is unknown.

**Unknown marker kinds render as `badgeNeutral` carrying the raw `kind` string. Never dropped
silently** (§2.5 already requires clients to tolerate unknown kinds).

#### The two empty states, and why conflating them is a bug

| state | trigger | copy |
|---|---|---|
| **(a) empty roster** | `roster.period_count === 0` | "No billing periods yet." |
| **(b) empty window** | `period_count > 0` **and** `periods: []` (D8's accepted consequence) | "None of this organization's 400 periods start in the last 12 months. Widen the window to see them. **Every check above still covered all 400.**" |

That last sentence is the guarantee doing real work. Without it, state (b) reads as a broken page on
precisely the maximally-lapsed org D8 accepts it for.

⚠ **The guarantee sentence is SWAPPED when `roster.analyzed === false`**, to say the overlap check
was skipped. The unconditional version is a lie in that state, and it is the sentence this page's
credibility rests on.

#### Brass budget, and the divergence treatment

**Exactly one brass moment on the page: the filled node plus the word "Open" on the ANCHORED open
row** (the greatest `start_date` among open rows, matching `get_current_period`'s
`order_by(start_date.desc())` at `billing_service.py:82` and the kernel's straddle anchor). Under
`duplicate_open` the other open rows get `badgeError`, **not brass**. **No `bg-accent-dim` row tint**:
it is a second brass surface, and it would change the backdrop under every muted string on that row.

⚠ **A structural fact that pins the divergence treatment, verified in the merged code and never
stated before revision 7:** `period_spend_window_end` returns a **closed** row's end verbatim and
applies the `max(end, today)` floor **only when `end_date IS NULL`** (`billing_service.py:607-614`).
**Therefore `effective_end != counting_through` can only ever occur on an OPEN row**, and under
`duplicate_open` on several. That is why `text-accent` was the wrong reach for divergence in
revision 2: three lapsed open rows would put brass in three places, breaking **The One Brass Rule**.

- **Converged rows** render both facts on ONE line in **identical** styling: "Period ends X ·
  Counting through X". The repetition IS the "these agree" signal and **must not be collapsed into a
  fused label**.
- **Diverged rows** move the second fact onto its own line in `badgeWarning`, **with the divergence
  stated in words inside the chip** ("Counting through {date}, past this period's end"), plus an
  explanatory sentence. Three independent non-colour signals: position, wording, and the chip.

**`settled_net` is NOT colour-coded.** Signed, `tabular-nums`, muted. On this page colour means
severity and nothing else.

**Deep link:** the `counting_through` window per §0.3, **omitted entirely when `counting_through` is
null**. The copy must state that opening a period in Transactions **replaces the user's saved
transaction filters** (§0.3's recorded localStorage side effect); the link is not read-only and the
page must not imply it is.

#### The one new primitive, and nothing else

**One addition to `frontend/lib/styles.ts`: a `warning` banner**, same construction as the shipped
`error` banner (`frontend/lib/styles.ts:57-58`, verified as
`"rounded-md bg-danger-dim px-4 py-3 text-sm text-danger"`):

```ts
export const warning =
  "rounded-md bg-warning-dim px-4 py-3 text-sm text-warning";
```

Both tokens already exist (`--color-warning`, `--color-warning-dim` in `frontend/app/globals.css:169-171`),
so `check-design-tokens.sh` stays green. **Verify the pair at AA in BOTH themes before merge.** **No
other new primitive, no new token, no new colour.**

#### Accessibility

- **`<ol aria-label>` of `<li>`. NOT table or grid ARIA roles**, which would reimpose the shape §1.1
  forbids at the semantic layer.
- **Rows are not headings** (200 rows would be 200 headings). `sr-only` per-row headings are the
  compliant escalation if row jumping is ever wanted.
- **Every chip carries an `sr-only` "Issue: " prefix**, so it is not heard as another metadata value.
- **Icons are always `aria-hidden`.**
- **`role="status" aria-live="polite"` on the WINDOW CAPTION ONLY.** The verdict does not change with
  the window, so live-regioning it would announce an unchanged sentence on every interaction.
- **Never colour alone, anywhere: a monochrome screenshot must lose no signal.**

#### Explicitly rejected, with reasons, so a later round does not re-propose them

- **Proportional Gantt bars on a time axis.** Rosters span years while periods are monthly, so the
  interesting rows compress to nothing; gaps and overlaps would be encoded by geometry alone; and it
  drags a diagnostic page into the dataviz register.
- **Any repair affordance.** TBD-235 owns repair, D11 forbids writes, and the page's own copy says it
  reads only.
- **Sort, filter or search controls.** Sorting off chronological order destroys the adjacency that
  makes a rail break legible, which is the page's single strongest signal.

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

**Ruling: render both, explicitly labelled.** "Period ends" (derived) and "Counting through" (spend
window), differentiated only when they diverge, per §1.1's divergence treatment. Ship one number and
this page becomes the one that proves the app is lying; ship both and the divergence *is* the
diagnostic. This is a deliberately-accepted residual of TBD-240 §7, surfaced rather than introduced.

⚠ **Divergence is possible on OPEN rows ONLY** (§1.1, verified at `billing_service.py:607-614`): a
closed row's end is returned verbatim before any floor, so `effective_end == counting_through`
identically on every closed row. A design or a test that expects divergence on a closed row is
testing something that cannot happen.

#### ⚠ B4 — how `counting_through` is computed, pinned in revision 7

Revision 6 named the field and never said how the route obtains it, and the **shipped** signature
admits three incompatible answers:

```python
async def period_spend_window_end(db, org_id, period: BillingPeriod, *, today) -> datetime.date | None
```

The annotation says `BillingPeriod`, but the body reads only **`.end_date`, `.start_date` and
`.id`** (`.id` solely in the unreachable inversion `RuntimeError`'s message), so a `RosterRow`
duck-types perfectly. The three answers, and why two are wrong:

| path | verdict |
|---|---|
| a second `select(BillingPeriod)` to re-materialise ORM entities | **REJECTED.** It reintroduces D6's two-sources problem, and it can 500 or silently drop a row under a concurrent delete |
| pass the `RosterRow` as-is | **works, but violates the annotation and is unpinned** |
| inline `max(end, today)` in the route | **REJECTED.** A third copy of the floor, against the boundary model's "two derived-end helpers, never collapse them" |

**Ruling: widen `period_spend_window_end`'s type annotation to accept either** (a small structural
`Protocol`, or `BillingPeriod | RosterRow`), **pass the roster row**, and **add a docstring note
naming the three attributes the body reads**, so a future edit that reaches for a fourth attribute
knows it is breaking a contract. Fenced by test 32's query counter (D6), which goes red against the
rejected second `SELECT`.

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

def kernel_derived_end(roster: CompleteRoster, i: int) -> datetime.date | None:
    ...  # pure, sync, module-level. The derivation below, and NOTHING else.
         # Module-level because test 11 asserts on it by name (§1, revision 5).

def find_period_anomalies(roster: CompleteRoster, *, today: datetime.date) -> list[PeriodAnomaly]:
    ...  # pure, sync, no DB
```

#### The derivation, and why it is a PROOF rather than a hope

`BillingPeriod` carries `UniqueConstraint("org_id", "start_date", name="uq_billing_period_org_start")`
(`backend/app/models/billing.py:12-14`, verified). For a **complete** roster in `start_date` ASC
order with per-org unique starts:

⚠ **"in `start_date` ASC order" is a PRECONDITION of the proof, not decoration, and revision 6 gives
it its own fence (test 14b).** On an unordered list `rows[i+1].start_date` is not
`MIN(start_date) WHERE start_date > rows[i].start_date` and every derived end below is garbage.
Production is MySQL, which guarantees no order without an `ORDER BY`; SQLite in the test suite
happens to return `(org_id, start_date)` order through the unique constraint's implicit index **with
or without the clause**, which is why deleting it was invisible to every behavioural test. See §4a's
R-S3 gap T1.

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

⚠ **Naming, pinned in revision 5: `effective_end(rows, i)` is this document's prose shorthand and
`kernel_derived_end(roster, i)` is the shipped symbol. They are the same function.** Every rule below
that reads `effective_end(rows, i)` compiles to a `kernel_derived_end` call, and the derivation exists
in exactly one place in the code.

This is an **equality, not an approximation**, and it holds **only** under the completeness
precondition. **That precondition replaces the window in the contract.**

#### Enforcement — the load-bearing half, shipped in the SAME PR as the kernel

**A pure kernel can never verify completeness itself.** Completeness is a claim about rows that are
NOT in the list. Purity and self-verification are therefore mutually exclusive: no in-kernel
assertion, length check or invariant guard can work, and anyone proposing one has misunderstood the
shape of the problem.

**Ruling: enforce at CONSTRUCTION.** `CompleteRoster` is a type whose only construction site in
`backend/app/` is `load_complete_roster`. `find_period_anomalies` accepts `CompleteRoster` and nothing
else, so a windowed `list[BillingPeriod]` cannot reach it without someone first building a
`CompleteRoster` out of it. Enforcement collapses to **one construction site**.

⚠ **Round-4 finding F3: revision 4 gated that site on a tool this repository does not have, and the
claim is struck.** Revision 4 said a windowed list was "a **type error**", "auditable by grep **and by
the type checker**", and test 14 called the type checker "the primary gate". **There is no type
checker in this repo, and there never has been.** Verified: `.github/workflows/` has four workflows
and the backend job runs `pytest --splits 4` plus `python -m compileall backend/app` (`test.yml:79`,
`:83`); `grep -niE 'mypy|pyright|pyre|typecheck' .github/workflows/` returns **nothing**;
`backend/requirements-dev.txt` is five lines and carries none of them; there is no `pyproject.toml`,
`mypy.ini`, `setup.cfg` or `.pre-commit-config.yaml` anywhere in the repo. A `@dataclass(frozen=True)`
has a public `__init__`, so both `CompleteRoster(org_id=1, rows=tuple(windowed))` and
`dataclasses.replace(roster, rows=windowed)` succeed at runtime with nothing objecting.

**Compounding, and it disproved the "only constructor" claim inside this document:** 234a's own tests
must construct `CompleteRoster` directly. Test 13 needs a roster past the cap, tests 1-10 need
hand-shaped rosters, and §8.1 advertises the kernel as testable without the route.

**Ruling: the mechanism is an AST GUARD TEST, a pattern this repo already ships twice.** Both
`backend/tests/test_no_raw_request_client.py` and
`backend/tests/auth/test_sessions_invalidated_at_allowlist.py` `ast.parse` every `.py` under
`backend/app/` and fail on a forbidden construct (the #552 pattern, verified: both use `import ast`, a
`BACKEND_APP.rglob("*.py")` walk and an `_enclosing_function` parent stack).

**234a ships one:** an AST guard asserting that within `backend/app/`, a `CompleteRoster` is
constructed **only** inside `load_complete_roster`. ⚠ **The scan is source-scoped to `backend/app/`,
so tests are exempt by construction**, which is also what resolves the contradiction above:
hand-shaped kernel fixtures are legal precisely because they are not production code. Test 14
carries the guard and is a **fence**, not a guard label, because it fails against an implementation
that constructs a `CompleteRoster` at a second site.

⚠ **Revision 6 (R-B1): "a call node NAMED `CompleteRoster`" is too narrow, and shipping it that way
left the guard green against three real construction shapes** — including `dataclasses.replace`,
which the paragraph above names in the same breath as the risk. **Normative: the guard matches four
SHAPES** — direct, `ImportFrom`-aliased, `dataclasses.replace` with a positional first argument, and
class-object indirection (`__class__`, `type(...)`) — **and it enumerates what it does not cover
rather than claiming completeness.** Detail and the proving injections in §4a's R-B1 note. It also
carries a **positive control**, because a detector that fires on nothing is green, and green is what
this guard looked like.

**This is NOT the rejected two-unit fix in disguise.** `load_complete_roster` **fetches rows**; it
does not **compute ends**. `period_spend_window_end` remains **structurally unreachable everywhere in
234a**, so the prohibition-by-construction property that #589 bought is fully intact.

Three amendments, all normative:

1. **`CompleteRoster` holds row tuples, not ORM entities.** `RosterRow = (id, start_date, end_date)`,
   decoupled from the model, so an unbounded fetch on a 10k-row org is trivial. Test 11 alone needs
   real `BillingPeriod` instances and may hold both.
2. **Belt [234b]:** a contract test asserting the route's roster length equals
   `SELECT COUNT(*) WHERE org_id = ?` (test 22). The AST guard is the braces; this test is the belt.
   ⚠ **Revision 5 pins the identity that makes the belt bite:** `roster.period_count ≡
   len(roster.rows)`, normative in §2.5. Without it test 22 passes when the route serves
   `period_count` from a separate `SELECT COUNT(*)` while the kernel received a windowed list, which
   is F3's exact attack surviving its own belt.
3. **Residual R1, must not be missed [234a]:** `period_effective_end`'s docstring
   (`billing_service.py:483-490`, verified) currently names TBD-234 as its consumer and says "Do not
   prune this as dead code". Under the subtraction **234a does not call it directly** (only test 11
   does). **Update that docstring in 234a.** ⚠ **The replacement text must NOT repeat §0.1's corrected
   claim.** It must say what is true: the helper is **reachable in production transitively via
   `period_spend_window_end`** (`billing_service.py:594`, six live call sites), and it is **directly
   exercised by 234a's test 11 as a differential oracle** against `kernel_derived_end`. The
   "do not prune / do not collapse the two helpers" instruction stays; only its false premise goes.

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

The predicate, with the anchor pinned **and with revision 5's two exclusions, which are normative**:

```
straddling(rows, i) = rows[i].start_date <= anchor.start_date
                      AND kernel_derived_end(rows, i) >= anchor.start_date

evaluated ONLY for every  i != anchor_index
                    where  kernel_derived_end(rows, i) is not None
```

⚠ **Round-4 finding F1, found INDEPENDENTLY BY BOTH REVIEWERS, one of whom proved it by building the
kernel: revision 4's predicate could not be implemented as written.** It quantified over every `i`
with no self-exclusion and no `None` guard, and produced two wrong outputs. A literal implementation
failed **8 of the 12 kernel tests** the reviewer wrote; the two exclusions above turned that into
12 of 12, and 16 of 16 with the DB-backed tests.

- **(a) Self-straddle.** `anchor.start_date <= anchor.start_date` is trivially true, and for an
  interior open anchor `kernel_derived_end = successor.start − 1 >= anchor.start_date`. So on this
  document's **own** healthy shape `[…closed…, OPEN, stub, stub]` a conformant implementation emits
  `straddling(period_id=anchor, anchor_period_id=anchor)`, and **test 2, the healthy-shape check, goes
  RED against a correct implementation.** The spec was internally unsatisfiable.
- **(b) `TypeError` → 500.** On `[…closed…, OPEN]` with the open row as the tail, which is **the
  commonest roster in the fleet** because nothing on the read path materialises stubs,
  `kernel_derived_end(anchor)` is `None` and the predicate evaluates `None >= date(...)`. ⚠ **This is
  round-2 finding F2 regressed into a new form on a more common roster**, and it regressed because
  revision 4 narrowed §2.4's `None` ruling to "**pairs**" while `straddling` is not a pair rule, so
  the guard never reached it.

**Production precedent, and it is exact.** `_apply_close_step`'s own straddle query carries
`BillingPeriod.id != current.id` at `billing_service.py:774`. The shipped code has always excluded
the anchor from its own straddle set; only this document failed to write it down.

⚠ **Citation correction.** Revisions 1 through 4 cited `_apply_close_step`'s predicate as
`billing_service.py:776-777`, which is only the `>=` bound pair. **The full predicate is `:772-779`**
and carries **both** `id != current.id` (`:774`) and `end_date IS NOT NULL` (`:775`) alongside the two
bounds. The two clauses revision 5 adds are the two clauses the citation was truncating away.

⚠ **`>=`, at or after — matching `_apply_close_step`'s bound pair at `billing_service.py:776-777`.**
Revision 1 said "ends after it", which would silently under-report exactly the shape whose deferral
(`:914-922`) created this marker.

**Marker precedence, normative.** A straddling row that is not the anchor is also an overlap under
rule 2. **Ruling: a row may carry multiple markers, and `straddling` is emitted *in addition to*
`overlap`, not instead of it.** Suppressing the overlap would hide genuine overlaps on any roster
containing a straddler, precisely the rosters this page targets. Test 9's fixture pins a
**non-adjacent** straddler with a normative marker-id assertion, so the ruling is fenced against a
regression to adjacent-pair semantics.

⚠ **Revision 4 stated this as "a straddling row is by construction also an overlap under rule 2",
without the qualifier, and that was FALSE for the self-straddle case:** rule 2 quantifies over `i < j`
and therefore never pairs a row with itself, so the self-straddler had no corresponding overlap. **That
inconsistency is internal evidence self-exclusion was always intended and simply never written.**

#### Two output sets, because one of these is not clock-free

| set | markers | clock |
|---|---|---|
| **structural** | gap, overlap, duplicate_open, no_open, inverted, straddling | none |
| **temporal** | lapsed_open (derived end in the past) | reads the injected `today` |

Revision 1 put `lapsed_open` in one undifferentiated set and then asserted the whole set was
clock-independent, a direct contradiction, since "in the past" is a comparison against `today`.
`lapsed_open` is computed on the **anchored** open row, the same anchor `straddling` uses.

⚠ **`lapsed_open` carries an explicit `None` guard, same trap as F1.** On a tail-open roster the
anchor's derived end **is** `None`, and `None < today` raises `TypeError`. **Ruling: when the anchored
open row's `kernel_derived_end` is `None`, `lapsed_open` is not emitted.** An open tail row has no
derived end, so it has no end that can be in the past, and there is nothing to report.

**⚠ The kernel's derived end is `period_effective_end`'s semantics, never
`period_spend_window_end`'s.** This is the entire reason #589 split one helper into two (its §2.1):
a clock-dependent end paints phantom overlaps between the open row's floored window and the historic
stubs on *every* lapsed org, verbatim the failure
`reference_billing_period_boundary_model.md` exists to prevent.

⚠ **Round-4 finding F4: revision 4 claimed this was "enforced by construction rather than by a test",
and that claim is STRUCK. It was the load-bearing half of a merge with no fence at all.** What is
structurally unreachable from a sync sessionless kernel is the **FUNCTION** `period_spend_window_end`.
Its **SEMANTICS** are one line away, because §8.1 item 3 injects `today` into the kernel:

```python
derived_end = max(rows[i + 1].start_date - datetime.timedelta(days=1), today)
```

That line is pure, sync and sessionless, it type-checks against every frozen signature in §8.1, and it
is **exactly what `period_spend_window_end:600-601` encodes**. Walk the fourteen kernel tests against
it: tests 1, 3, 4, 5 and 10 use closed-row fixtures, where the floor never applies; test 2 is
instructed to pin its fixture converged relative to `today`; tests 6, 7, 12, 13 and 14 do not test ends
at all. **Test 11 is the only test that could catch it**, and revision 4's clause (c) asked only for
"an open INTERIOR row, asserting `successor.start − 1`", with no stated relation to `today`. §4's
house rule "anchor dates relative to `date.today()`" plus any natural fixture puts that row around
today, where the floor is a no-op. **All fourteen pass, 234a merges, and every lapsed org in
production gets phantom overlaps between the open row's floored end and its historic stubs.**

**Ruling: the correct statement is that the async helper is unreachable while the floored semantics
are trivially reimplementable, and 234a must fence the semantics rather than rely on the signature.**
Test 16 additionally fences the route, where both helpers are genuinely reachable.

⚠ **Revision 6 correction (R-S1): revisions 5's claim that test 11 clause (e) is "F4's only fence
inside 234a" is FALSE, and the review PROVED it by building the three floors separately.** Clause (e)
catches exactly one of the three, and not through the clause:

| where the floor is written | caught by | not caught by |
|---|---|---|
| **D3a** — inside `kernel_derived_end`, reading a real clock | **test 11** | — |
| **D3c** — inside `find_period_anomalies`, duplicating the derivation | **test 2** | test 11 (it asserts on `kernel_derived_end`, which stays clean) |
| **D3b** — behind an optional `today=` kwarg on `kernel_derived_end` | **test 14a's signature assertions** | test 11 (the kwarg defaults to no floor) |

Test 11 is green against **both** D3b and D3c. And it catches D3a by wall-clock MAGNITUDE — the
fixture's `today` is years past the derived end — not by clause (e)'s relation as such. Clause (e)
remains normative and remains required, because without it even D3a escapes; it is simply not the
only fence, and stating that it was left two of the three floors reading as unfenced when they are
not.

**Ruling: F4's fences inside 234a are test 2 (D3c), test 14a's signature assertions (D3b) and test
11's clause (e) (D3a) — three tests, one per place the floor can be written.** This is why §4a now
labels test 2 a `fence` and deletes the fixture alternative that would have dissolved it.

#### Row-level suppression rules, both normative

**Tail rows.** `effective_end` is `None` for the roster tail. ⚠ **Revision 3 wrote "a row whose
`effective_end` is `None` participates in no pair, on either side." That was a NEW defect.** Both
pair rules only ever read the **LEFT** row's end, so excluding a row as the RIGHT member is lossy: it
suppresses real gaps and real overlaps measured against a tail open row.

**Ruling: a row whose `effective_end` is `None` is never the LEFT member of a pair; it may be the
RIGHT member of either rule.**

⚠ **Revision 5 widens the scope of that ruling, because narrowing it to pairs is what produced F1(b).**
Revision 4 wrote the clause against "pairs" only, and `straddling` and `lapsed_open` are not pair
rules, so on a tail-open roster both reached a `None` and raised. **The general rule: NO predicate
anywhere in the kernel may compare a `None` derived end. Every rule states its own `None` handling
explicitly** (pairs above, `straddling` in its predicate block, `lapsed_open` in the temporal-set
note). A future rule that omits it is a defect on the fleet's commonest roster shape, not an edge case.

With that, the intended shape
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
  adjacent-pair `O(n)`; `straddling` resolves ONE anchor and makes ONE `O(n)` pass; `inverted`,
  `no_open`, `duplicate_open` are `O(n)`. ⚠ **Revision 6 correction:** revision 5 said `straddling`
  was `O(n·k)` in open rows. It is not, and never was as implemented — the anchor is a single row
  (the MAX-start open one), so there is no `k`. The shipped code was better than this document
  advertised; the advertisement is corrected rather than the code.

**Ruling: past the cap, suppress `overlap` ALONE** and emit an **`overlap_analysis_skipped`** marker
carrying the row count and the cap. Suppressing every structural marker would kill `duplicate_open`
on 1000+ row orgs, which is precisely where that corruption hides.

**Cap = 2000 rows.** 2M pair comparisons is sub-second in Python; the real cliff is around 5000. At
2000 the refusal path should never fire in practice, which is what a refusal path should look like.

⚠ **The comparison is pinned, because revision 4 stated the boundary three different ways** ("> 2000"
in §2.4a, "past 2000 rows" in test 13, and a `>= 2000` reading elsewhere). **Normative:
`len(roster.rows) > 2000` skips overlap analysis.** At exactly 2000 rows the analysis RUNS. Test 13
therefore seeds **2001** rows, not 2000.

⚠ **Never return an empty `anomalies` list when analysis was skipped.** The skipped marker is itself
an anomaly, and `roster.analyzed` (§2.5) reports the same fact at the scope level.

#### The emission ceiling, added in revision 5

⚠ **§2.4a caps comparison COST but said nothing about emission COUNT, and the same named rule applies
to both.** Below the cap, 1999 closed rows each spanning ten years is a legal roster and yields on the
order of **2M `overlap` markers**, a response in the hundreds of megabytes and a page that cannot
render. The comparison loop stays sub-second, so the cap never fires.

This is admin-authenticated and self-inflicted, so it is **recorded with a bound rather than treated
as a threat**. **Ruling, consistent with §2.4a's named rule (truncation for analysis must be refused,
never silently applied): a marker-count ceiling of 5000.** Past it the kernel stops emitting `overlap`
markers and emits **`overlap_emission_capped`**, a **roster-scoped** marker under §1.1's third class,
exactly like `overlap_analysis_skipped`. Non-`overlap` markers are never suppressed by this ceiling.

⚠ **The ceiling's boundary is PINNED, in the same direction as the analysis cap's, and revision 5
left it unstated while the implementation used `>=`.** **Normative: the marker fires when the number
of candidate `overlap` markers EXCEEDS 5000.** At exactly 5000 nothing was suppressed, so there is
nothing to refuse and no marker is emitted. (The two comparisons are deliberately parallel:
`len(rows) > 2000` for the analysis cap, `overlap_count > 5000` for the emission ceiling.)

⚠ **The payload is `overlap_count`, NOT `emitted_count`, and revision 5's field could not carry
information.** The review proved it: the loop stopped the instant `emitted` reached the ceiling, so
whenever the marker fired `emitted_count` was **always exactly 5000** and the page would render
"5000 of 5000". **Ruling: the marker carries `overlap_count`, the number of `overlap` markers the
roster WOULD have produced had there been no ceiling, together with `cap`.** How many were emitted is
`cap`, by construction, so it needs no field of its own. The pair scan therefore runs to completion
rather than breaking early; the extra cost is bounded by the analysis cap directly above
(`n <= 2000` → at most ~2M comparisons, the exact budget that cap was sized for).

### 2.5 Response contract

**The `PeriodAnomaly` type and its marker payload schema are [234a]** — the kernel's output contract,
which 234b consumes verbatim. **Everything else in this section is [234b], with ONE exception, named
explicitly below.**

⚠ **Revision 6 resolves a contradiction the review found (R-S4).** "Everything else in this section
is [234b]" swept up the **anomaly list ordering ruling** further down this section, while §8.1 item 4
assigns that ordering to the 234a freeze. The two statements cannot both hold, and the shipped code
follows §8.1: `_anomaly_sort_key` and the sorted return are in the kernel. **Ruling: §8.1 wins. The
ordering ruling is [234a]**, because an unordered kernel output cannot be pinned by a 234b test
without 234b first re-sorting, which is the rewrite §8.1 exists to prevent.

#### The kernel's types [234a]

⚠ **Named `PeriodAnomaly`, NOT `Anomaly`.** `backend/app/schemas/ai_forecast.py:38` already owns
`AnomalyFlag`, with `anomalies` fields at `:59` and `:106`, in an unrelated AI-forecast sense
(verified). A bare `Anomaly` in `billing_service.py` would collide in every reader's head and in
every grep.

**Shape: a frozen dataclass with a `kind: Literal[...]` tag and optional fields.** That matches the
repo's service-layer convention (verified: `cc_cycle_service.py:31`, `budget_rebalance_service.py:112`,
`loan_service.py:103` all return `@dataclass(frozen=True)`, and there are **no** `NamedTuple` or
`TypedDict` declarations anywhere in `backend/app/services/`).

A **discriminated Pydantic union** is the house pattern only at the **wire boundary**. That is
**234b's response model**, built over the kernel's dataclasses without the kernel importing Pydantic
at all.

⚠ **Revision 7 corrects the cited precedent.** Revision 6 cited `backend/app/schemas/dashboard.py`,
which is a **weak** precedent twice over: it is an **INPUT** union (a layout the client sends), and
**every variant carries identical fields** (`type` plus `config`, over a shared base). It says
nothing about a nine-variant RESPONSE union whose variants have genuinely different field sets.
**Cite `backend/app/schemas/report_layout.py`** (nine widget variants, `Field(discriminator="type")`
at `:260`) **or `backend/app/schemas/scenario.py`** (`:304`, `:329` — two nested discriminated
unions over variants with materially different fields) instead.

⚠ **The kind→model mapping must be PER-KIND EXPLICIT, never a `dataclasses.asdict` sweep.** The
shipped payloads are not uniform: `no_open` carries `period_ids=()` (`billing_service.py`, the
`kind="no_open"` emission) while **both refusal markers carry `period_ids=None`**, by defaulting.
A sweep serialises the two indistinguishably, or emits every `None` field on every variant, which is
the nine-column table arriving through the wire format.

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
                  "straddling", "lapsed_open", "overlap_analysis_skipped",
                  "overlap_emission_capped"]
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
    overlap_count: int | None = None      # ⚠ revision 6: was `emitted_count`
    cap: int | None = None
```

Four more module-level names ship with the kernel and are **part of the freeze** (§8.1), because
234b's response model and threshold tests need them by name:

```python
AnomalyKind = Literal[...]          # the nine kinds, in the order §2.5 sorts by
PeriodStatus = Literal["invalid", "open", "upcoming", "current_by_calendar", "past"]
OVERLAP_ANALYSIS_CAP = 2000         # §2.4a
OVERLAP_EMISSION_CAP = 5000         # §2.4a's emission ceiling
```

⚠ **`AnomalyKind`'s declaration order IS the sort order.** Reordering the `Literal` silently
reorders every response; it is contract, not formatting.

⚠ **The nine kinds are already written down TWICE in shipped code** (`AnomalyKind` and `_KIND_ORDER`,
which encodes the same nine in the same order), and **234b's Pydantic union would be a THIRD copy**.
**Normative: 234b's union is DERIVED from `AnomalyKind`, or ASSERTED against it by a test that fails
when the two sets differ.** A hand-typed third list drifts the moment a tenth kind lands, and it
drifts silently: an unknown kind still serialises through §2.5's tolerate-unknown rule.

#### Marker payloads, one row per kind, all nine, frozen in 234a

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
| `overlap_analysis_skipped` | `period_count`, `cap` | §2.4a; `period_count` is the org's true row count, `cap` is 2000. **Roster-scoped** (§1.1) |
| `overlap_emission_capped` | `overlap_count`, `cap` | §2.4a's emission ceiling; `cap` is 5000. ⚠ **`overlap_count` is what the roster WOULD have emitted, always `> cap` when the marker fires** — never the emitted count, which is `cap` by construction (revision 6). **Roster-scoped** (§1.1) |

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
  "referenced_periods": {              // one entry per id ANY marker names,
                                       // in-window ids INCLUDED (see below)
    "12": { "id": 12, "start_date": "2023-01-01", "end_date": "2023-09-30",
            "effective_end": "2023-09-30", "status": "past" },
    "41": { "id": 41, "start_date": "2026-07-25", "end_date": null,
            "effective_end": "2026-08-24", "status": "open" }
  }
}
```

**⚠ Two scopes, never conflated. This is the response's central contract.**

| scope | meaning | fields |
|---|---|---|
| `roster` | **org-wide**, the anomaly domain | `period_count`, `first_start`, `last_start`, `analyzed` |
| `window` | **display only** | `from` (min displayed `start_date`), `to` (null, no upper bound), `displayed_count`, `truncated` |

⚠ **Null cases, stated in revision 7 because two of these fields had no value on legitimate
responses:**

- **`window.from` is `null` when `periods` is empty**, which D8 accepts as legitimate. There is no
  minimum displayed `start_date` to report.
- **`roster.first_start` and `roster.last_start` are BOTH `null` on a zero-period org.**
- **`window.to` is permanently `null`.** D8 gives the window no upper bound, so nothing can ever
  populate it. **It is kept for schema stability and is dead**; a reader should not go hunting for
  the code that sets it.
- **`months` is NOT in the response, deliberately.** §1.1's copy needs `N`, and the **page owns the
  query param** it sent, so it already has it. Stated explicitly so nobody invents a `window.months`
  field to close a gap that does not exist.

**`referenced_periods` is required, not optional.** With org-wide analysis a marker can name a period
id the displayed page does not carry. **Every id ANY marker references appears here, in-window ids
included** (the example above carries one of each; revision 6's example showed only the off-window
id, which read as an off-window-only map). Keyed by id as a string. **`effective_end` is mandatory on
each entry**, or the page cannot render an off-window open row's gap bounds without recomputing what
the kernel already knew. **`off_window` is emitted per marker**, true when any id it references is
absent from `periods`; a client could derive it by set-difference, and the field exists so it does
not have to.

⚠ **`off_window` is `false` on every roster-scoped marker, and that is meaningless rather than
reassuring** (F5). `no_open` carries an empty `period_ids` and `overlap_analysis_skipped` /
`overlap_emission_capped` carry none at all, so the set-difference is vacuously empty. **The field is
emitted for schema uniformity and 234b must not use it to decide whether a roster-scoped marker
renders**; §1.1's third marker class governs those, unconditionally.

**Copy that states the guarantee**, and it must appear on the page: *"Checks cover your entire
roster. The timeline below shows the last N months."*

⚠ **Recorded upgrade.** Revision 3's D8 carried a bolted-on caveat, "the response states the window
bounds so a marker's absence is never mistaken for health", which was an admission the page could not
be trusted. **It is deleted.** Org-wide analysis lets the page make the **strong** claim (*absence of
markers means the roster is healthy*) where the windowed design could only make a weak one. That
upgrade, not the caveat, is what the subtraction bought.

Remaining rules:

- ⚠ **`roster.period_count ≡ len(roster.rows)`, normative** (§2.2 amendment 2). The route serves it
  **from the `CompleteRoster` it handed the kernel**, never from an independent `SELECT COUNT(*)`.
  Without this pin, test 22 goes green on exactly the wiring it exists to forbid: a correct count from
  one query beside a windowed list handed to the kernel from another.
- ⚠ **`roster.analyzed` has no kernel-side source, and revision 4 never said where it comes from.**
  `find_period_anomalies` returns a marker list and nothing else. **Ruling: 234b derives it,
  `analyzed = not any(a.kind == "overlap_analysis_skipped" for a in anomalies)`.** It is a scope-level
  restatement of a marker the kernel already emits, not a second source of truth, and it is stated
  here so an implementer does not add a return value to the frozen signature to carry it.

  ⚠ **`analyzed` names a roster-wide property and derives from an OVERLAP-ONLY refusal.**
  `analyzed=false` means **the overlap analysis was skipped**; the other eight rules always ran
  (§2.4a suppresses `overlap` alone, precisely so `duplicate_open` survives on the 1000+ row orgs
  where that corruption hides). §1.1's swapped guarantee sentence must say exactly that, not
  "checks did not run".
- ⚠ **Anomaly list ORDERING is pinned, and revision 4 left it unspecified.** Unordered, 234b's
  rendering is nondeterministic across equivalent rosters and every test asserting a list is
  accidentally order-sensitive. **Ruling: `anomalies` is sorted by `kind` in the `Literal` declaration
  order above, then by the period ids the marker references, ASCENDING, as a whole tuple, then by
  `from_date`.** Markers referencing no id sort last within their kind. **Tests may assert the list
  directly**; the alternative (mandating order-insensitive assertions everywhere) was considered and
  rejected, because it leaves 234b's rendering unpinned. **This ruling is [234a]** (see the exception
  named at the top of this section).

  ⚠ **Revision 6 correction (R-B2): revision 5 said "by the LOWEST period id the marker references"
  and asserted "this is a total order on every roster because ids are unique". The second claim was
  FALSE**, and the review produced the counterexample: rows `A(id=5)`, `B(id=6)` and `C(id=1)`, with
  both `A` and `B` containing `C`, produce `overlap 5→1` and `overlap 6→1` — two distinct markers,
  same kind, same lowest id, same `from_date`, therefore **byte-identical keys**. Output stayed
  deterministic (stable sort, fixed emission order), so nothing was broken at runtime; but the licence
  to "assert the list directly" and 234b's rendering pin both rest on totality, so **the claim is made
  TRUE rather than softened: the id component is the whole sorted tuple, not its minimum.** The
  leading comparison is unchanged — the lowest id still decides first — and the remaining ids are the
  tie-break behind it.

  **What totality guarantees, precisely:** no two markers the kernel emits from one roster share a
  key. Within a kind, each rule emits at most one marker per row (`inverted`, `straddling`), per
  adjacent pair (`gap`), per `i < j` pair (`overlap`), or per roster (`duplicate_open`, `no_open`,
  `lapsed_open`, and the two refusal markers), and row ids are unique. Fenced by test 13e.
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
`CompleteRoster`**. §2.4. Its signature, `kernel_derived_end`'s and the `CompleteRoster` type are
frozen in §8. Fenced in 234a by tests 11 and 14a, and in 234b by test 16.

**D5 [234a] — Ends are derived IN-MEMORY from the complete roster, and the equivalence is proved in
§2.2.** ⚠ **This REVERSES revision 2's D5 IO mandate, which is deleted.** The mandate turned a
formula bug into an IO requirement, and an IO requirement is what made revision 3's pure-kernel
contract unbuildable. **The fence is the differential test (test 11), not a mandate**, and after
round 4 it is test 11 **with fixture clause (e)**, which is also 234a's only fence against the wrong
end semantics being reimplemented inside the kernel (§2.4, F4). The completeness precondition is
enforced by the single `CompleteRoster` construction site (§2.2), audited by test 14's AST guard.

**D6 [234b] — Per-row bounded aggregates, over a hard-capped N. Reject the single grouped `CASE`
query outright.** The ticket correctly warns that a `CASE` returns the first match, and then still
frames the work as one grouped query. On the page whose *purpose* is exposing overlaps, a transaction
must be attributable to **every** period that contains it. Overlapped transactions appear in more
than one row's count, and the UI carries an explicit note that counts may exceed the org total where
periods overlap. **Columns that sum to the org total would be a lie on precisely the corrupt rosters
this page targets.**

⚠ **N must be capped explicitly.** `POST /billing-period` accepts arbitrary starts, so an org can
hold hundreds of rows. Ruling: **`months` is clamped to 1..60** (house pattern at
`routers/settings.py:467`), and the **display slice** is capped at **200 rows**, reported as
`window.truncated`.

⚠ **The display window is SLICED from `load_complete_roster`'s result in Python, not re-SELECTed.**
Revision 4 left 234b with two queries returning the same rows, and `load_complete_roster` already
returns every `(id, start_date, end_date)` the org has, `start_date` ASC, with no LIMIT. A second
windowed SELECT buys nothing and costs correctness: **an insert landing between the two queries makes
`roster.period_count` and `periods` describe different rosters**, which is the scope confusion §2.5's
two-scope contract exists to prevent, reintroduced at the query layer. **Ruling: one fetch. The
window, the `LIMIT 200` truncation and `window.from` / `window.displayed_count` / `window.truncated`
are all computed over `roster.rows`.** The per-row aggregate queries below are unaffected; they are
keyed on the sliced ids.

⚠ **B2, revision 7: revision 6 ruled "one fetch" and then, ten lines away, described the display
window as a second SQL query** ("the display query carries `LIMIT 200`"; "the query is
`ORDER BY start_date DESC LIMIT 200`, re-sorted ASC for the body"). **Those were stale revision-4
sentences the subtraction failed to remove, and they are DELETED.** An implementer following them
literally adds a second `select(BillingPeriod).order_by(start_date.desc()).limit(200)` and **every
test still passes**: test 22 asserts `period_count` against `COUNT(*)` plus an off-window anomaly,
and both stay green when the SECOND query is what produced the display slice.

⚠ **The cap's truncation DIRECTION is normative, restated in SLICE vocabulary: the cap keeps the
NEWEST rows.** `roster.rows` is already `start_date` ASC, so the display slice is **the LAST 200 of
the in-window slice**, which needs no re-sorting at all. The naive alternative, taking the FIRST 200,
keeps the oldest rows and discards the open row, every stub and every recent boundary.
`window.from` reports the **truncated** lower bound, not the requested lookback bound.

⚠ **The fence is a QUERY COUNTER, not prose (test 32).** **Normative: the request issues NO `SELECT`
against `billing_periods` other than `load_complete_roster`'s.** Implement by counting statements
through a SQLAlchemy `before_cursor_execute` event, the pattern already shipped at
`backend/tests/services/test_admin_orgs_service.py:333-342` (registered on the sync engine, removed
in a `finally`). It is also B4's fence: a route that re-materialises ORM entities to feed
`period_spend_window_end` trips the same counter.

⚠ **Truncation is now display-only and cannot hide an anomaly.** Under revision 3 this cap was also
the analysis domain, which is why its direction was round 2's only unanimous blocker. Under revision
4 the anomalies come from `load_complete_roster`, so a truncated page still reports every marker,
with the off-window ones in `referenced_periods`. The direction ruling is kept because a timeline
starting five years ago is still a bad page, not because correctness depends on it.

**Query budget, accepted not hidden.** At the 200-row display cap: **1** `load_complete_roster` fetch
+ **≤400** aggregates (two per displayed row) + **~1 per OPEN row** for `counting_through` ≈ **402
round trips**. ⚠ **`period_effective_end` costs ZERO queries on a closed row** (it returns
`period.end_date` before touching the DB), so `period_spend_window_end` only queries on open rows,
and revision 6's worry about "N extra queries" for the second end was wrong: on a healthy roster it
is exactly one. ⚠ DO App Platform applies a request timeout, so an org near the cap hits it first;
the fix is the alternative below, not a raised cap.
**Recorded alternative, legitimate but not mandated:** a single
`JOIN billing_periods ON <bucketing date> BETWEEN start_date AND effective_end` emits one row per
(period, transaction) pair, **natively satisfying D6's every-containing-period requirement** in one
query rather than 400, so it is not the rejected `CASE` shape. It **must be measured before adoption**
(the join predicate is not sargable against either named index) and must reproduce the pinned numbers.

**D7 [234b] — Two columns, two filters, two *different* predicate shapes.** ⚠ Revision 1 named the
right indexes and then mandated one predicate shape for both columns, which **defeats the very index
it claims each column uses**. Both reviewers caught it. Corrected:

⚠ **B1, revision 7: the window `[a, b]` both columns bound on was NEVER DEFINED, and the tail row had
no upper bound at all.** Two candidates exist and §2.5 carries both, `effective_end` and
`counting_through`. **Ruling: both aggregate columns bound on `[start_date, counting_through]`,
matching §0.3's deep link.**

*Why, and it is not a preference.* §0.3 rules the deep link uses `counting_through`. Bounding the
aggregates on `effective_end` instead makes the rendered count differ from the linked-to page's count
**on every lapsed org**, destroying the "the count and the deep link agree by construction" property
the `UNION ALL` below exists for. Concretely: open row `effective_end` 2026-05-31, `today` 2026-07-29
→ `counting_through` 2026-07-29. Two months of settled rows sit in the link's set and are absent
from the count beside it, on the page whose subject is numbers that disagree.

⚠ **A `null` `counting_through` yields a ONE-SIDED predicate**, `>= start_date` with **no upper
bound**, on the settled-net query and on **BOTH branches** of the `UNION ALL` below. This is the
roster **tail**, where `effective_end` and `counting_through` are both `None` (§2.5), and revision
6's "plain `BETWEEN`, no `OR`, no `coalesce`" wording forbade the only correct shape by omission. The
one-sided form keeps the index range intact; it simply drops the trailing bound. Fenced by test 33.

- **Settled net — `reportable_transaction_filter()` + `status = SETTLED` + plain
  `settled_date BETWEEN start_date AND counting_through`** (one-sided `>= start_date` when
  `counting_through` is null). No `OR`, no `coalesce`. That is a clean three-column range on
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
  two-branch `UNION ALL`** — one branch `settled_date BETWEEN start_date AND counting_through`, one
  branch `settled_date IS NULL AND date BETWEEN start_date AND counting_through` (which does use
  `ix_transactions_org_date`); **both branches go one-sided together** when `counting_through` is
  null, never one and not the other. If the
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
`upcoming`. Then the newest-200 slice (D6).

⚠ **B5, revision 7: "a calendar lookback from today" named neither a COLUMN nor an ANCHOR**, leaving
open whether a period starting 13 months ago but ending this month is in-window, and whether the
cutoff is a relative date or the first of that month. **Ruling, both halves normative:**

```python
cutoff = today - relativedelta(months=months)   # not the 1st of that month
in_window = row.start_date >= cutoff            # start_date only; end is irrelevant
```

So a period that starts before `cutoff` is out of window however late it ends. That is deliberate:
the window is a display concern with no correctness weight (below), and predicating on `start_date`
is what makes the slice a contiguous suffix of `roster.rows`. **Fenced from BOTH sides (test 34):** a
row starting exactly at `cutoff` is present, a row starting one day earlier is absent. A prior round
found exactly this both-sides gate missing in 234a (§4a's T2), and the same gate is written down here
before the code exists rather than after.

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

House rules: FK-sensitive assertions belong in the router suite (`PRAGMA foreign_keys=ON`); **every
service-level test names its public entry point.**

⚠ **The date rule SPLITS in revision 5, and the 234a half is REVERSED.** The house rule "anchor dates
relative to `date.today()`" (`reference_wall_clock_date_bomb_tests`) exists because a hardcoded
near-today date flips meaning when the wall clock crosses it. **234a has no wall clock**: §8.1 item 3
makes `today` a required injected argument, so a 234a test's own `today` is a literal it chooses.
Relative anchoring there buys nothing and actively hurts, because it is what let F4's floored-end
attack hide (a relatively-anchored open row sits near `today`, where `max(end, today)` is a no-op).
**Ruling: 234a tests use FULLY FIXED calendar dates, including the injected `today`.** They cannot
date-bomb, because nothing in them reads the clock. **234b keeps the relative rule**, because its route
resolves a real `date.today()`.

⚠ **Each item below is labelled `fence` (fails against an implementation missing the rule) or
`guard` (passes either way, kept as a regression net), and carries its owning ticket.** Revision 1
labelled none, and three of its "fences" could not fail. Revision 2 labelled them and three were
still wrong. Revision 3 fixed those and shipped a **new** vacuous one. Revision 4's test 2 was
labelled a fence and **empirically was not** (round 4's F2, relabelled below). This programme's
vacuous-test defect has now been caught **FIFTEEN times** — eleven in sign-off, and four more in the PR review of 234a's implementation, where the pattern shifted from *tests that could not fail* to **guards that could not guard** (§7's PR review round).

⚠ **Revision 7 adds the INVERSE, and it is the same root cause: test 16's mechanism 2 was RED against
a correct implementation** (§7's readiness round, B3). A label is not a result. **Every fence below
is owed the same gate in both directions: build the correct implementation and confirm GREEN, then
revert the fix and confirm RED.**

### 4a. Kernel — `tests/services/test_period_anomalies.py` [TBD-234a]

Entry points: `load_complete_roster`, `kernel_derived_end`, `find_period_anomalies`, `period_status`.

⚠ **Fixture plumbing, stated so the implementer does not hunt for it.** `backend/tests/conftest.py`
carries **no DB fixture** (verified: no `session_factory`, no `create_async_engine`, no
`async_sessionmaker` anywhere in it), and there is no `tests/services/conftest.py`. Every service test
builds its own engine. `tests/services/test_period_anomalies.py` therefore **copies the
`session_factory` block from `backend/tests/services/test_billing_service.py:38-52`** (in-memory
SQLite over `StaticPool`, `Base.metadata.create_all`, disposed in a `finally`). Tests 1-10, 12, 13a,
13b, 13c, 13d and 13e need no session at all; tests 11, 13, 14's load clause and 14a do.

⚠ **Test 14 SPLITS across two files, and 14b joins it there.** Test 14's load clause is a DB test and
stays in `test_period_anomalies.py`; its **AST half**, plus test **14b** (the `ORDER BY` source
guard), live in `backend/tests/test_complete_roster_single_construction_site.py`, matching the
placement of the two shipped backend source guards they are modelled on
(`tests/test_no_raw_request_client.py`, `tests/auth/test_sessions_invalidated_at_allowlist.py`).

| # | Ticket | Test | Kind |
|---|---|---|---|
| 1 | 234a | Clean contiguous roster → no anomalies | guard |
| 2 | 234a | The healthy shape `[…closed…, OPEN, stub, stub]` → **no structural anomalies**. ⚠ **Assert the STRUCTURAL set only, and pin the fixture LAPSED against the injected `today`.** Revision 5 offered a second option ("or pin the fixture converged against the injected `today`") and **revision 6 DELETES it: it silently killed the D3c fence**, because a converged fixture gives an in-`find_period_anomalies` floor nothing to reach | **fence** — ⚠ **relabelled AGAIN in revision 6**, this time upward. It is the SOLE fence for three defects: D3c (the floored end duplicated inside `find_period_anomalies`), D5 (dropping `straddling`'s self-exclusion) and D29 (`lapsed_open` never emitted). Round 4's F2 correctly showed it was not a fence for the two conditions revision 4 CLAIMED; it is a fence for three others |
| 3 | 234a | Gap between two **closed** rows → one `gap` with **both dates pinned** per §2.5 (`effective_end + 1` and `next.start − 1`) | **fence** — red against revision 1's dead-detector derivation, and the date pins make it falsifiable |
| 4 | 234a | Overlap between two **closed** rows → one `overlap`, dates pinned to `(rows[j].start_date, effective_end(rows, i))` | **fence** |
| 5 | 234a | **All-pairs overlap.** `A[2026-01-01→2026-12-31]`, `B[2026-02-01→2026-02-28]`, `C[2026-03-01→2026-03-31]`, all closed → **two** overlaps, `(A,B)` and `(A,C)` | **fence** — red against any adjacent-pair implementation |
| 6 | 234a | Duplicate open rows → `duplicate_open` naming **both ids** in `period_ids`, derived from the roster with no `open_row_ids` argument in sight | **fence** |
| 7 | 234a | Zero open rows → `no_open`; **no `straddling` marker is computed** and no exception is raised on an org with closed rows but no open row | **fence** |
| 8 | 234a | Roster tail (open row, no successor) → **not** a gap, **and** a genuine gap whose RIGHT member is that tail row **IS** reported | **fence** — ⚠ the second clause is red against revision 3's "participates in no pair, on either side" clause; the first clause alone is a guard (a pairwise iterator never makes `rows[-1]` a LEFT member) |
| 9 | 234a | **Straddling, non-adjacent, with two open rows.** A straddler `S` separated from anchor `O` by an intervening row `X` → `straddling(S)` naming `O` as `anchor_period_id`, **and** an `overlap` with **`from_period_id == S.id` and `to_period_id == O.id`** | **fence** — ⚠ **the id assertion is normative and is what makes this non-vacuous**: `overlap(S, X)` also holds and an adjacent-pair implementation emits it, so "an overlap marker is present" goes green against the very implementation all-pairs exists to kill |
| 10 | 234a | Row with `end_date < start_date`, **inserted directly** → `inverted`; `period_status` returns `invalid`; `length_days` is `null`; **and no `gap` or `overlap` is emitted on either side of it** | **fence** — not vacuous: the fixture bypasses every writer §2.3 proves non-inverting |
| 11 | 234a | **⭐ The differential fence.** For **every** row: `kernel_derived_end(roster, i) == await period_effective_end(db, org_id, row)`. The kernel does **not** call the helper, so this is a genuine differential and kills any divergence. Fixture clauses (a)-(e) below are normative | **fence** — 234a's flagship. Red against (i) an `effective_end` that returns `None` for every open row, (ii) revision 1's `successor.start − 1`-for-every-row formula, and (iii) a `max(end, today)` floor **written inside `kernel_derived_end`**. ⚠ **Revision 6 (R-S1) strikes the word "only":** test 11 is GREEN against the same floor written inside `find_period_anomalies` (test 2 catches that) or hidden behind an optional `today=` kwarg (test 14a catches that) |
| 12 | 234a | **Status partition**: an `invalid` row, a `current_by_calendar` row on a lapsed roster, and an open row starting tomorrow each get the documented status from `period_status`, with `today` injected | **fence** |
| 13 | 234a | **Analysis cap** (§2.4a): a roster of **2001** rows (`> 2000`, §2.4a's pinned comparison) carrying a duplicate open pair → `overlap_analysis_skipped` with `period_count` and `cap`, **`duplicate_open` still emitted**, and `anomalies` is **not** empty. ⚠ **Seed the rows and route through `load_complete_roster`**, do not hand-build a `CompleteRoster`; see the note below | **fence** — red against a cap that suppresses every structural marker, and red against a silent empty list |
| 13a | 234a | **The emission ceiling** (§2.4a): 101 mutually-containing rows → 5050 candidate overlaps against a ceiling of 5000 → exactly 5000 `overlap` markers plus `overlap_emission_capped(overlap_count=5050, cap=5000)`, and **no** `overlap_analysis_skipped` (the roster is below the analysis cap) | **fence** — ⚠ added during implementation: revision 5 introduced the ceiling and its marker without assigning either a test. The `5050 != 5000` pin is what makes revision 6's `overlap_count` semantics falsifiable |
| 13b | 234a | **The pinned ordering** (§2.5): a fixture that emits `inverted` before `gap` in rule order comes back `gap`, `no_open`, `inverted` | **fence** — ⚠ added during implementation, same reason: revision 5 made the ordering normative and assigned it no test, yet it is what licenses tests 3, 4, 6, 7, 8 and 10 to assert their lists directly |
| 13c | 234a | **The analysis cap from BELOW** (§2.4a): a roster of **exactly 2000** rows containing one genuine overlap → **no** `overlap_analysis_skipped`, **and the overlap IS reported**. Hand-built, not seeded (the AST guard is source-scoped to `backend/app/`) | **fence** — ⚠ **added in revision 6, closing proven gap T2.** Red against `>=` in place of `>` |
| 13d | 234a | **`straddling`'s `>=` bound AT the boundary** (§2.4): a closed row whose derived end equals `anchor.start_date` **exactly** → `straddling` fires, **and** the `overlap` is emitted alongside it | **fence** — ⚠ **added in revision 6, closing proven gap T3.** Red against a strict `>` |
| 13e | 234a | **The ordering is TOTAL, not merely deterministic** (§2.5): `A(id=5)` and `B(id=6)` both containing `C(id=1)` → the two `overlap` markers' sort keys **differ** | **fence** — ⚠ **added in revision 6.** Red against the shipped `min(ids)` key, which tied them |
| 14 | 234a | **The `CompleteRoster` AST guard** (§2.2): an `ast.parse` walk over every `.py` under `backend/app/` asserting that a call node named `CompleteRoster` appears **only** inside `load_complete_roster`. Plus the load assertion: `load_complete_roster` returns **every** row for the org, `start_date` ASC, on a roster larger than `list_periods`' 24 and larger than any lookback, **seeded DESCENDING** so the output order does not merely inherit the insertion order | **fence** — ⚠ relabelled in revision 5. Red against a second construction site anywhere in `backend/app/`, which is the completeness precondition's **only** mechanism now that the type-checker claim is struck (F3). ⚠ **Revision 6 widens the AST half to four construction SHAPES and adds a positive control**; see R-B1 below |
| 14a | 234a | **The clock-injection fence** (§8.1 item 3, previously untested): `find_period_anomalies`, `kernel_derived_end` and `period_status` never consult `date.today()`. Monkeypatch `billing_service.datetime` with a `SimpleNamespace(date=_ExplodingDate, timedelta=datetime.timedelta)` whose `date.today()` raises, then call all three with an injected `today`. ⚠ Reuse the existing pattern verbatim from `tests/services/test_billing_service.py:1400-1425` (roughly 8 lines) | **fence** — red against any `date.today()` fallback inside the kernel. ⚠ Numbered **14a** rather than renumbering 15-31, so every cross-reference in this document and in §7's round records stays valid |
| 14b | 234a | **The `ORDER BY start_date` source guard**: an `ast.parse` of `load_complete_roster` asserting its `SELECT` carries exactly one `.order_by(...)`, naming `BillingPeriod.start_date`, ascending | **fence** — ⚠ **added in revision 6, closing proven gap T1.** Red against deleting the clause AND against `.desc()`. It is a SOURCE guard because the clause is **behaviourally unobservable** in the test DB; see R-S3/T1 below |

**⚠ Round-4 finding F2: test 2's `fence` label was FALSE, and this is vacuous-test instance ELEVEN.**
The reviewer built both of test 2's stated red conditions and ran them:

- **Defect A**, "the open row's end is read as unbounded" (`effective_end` returns `None` for every
  open row): **passes all 12 kernel tests, including test 2.**
- **Defect B**, revision 1's `successor.start − 1` for every row: **also passes test 2.**
- **Only test 11 catches either**, verified RED with `assert None == date(2026, 4, 30)` on the
  interior open row.

**Ruling: test 2's two stated red conditions MOVE to test 11**, where they actually hold. ⚠ **Test 2
nonetheless earned its keep, by catching a defect it was not aimed at:** it is the test that went red
against F1's self-straddle, because the healthy shape is exactly where a missing `i != anchor_index`
shows up.

⚠ **Revision 6 (R-S2) relabels test 2 `fence`, and the direction matters.** Revision 5 demoted it to
`guard`, which was right about the two conditions revision 4 claimed and wrong about the test. The
PR review injected all 33 defects against it and found test 2 is the **SOLE** fence for three:

- **D3c** — the floored `max(end, today)` derivation duplicated inside `find_period_anomalies`
  (test 11 asserts on `kernel_derived_end`, which stays clean under D3c);
- **D5** — dropping `straddling`'s `i != anchor_index` self-exclusion;
- **D29** — `lapsed_open` never emitted at all.

**And its fixture is what makes all three hold, which is why revision 5's fixture ALTERNATIVE is
deleted.** "Pin the fixture converged against the injected `today`" reads as a harmless equivalent
and is not: a converged open row gives D3c's floor nothing to reach, so choosing that option removes
the D3c fence with nothing failing to announce it. **The lapsed fixture is mandatory.** A spec option
that silently disarms a fence is the same defect class as a mislabelled one.

**⚠ Test 11's fixture is normative, and without it the test re-vacuums.** On a clean contiguous
roster `end_date == successor.start − 1` by construction, so the wrong derivation and the right one
agree and the test proves nothing. That is exactly why revision 1's version failed to catch its own
structural defect. **The fixture must contain, at minimum:**

- **(a)** a closed row where `end_date != successor.start − 1`, the row that kills revision 1's
  formula;
- **(b)** a closed row where `end_date >= successor.start` (an overlap);
- **(c)** an open **interior** row, asserting `successor.start − 1`;
- **(d)** an open **tail** row, asserting `None`;
- **(e)** ⚠ **added in revision 5: the open interior row of clause (c) must be LAPSED relative to the
  injected `today`**, that is `rows[i+1].start_date - 1 day < today`. Without it, an in-kernel
  `max(rows[i+1].start_date - 1 day, today)` floor **written inside `kernel_derived_end`** is a no-op
  on the fixture, test 11 goes green, and 234a merges with the wrong end semantics baked in.
  ⚠ **Revision 6 correction (R-S1): clause (e) is NOT "F4's only fence inside 234a", as revision 5
  claimed.** It fences the floor written into `kernel_derived_end` with a real clock read (D3a), and
  it fences that by wall-clock magnitude rather than through the clause as such. The floor written
  into `find_period_anomalies` (D3c) is fenced by **test 2**, and the floor hidden behind an optional
  `today=` kwarg (D3b) by **test 14a's signature assertions**; test 11 is green against both. See
  §2.4's three-row table. All three fences are required and none is redundant.

**Without (a) and (b) both formulas agree; without (e) the floored variant agrees too.** All three are
required for test 11 to be worth its ⭐.

⚠ **Test 11 builds its roster through `load_complete_roster`, not by hand.** The reviewer verified this
and it costs four lines: seed the periods once, then take the row tuples from `load_complete_roster`
and the ORM instances from `select(BillingPeriod).order_by(BillingPeriod.start_date)`. The two are
index-aligned because `uq_billing_period_org_start` makes `start_date` a unique key per org, and the
test asserts `orm[i].id == roster.rows[i].id` per row as the belt. Deriving both views from one seeded
DB is what stops the test drifting into comparing two hand-built representations of different rosters.

⚠ **Test 13 seeds its 2001 rows and routes through `load_complete_roster` for the same reason plus
one more:** written as a pure test it would construct a `CompleteRoster` outside `load_complete_roster`,
which is the shape test 14's AST guard forbids in `backend/app/`. Measured by the reviewer, the DB path
is affordable: seeding 2102 rows took **0.357s** and `load_complete_roster` over them **0.003s**. This
is consistent with F3's source-scoped guard rather than an exception to it, since the exemption for
tests is what makes tests 1-10's hand-shaped fixtures legal in the first place.

#### ⚠ Revision 6 — four test gaps, each PROVEN by a green defect injection

The PR-review round ran **33 defect injections** against the merged-pending 234a suite. Four passed
the entire suite, which means four wrong implementations would have shipped. This is the
vacuous-test defect class again — instances **twelve through fifteen** — and the record is kept here
because "a test exists for this rule" is exactly the claim that keeps turning out to be untrue.

| gap | injection that stayed GREEN | why the suite could not see it | closed by |
|---|---|---|---|
| **T1** | **D22** — delete `load_complete_roster`'s `ORDER BY start_date` entirely | ⚠ **The reported cause was wrong and the prescribed fix does not work.** It is not that fixtures seeded ascending and rowid order coincided: `uq_billing_period_org_start` is on `(org_id, start_date)`, and SQLite plans the query through the implicit index behind it whether or not the clause is present (`EXPLAIN QUERY PLAN` → `SEARCH ... USING INDEX sqlite_autoindex_billing_periods_1 (org_id=?)` in **both** forms). The order survives deletion for **any** insertion order — verified by seeding DESCENDING and re-running. A *wrong* order (`.desc()`) was always caught; a *missing* one is **behaviourally unobservable against this schema** | **test 14b**, a source guard. Test 14 additionally seeds DESCENDING, which is worth having (it proves output order is not inherited from insertion order) but is **not** the fence |
| **T2** | **D17** — `len(rows) >= 2000` in place of `> 2000` | test 13 seeds **2001** rows, which both comparisons skip identically. §2.4a pinned the boundary in prose and nothing tested the lower side | **test 13c**, at exactly 2000 |
| **T3** | **D20** — strict `>` in place of `>=` on `straddling`'s upper bound | test 9's straddler ends `2026-12-31`, nine months past the anchor's start, so `>` still fires. ⚠ §2.4 explicitly warns that "ends after it" would "silently under-report exactly the shape whose deferral created this marker" — and then left it untested | **test 13d**, derived end `==` anchor start |
| **T4** | **D24a** — a value-neutral `date.today()` read inside `kernel_derived_end`'s successor branch | test 14a's roster was one closed row plus one open **tail**, so `rows[i+1].start_date - 1` never executed under the `_ExplodingDate` monkeypatch. The clock fence never ran the branch most likely to grow a clock — **F4's floor is written on that very line** | **test 14a's fixture**, now three rows so the open one is INTERIOR and all three branches execute armed |

#### ⚠ Revision 6 — R-B1: the AST guard missed three construction shapes

All three reviewers found this and one proved it with three green injections. The shipped guard
matched only a `Call` whose terminal callee name was literally `CompleteRoster`. These all produce a
windowed `CompleteRoster` inside `backend/app/` and left it **green**:

- `dataclasses.replace(roster, rows=windowed)` — ⚠ **the guard's own docstring named this shape and
  asserted "Only this test objects." That claim was false.** And it is the live risk: **TBD-234b is
  the ticket that will window a roster for display**, where `dataclasses.replace` is the natural
  reach;
- `from ... import CompleteRoster as _CR` followed by `_CR(...)`;
- `roster.__class__(org_id=..., rows=windowed)`.

**Ruling: the guard covers four shapes** — direct, `ImportFrom`-aliased, `dataclasses.replace` with a
positional first argument, and class-object indirection (`__class__` and `type(...)`) — **and its
docstring now enumerates what remains UNCOVERED** (assignment aliasing, fully dynamic construction,
unparseable modules) instead of claiming completeness it does not have. **A positive control is
mandatory**: the guard is exercised against synthetic module sources, one per covered shape plus
false-positive controls, because a detector with no positive control cannot be distinguished from a
detector that fires on nothing — and green is what both look like.

### 4b. Endpoint — `tests/routers/test_billing_period_roster.py` [TBD-234b]

⚠ **Fixture plumbing, recorded in revision 7 so it is not discovered mid-build. There is NO
`backend/tests/routers/conftest.py`** (verified: `backend/tests/conftest.py` is the only `conftest.py`
under `backend/tests/`, and it carries no DB fixture). **Every router test file builds its own
FastAPI app, its own StaticPool engine and its own dependency overrides.** Copy that block from a
neighbour rather than expecting to inherit one; `backend/tests/routers/test_admin_org_members.py` is
a close fit and already registers an `Engine` `connect` listener for `PRAGMA foreign_keys=ON`.

| # | Ticket | Test | Kind |
|---|---|---|---|
| 15 | 234b | The route **emits** `status` on every period, with the values `period_status` returns | guard — thin; the partition itself is fenced by test 12 |
| 16 | 234b | **⭐ The D4 fence.** `effective_end` and `counting_through` **diverge** on a lapsed roster and **agree** on a converged one. ⚠ **Both mechanisms REWRITTEN in revision 7 (B3); the note below is normative and supersedes revision 6's** | **fence** |
| 17 | 234b | Overlapping periods: one transaction appears in the count of **every** row containing it | **fence** — kills the `CASE` shape |
| 18 | 234b | Count is unfiltered (a transfer leg counts); settled net is filtered (it does not) | **fence** |
| 19 | 234b | Future stubs render as `upcoming` (no upper bound) | **fence** |
| 20 | 234b | **Off-window markers.** An org whose corruption sits entirely **outside** the display window still reports it: the marker is present, `off_window` is `true`, and `referenced_periods` carries every named id **including `effective_end`** | **fence** — red against any residue of window-scoped analysis, and red against dropping `effective_end` from the referenced entries |
| 21 | 234b | `months=0` is clamped, not rejected; past `LIMIT 200`, `window.truncated` is true and the surviving rows are the **newest** ones (`window.from` equals the truncated lower bound, not the lookback bound); **`roster.period_count` still reports the full count and the anomaly set is unchanged by truncation** ⚠ **The `months=999` clause MOVED to test 21b in the PR-review fold** | **fence** — the last clause is what proves display truncation no longer touches analysis |
| 21b | 234b | **The UPPER `months` clamp, on a fixture where it actually binds.** ⚠ **Added in the PR-review fold, because test 21's version was VACUOUS.** The two properties cannot share a fixture: truncation needs **more** than `ROSTER_DISPLAY_CAP` rows in the window, and once the display cap binds, *every* lookback wide enough to admit them collapses to the identical newest-200 slice — so `months=999 == months=60` could not fail. (Bracketing on the 250-row / ~57-month fixture: clamps of 12/24/36 caught, **48 and total removal not**.) 21b uses 120 rows spaced 30 days — spanning ~117 months, staying **under** the display cap — and asserts **two** things: `months=999 == months=60` and the **absolute** expected slice | **fence** — the equality catches *removing* the clamp; only the absolute slice catches *lowering* it (e.g. to 48), and neither alone is sufficient |
| 22 | 234b | **Scope separation belt** (§2.2 amendment 2): `roster.period_count` equals `SELECT COUNT(*) WHERE org_id = ?`, and `window.displayed_count` equals `len(periods)`, on an org where the two differ. ⚠ **Plus the revision-5 clause that makes it bite: assert an anomaly whose subject lies entirely outside the display window is still emitted**, so the test cannot pass on a route that counts correctly from one query and analyses a windowed list from another | **fence** — ⚠ the first two clauses alone are a **guard**; revision 4 labelled the whole test a fence and it was not one (round 4, non-blocking 11) |
| 23 | 234b | Non-admin → 403. ⚠ **The fixture user must be a PLAIN MEMBER**, not merely a non-superadmin: `_require_admin` passes anyone whose `role` is `OWNER` or `ADMIN` **or** who has `is_superadmin` (`routers/settings.py:48-53`), so a non-superadmin admin gets 200 and the test would prove nothing | **fence** |
| 24 | 234b | The route creates **no** `BillingPeriod` on an org with no open row; period count unchanged; response reports `no_open` | **fence** — fails if anyone reaches for `get_current_period` |
| 25 | 234b | Org with zero periods → 200, `periods: []`, `referenced_periods: {}`, `no_open` | **fence** |
| 26 | 234b | `GET /billing-periods` response shape unchanged | guard (regression net; nothing here touches it) |
| 32 | 234b | **The single-fetch fence** (D6/B2): with a `before_cursor_execute` counter registered on the test engine, one request issues **exactly one** `SELECT` against `billing_periods`, `load_complete_roster`'s. Register on the sync engine and `event.remove` in a `finally`, per `backend/tests/services/test_admin_orgs_service.py:333-342` | **fence** — ⚠ **added in revision 7.** Red against a second windowed `SELECT` for the display slice (B2's attack, which every other test passes), and red against B4's rejected ORM re-materialisation for `counting_through` |
| 33 | 234b | **The tail row's unbounded aggregate window** (D7/B1): on a roster whose tail is the open row (`effective_end` and `counting_through` both `null`), a **future-dated settled transaction IS counted** in that row's `transaction_count` and included in its `settled_net` | **fence** — ⚠ **added in revision 7.** Red against a `BETWEEN` with a coalesced or fabricated upper bound, and red against dropping the row's aggregates entirely |
| 34 | 234b | **The lookback boundary, BOTH sides** (D8/B5): a period starting **exactly at** `today - relativedelta(months=months)` is in `periods`; one starting **one day earlier** is not | **fence** — ⚠ **added in revision 7.** Red against a first-of-month cutoff and against a strict `>` |

**⚠ Test 16's mechanism is normative and was REWRITTEN in revision 7. It absorbs, and replaces,
revision 3's test 9.**

Revision 3 carried a separate kernel-level "clock independence of the structural set" test that was
**vacuous BY CONSTRUCTION**: the kernel is sync and holds no session, so `period_spend_window_end` is
unreachable from it and a kernel-level test can no longer fail. **Test 16 already IS the
clock-independence fence**, and it lives in 234b where both helpers are genuinely reachable. Three
mechanisms are folded into it, all normative:

⚠ **B3: revision 6's mechanism 2 was RED AGAINST A CORRECT IMPLEMENTATION, and its mechanism 1 went
vacuous the moment 234a merged.** Both defects are structural, not fixture-level:

- **Mechanism 2 mandated "compare full payloads including dates" across two frozen clocks.** But
  `lapsed_open` fires iff `anchor_end < today` (`billing_service.py:1737`), and the mandated fixture
  is `T1 <= period_effective_end(open) < T2`. **A correct route therefore returns no `lapsed_open` at
  `T1` and one at `T2`**, so the anomaly lists legitimately differ and any literal implementation of
  "compare full payloads" fails against correct code. This is round 4's F1 defect class **inverted**:
  not a test that cannot fail, but one that **must** fail.
- **Mechanism 1 is now vacuous.** Post-`15faa922` the kernel derives its own ends from
  `load_complete_roster`'s raw columns; the only clock it sees is the `today` kwarg; and injecting
  floored ends would require a **second `CompleteRoster` construction site**, which test 14's merged
  AST guard forbids anywhere in `backend/app/`. **Overlap payloads are clock-invariant by
  construction in every buildable 234b**, so no fixture can make mechanism 1 bite.

**The four mechanisms, all normative, replacing revision 6's three:**

1. **Structural-set equality across the two clocks, with the marker set named EXPLICITLY and
   `lapsed_open` EXCLUDED.** Assert equality over the structural markers (§2.4's table: `gap`,
   `overlap`, `duplicate_open`, `no_open`, `inverted`, `straddling`), full payloads including dates.
   An explicit exclusion list, never a filter written as "everything except the temporal ones".
2. **`lapsed_open` LEGITIMATELY DIFFERS across the two clocks, and the test asserts that it does:**
   absent at `T1`, present at `T2` naming the anchored open row. This is a genuine fence for the
   route **forwarding its resolved clock** (D8a) into `find_period_anomalies`; a route that passes a
   stale or defaulted clock is red here.
3. **The `effective_end` response field is computed from `period_effective_end` semantics, NOT from
   `period_spend_window_end`, and the route does not apply the floor itself.** That is the
   actually-reachable red condition post-234a: collapsing §2.1's two columns into one. **Mandate a
   revert-the-fix gate: wire `effective_end = counting_through`, run the suite, confirm RED.** A
   green suite under that wiring means this test does not exist yet, whatever it is called.
4. **Assert CONCRETE EXPECTED VALUES, never merely "the two responses are equal".** Equality-only
   assertions are the family this programme keeps getting burned by, and they are what let both of
   revision 6's mechanisms read as fences.

It must also **freeze the clock**, not only pass a `today=` kwarg: `period_spend_window_end` defaults
`today` to `date.today()` internally (`billing_service.py:600`), so a route that wires in the floored
helper and does not forward `today` is unaffected by a kwarg alone.

**Net effect of the deletion: one test deleted, one strengthened, zero coverage lost.**

### 4c. Frontend [TBD-234b]

| # | Ticket | Test | Kind |
|---|---|---|---|
| 27 | 234b | Page renders under `SettingsLayout` with the Organization tab active, by passing the literal `activeTab="/settings/organization"` | **fence** — ⚠ (§0.4) red if the page passes `activeTab="/settings/organization/periods"`, which un-highlights every tab |
| 28 | 234b | Row-scoped markers render inline on the row they name; a `gap` renders as an interstitial rail break between its two named rows, **not** as a chip on either (§1.1); the overlap note shows when any row overlaps | **fence** |
| 29 | 234b | Both ends render on every row; on a CONVERGED row they share one line in identical styling; on a DIVERGED row the second fact moves to its own line in `badgeWarning` with the divergence stated **in words inside the chip** (§1.1). ⚠ The diverged fixture's row must be OPEN, since a closed row cannot diverge (§2.1) | **fence** |
| 30 | 234b | **The summary band** (§1.1) renders every `off_window: true` marker from `referenced_periods`, on a response whose `periods` array does not contain the referenced ids; and the page renders exactly **one** `<h1>`, `SettingsLayout`'s. ⚠ **Extended in revision 5 for the roster-scoped class (F5), two further cases, both normative: (i) `no_open` renders in the band on a response with `periods: []` and a non-zero `roster.period_count`; (ii) `overlap_analysis_skipped` renders in the band likewise.** Both carry `off_window: false` | **fence** — red against inline-only marker rendering, and red against a band that filters on `off_window == true`, which erases both roster-scoped markers on the exact rosters D10 and test 24 exist to report |
| 31 | 234b | A non-admin deep-linking the page is redirected, matching `settings/organization/page.tsx:106,128` | **fence** |

**Added in the PR-review fold.** Every row below closes a hole an injection proved was green.

| # | Ticket | Test | Kind |
|---|---|---|---|
| 35 | 234b | **`length_days` is the INCLUSIVE span**, on a 30-day row and on a **one-day** row (1, never 0); `null` on an `invalid` row and on the roster tail | **fence** — the field was unfenced END TO END: both dropping the `+ 1` and hardcoding `None` passed the whole suite, since the only assertion anywhere was `is None` on the tail, which both satisfy |
| 36 | 234b | **`roster.analyzed` is `false` when the overlap check refuses.** A roster past `OVERLAP_ANALYSIS_CAP` returns `analyzed: false` **and** the `overlap_analysis_skipped` marker; every other rule still ran | **fence** — hardcoding `analyzed = True` passed everything, and the wrong value makes the page claim "Checks cover your entire roster" on the one roster where that is false. `analyzed: true` is fenced on the other side by tests 20/21/25 |
| 37 | 234b | **Every nullable wire field is required-and-nullable**, asserted against the generated OpenAPI `required` sets. `WindowScope.from`/`to` carried Pydantic defaults, making them *optional* while every sibling nullable field was required | **fence** — a generated TS client typed `window.from` as `string \| undefined`, so a consumer could not tell §2.5's legitimate empty-window case from "field absent" |
| 38 | 234b | **The `months` clamp is DOCUMENTED, not enforced.** The OpenAPI parameter carries the 1..60 bounds in its `description`, and carries **no** `minimum`/`maximum`; `-5`, `0`, `600`, `10**9` all still 200, `"abc"` still 422s | **fence** — adding `ge`/`le` would satisfy the documentation half while inverting D6/D8's clamp-don't-reject ruling |
| 39 | 234b | **An UNKNOWN marker kind renders in the band as `badgeNeutral` carrying the raw `kind` string** (§1.1, §2.5) | **fence** — ⚠ this is how a shipped blocker was found. `bandAnomalies` admitted a marker only when roster-scoped **or** `off_window`; an unknown kind is neither, `inlineAnomaliesFor` cannot place it (`anomalyPeriodIds` returns `[]` for it) and `railBreakGaps` takes only `gap`, so it rendered **nowhere** while the verdict still counted it: "1 issue found", zero visible markers. Reachable under deploy skew |
| 40 | 234b | **The timeline stays MOUNTED across a window change**: the `role="status"` caption is the **same DOM node** before and after, the `<select>` keeps focus, and the previous window's rows stay on screen until the new key resolves | **fence** — without `keepPreviousData` SWR returns `undefined` for the new key and `{data && …}` unmounts the subtree, so (a) the one mandated live region is re-inserted already populated and provably never announces, and (b) the keyboard user's `<select>` is destroyed mid-interaction with focus dumped to `<body>` |
| 41 | 234b | **Empty state (a) is asserted**: `roster.period_count === 0` renders "No billing periods yet", and **not** state (b)'s copy | **fence** — deleting the branch fell through to "None of this organization's **0** periods … still covered all **0**", nonsense copy with a green suite |
| 42 | 234b | The recessive line: `length_days` rendered, `Length not shown` when null, the `settled_net` sign, and **exactly one** brass dot, on the MAX-`start_date` open row | **fence** — `Number("-0.00") >= 0` printed `+-0.00`; the anchored-open reducer had no coverage at all |

Test 29 was also tightened in the fold: "identical styling" was fenced only as "no warning chip in this
row", so wrapping `Counting through` in a `text-text-muted` span — visually differentiating, which §1.1
forbids — passed. It now asserts the converged line contains **exactly two elements**, the two `<time>`s,
with equal class lists.

---

## 5. Rollout

Additive: one new GET, one new page, in that order across two tickets. No existing endpoint, query or
component changes, so nothing that works today can move. Worst case the new page renders wrong numbers
on a surface nobody depended on yesterday.

**234a merged caller-less, for exactly one ticket.** Four service helpers plus their tests, invisible
to users, so the kernel contract (§8) was merged, reviewed and frozen before any consumer existed to
constrain it. ⚠ **That was tolerable ONLY because 234b is committed, not optional. The debt is now
outstanding: `15faa922` is in `main` with no production caller until 234b lands.**

⚠ **Revision 4 justified this by calling `period_effective_end` "one uncalled helper defended by a
docstring". That framing is struck; see §0.1's correction C1.** `period_effective_end` is reachable in
production through `period_spend_window_end` and always has been. **What 234a actually merges
caller-less is its own four new helpers**, and §5 already gates that on 234b being committed rather
than optional, which is the real argument. Residual R1 exists to keep `period_effective_end`'s
docstring truthful about *which* consumer keeps it alive, not to defend a helper nobody calls.

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
claiming to fence the fix) has been caught **ELEVEN times**: six across TBD-232/239/241/240, three
in this document's rounds 1 and 2, a tenth in round 3 (**in the revision that folded the ninth**), and
an eleventh in round 4, test 2 (see below). `reference_vacuous_test_pattern.md` stands: **revert the
fix and confirm red is the only reliable gate.**

### Sign-off round 4 — revision 4: REJECT / REJECT, and one reviewer BUILT the kernel

Two independent reviewers, 2026-07-29. **Five blocking findings, two corrections, thirteen
non-blocking items. No design ruling was challenged by either reviewer.**

**⚠ The method changed, and it is the round's most transferable fact. One reviewer implemented the
kernel and ran this document's own tests against it.** That is what found F1 empirically: a literal
implementation of §2.4's `straddling` predicate **failed 8 of the 12 kernel tests**, and the one-line
fix (the two exclusions) took it to 12 of 12, and 16 of 16 with the DB-backed tests. Four rounds of
tracing had not found it.

**⚠ F1 was found INDEPENDENTLY BY BOTH REVIEWERS, by different methods**, one by tracing the predicate
and one by building it. **That convergence is the strongest signal in the round**, and it is the third
time in this document's history that a genuine contract defect surfaced in both reviews at once.

| # | Finding | Disposition |
|---|---|---|
| F1 | `straddling` cannot be built as written: no self-exclusion (self-straddle makes **test 2 red against a correct implementation**, so the spec was unsatisfiable) and no `None` guard (`TypeError` → 500 on `[…closed…, OPEN]`, the fleet's commonest roster) | **ADOPTED.** Two normative exclusions, `i != anchor_index` and non-`None` derived end, matching `billing_service.py:774`'s shipped precedent. Citation corrected to `:772-779` |
| F2 | Test 2's `fence` label was FALSE. **Both** of its stated red conditions were built and **both pass it**; only test 11 catches either | **ADOPTED.** Test 2 → `guard`; its red conditions moved onto test 11. Recorded that test 2 caught F1 anyway. **Vacuous-test instance ELEVEN** |
| F3 | The completeness precondition rested on a **type checker this repo does not have**, and 234a's own tests must construct `CompleteRoster` directly, disproving the "only constructor" claim inside the document | **ADOPTED.** Every type-error / type-checker claim struck; replaced with an **AST guard** (the #552 pattern, shipped twice already), source-scoped to `backend/app/` so tests are exempt by construction. Test 14 → `fence` |
| F4 | 234a would merge with **zero fence** against an in-kernel `max(end, today)` floor: the async helper is unreachable but the semantics are one pure line, and all fourteen kernel tests pass against it | **ADOPTED.** §2.4's "enforced by construction rather than by a test" struck; test 11 gains normative fixture clause **(e)**, the lapsed open interior row |
| F5 | `no_open` and `overlap_analysis_skipped` had **no rendering home**: both are vacuously `off_window: false`, so the page renders "no issues" under copy claiming full-roster coverage, on the one roster D10 and test 24 exist to report | **ADOPTED.** Third normative marker class in §1.1, **roster-scoped**, rendered unconditionally. Test 30 extended with both cases |
| C1 | "`period_effective_end` has no production caller" is FALSE and had been repeated since #589 | **ADOPTED.** Corrected in §0.1 and §5; R1's replacement text pinned so it cannot restate the false premise |
| C2 | Effort is **S**, not M (the reviewer built it: ~130 lines plus 14 tests reusing an existing fixture block) | **ADOPTED.** 234a relabelled effort-s in the header |

**Non-blocking, all thirteen folded:** `kernel_derived_end` named as a module-level deliverable and
frozen (test 11 was asserting on an unwritable closure); `lapsed_open`'s `None` guard; the cap
comparison pinned to `> 2000`; anomaly ordering pinned deterministically; `roster.analyzed`'s source
stated; tests 11 and 13 routed through `load_complete_roster`; test 14a added for §8.1 item 3 using the
existing `_ExplodingDate` pattern; the 234a date rule reversed to fixed calendar dates; §2.4a given an
emission ceiling and its own refusal marker; 234b's display window sliced from the single fetch; test
22's real fence clause added; the `conftest.py` gap recorded.

**⚠ The core subtraction SURVIVED, and it was attacked directly.** One reviewer went at §2.2's
equivalence proof and **could not break it**, confirming the uniqueness premise holds in **both**
databases: `uq_billing_period_org_start` at `models/billing.py:12-14` for the SQLite test DB via
`create_all`, and `backend/alembic/versions/017_billing_period_unique_constraint.py:24` for MySQL. The
five-branch status partition was verified **total and disjoint by exhaustion**. Every design ruling
this document has carried since revision 1 has now survived **four** independent rejection rounds
unchanged.

**⚠ Round 3's pattern repeated, and it is now a documented property of this programme rather than an
observation. THREE of round 4's five blocking findings were created by revision 4's OWN rewrites:**
F1 by narrowing the `None` clause to "pairs", F4 by the "structurally unreachable" claim, and F5 by the
org-wide analysis upgrade. **Folding findings keeps introducing findings.** Revision 5 is deliberately
a targeted fold rather than a restructure for that reason, and each of its edits was verified against
the repository before being written.

**Sign-off round 5 — revision 5:** APPROVED; 234a built and opened as PR #590.

### PR review round — 234a's IMPLEMENTATION (PR #590), three independent reviewers

2026-07-29. Kernel correctness, test vacuity (**33 defect injections**), and conventions/regressions.
**No design ruling was challenged, for the fifth consecutive round.** What the round found was that
several of **this document's own claims** were empirically false, and that the guard the document
calls "the precondition's only mechanism" did not cover the shape the document names as the risk.

| # | Finding | Disposition |
|---|---|---|
| R-B1 | The `CompleteRoster` AST guard missed three construction shapes, **including `dataclasses.replace`, which its own docstring claimed to catch** ("Only this test objects"). Found by all three reviewers; one proved it with three green injections | **ADOPTED.** Guard widened to four shapes (direct, `ImportFrom` alias, positional `replace`, `__class__` / `type(...)`); docstring now enumerates what it does NOT cover; **positive control added** so the detector cannot silently stop detecting. §2.2, §4a R-B1 |
| R-B2 | `_anomaly_sort_key`'s docstring shipped a false invariant: collapsing four id fields to `min(ids)` is **not** a total order. Counterexample proven | **ADOPTED.** Key uses the whole sorted id tuple; the claim is made TRUE rather than softened. §2.5, **test 13e** |
| R-T1..T4 | **Four test gaps, each proven by a GREEN injection** (D22 `ORDER BY`, D17 the cap boundary, D20 `straddling`'s `>=`, D24a the successor branch under the clock monkeypatch) | **ADOPTED, with one correction to the finding itself:** T1's stated cause and prescribed fix were wrong — the clause is behaviourally unobservable against this schema, so it gets a **source guard (test 14b)**. §4a's R-S3 table |
| R-S1 | §4a's claim that test 11 clause (e) is "F4's only fence inside 234a" is FALSE; test 11 is green against two of the three places the floor can be written | **ADOPTED.** §2.4 gains the three-row fence table; clause (e)'s claim narrowed to D3a |
| R-S2 | Test 2 is the SOLE fence for three defects, and §4a still offered a fixture option that **silently kills one of them** | **ADOPTED.** Option deleted, lapsed fixture mandated, test 2 relabelled **`fence`** |
| R-S4 | Contract gaps that would mislead 234b: the emission ceiling's boundary direction unpinned; `period_status` missing from the freeze (and its `RosterRow` argument unstated); four shipped symbols unnamed; test numbering drifted; §2.5 and §8.1 contradicted each other over the ordering ruling | **ADOPTED.** §8.1 now freezes **five** items; §2.5 defers to §8.1; numbering reconciled to 1-13, 13a-13e, 14, 14a, 14b |
| R-C1..C6 | Stale self-citations (+13 shift), the missing "no production caller / do not prune" idiom, a duplicated `invalid` predicate, an `emitted_count` that could not carry information, an inaccurate `O(n·k)` comment, an undocumented unreachable guard | **ADOPTED.** In-file citations converted to **symbol references** (shift-proof, and the same defect class as residual R1); `_is_inverted` extracted **without** routing through `period_status`, which would make a clock-free structural rule depend on `today`; `emitted_count` → `overlap_count` |

**⚠ Vacuous-test instances twelve through fifteen, and the pattern has now shifted.** Rounds 1-4
caught tests that could not fail; this round caught **guards that could not guard** — an AST matcher
with no positive control, a boundary pinned only on the side a fixture happened to sit, a clock fence
that never executed the branch most likely to grow a clock. **Revert-the-fix-and-confirm-red is
still the only reliable gate, and it must be run against the GUARD as well as against the code.**

### 234b readiness round — revision 6's 234b sections, read against the MERGED kernel

2026-07-29, after `15faa922`. **Five blocking defects, eleven non-blocking items, plus a design pass.
Every one of the five was found BEFORE a single line of 234b existed**, which is the round's point:
the four earlier rounds each found the spec's defects by building against it, and this one found them
by reading the spec against code that had already shipped. **No design ruling was challenged, for the
sixth consecutive round.**

| # | Finding | Disposition |
|---|---|---|
| B1 | D7's aggregate window bounds `a` and `b` were **never defined**, and the roster **tail** had no upper bound at all while the prose forbade the fix by omission ("plain `BETWEEN`, no `OR`, no `coalesce`") | **ADOPTED.** Both columns bound on `[start_date, counting_through]`, matching §0.3's deep link; a `null` `counting_through` yields a **one-sided** predicate on the settled-net query and on **both** `UNION ALL` branches. **Test 33** |
| B2 | D6 ruled "one fetch, sliced in Python" and then described the display window as a **second SQL query**, ten lines away. Stale revision-4 sentences the subtraction missed | **ADOPTED.** Sentences deleted, truncation direction restated in slice vocabulary, and a **query-counting fence (test 32)** added because every existing test passes against the two-query wiring |
| B3 | Test 16's mechanism 2 was **RED against a CORRECT implementation** (`lapsed_open` legitimately differs across the mandated clocks, `billing_service.py:1737`), and mechanism 1 went **vacuous** the moment the kernel merged (overlap payloads are clock-invariant by construction; injecting floored ends needs a second `CompleteRoster` site, which test 14's AST guard forbids) | **ADOPTED.** Four mechanisms replace three: structural-set equality with `lapsed_open` explicitly excluded; `lapsed_open` asserted to **differ**; the reachable red condition restated (`effective_end` computed from the spend-window helper); concrete expected values. **Revert-the-fix gate mandated** |
| B4 | `counting_through`'s computation path was unspecified, and the shipped signature admits **three incompatible answers** (a second `SELECT`, passing the `RosterRow`, or an inline third copy of the floor) | **ADOPTED.** Annotation widened to accept either shape, the roster row is passed, and a docstring note names the three attributes the body reads. Fenced by test 32 |
| B5 | D8's lookback named **neither a column nor an anchor**: `start_date >= cutoff` or containment, and `today - relativedelta` or the first of that month | **ADOPTED.** `cutoff = today - relativedelta(months=months)`, predicate on `start_date`. **Test 34, both sides**, the same gate a prior round found missing in 234a |

**⚠ B3 is this programme's signature defect INVERTED, and that is worth naming.** Fifteen times over
five rounds the finding was *a test that cannot fail*. B3 is *a test that must fail against correct
code*, the same root cause (a fence asserted rather than executed) pointing the other way. **Both
are caught by the same gate**: build the correct implementation, run the test, and read the result
instead of the label.

**Non-blocking, all eleven folded:** `window.from` null on an empty `periods`; `roster.first_start` /
`last_start` null on a zero-period org; `months` is the page's query param and not a response field;
`window.to` recorded as permanently dead; `analyzed=false` scoped to **overlap** analysis only;
test 23's fixture pinned to a plain member (`_require_admin` passes `is_superadmin`);
`referenced_periods`' example given an in-window id; the union precedent moved off `schemas/dashboard.py`
(an INPUT union whose variants have identical fields) onto `report_layout.py` / `scenario.py`, with a
per-kind explicit mapping mandated because `no_open` carries `period_ids=()` while both refusal
markers carry `None`; the absent `backend/tests/routers/conftest.py` recorded; 234b's Pydantic union
required to derive from or assert against `AnomalyKind` (`_KIND_ORDER` is already a second copy); and
the query budget stated at ~402 round trips, with revision 6's "N extra queries" worry corrected
(`period_effective_end` costs zero queries on a closed row).

**The design pass**, run against `PRODUCT.md`, `DESIGN.md`, `frontend/lib/styles.ts` and
`SettingsLayout.tsx`, is folded into **§1.1** and replaces the placeholder layout intent. Its
load-bearing discovery is structural rather than visual: **`effective_end != counting_through` can
only occur on an OPEN row** (`billing_service.py:607-614`), and under `duplicate_open` on several,
which is why `text-accent` was always the wrong reach for the divergence treatment. One new
primitive (a `warning` banner), no new token, no new colour, and three explicit rejections recorded
with their reasons so a later round does not re-propose them.

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
| **Deliverables** | `load_complete_roster`, `kernel_derived_end`, `find_period_anomalies`, `period_status`, the `PeriodAnomaly` / `CompleteRoster` / `RosterRow` types, the `AnomalyKind` / `PeriodStatus` literals, `OVERLAP_ANALYSIS_CAP` / `OVERLAP_EMISSION_CAP`, the marker payload schema, the anomaly ordering, the `CompleteRoster` AST guard, the `ORDER BY` source guard, residual R1 | the route, D6/D7/D8/D8a display windowing and aggregates, D9 gating, the §2.5 response body, `referenced_periods`, the page |
| **Sections** | §2.2, §2.3, §2.4, §2.4a, §2.5's kernel types and marker table, D2, D4, D5 | §1.1, §2.1, §2.5's response body, D1, D3, D6, D7, D8, D8a, D9, D10 |
| **Tests** | 1-13, 13a-13e, 14, 14a, 14b | 15-26, 27-31, **32-34** (added in revision 7) |
| **Contains** | no route, no page, no display window, no aggregates, **no window vocabulary** | no changes to the kernel |
| **Order** | ✅ **SHIPPED, `15faa922`** | open now |

**⚠ Never run the two in parallel.** 234b's tests consume 234a's contract as frozen. Parallel work
against an unfrozen signature is exactly how a contract drifts, and a drifted kernel signature is
what forces a rewrite.

**Test 12 (the status partition) lives in 234a**, as a service-level test of `period_status` with
`today` injected. 234b keeps only test 15, a thin assertion that the route emits the field. The
partition is a kernel fact and should not first be tested through an HTTP round trip.

### 8.1 The frozen kernel contract

Five items are **frozen by 234a** and 234b consumes them unchanged.

1. **The FOUR signatures** (§2.2, §2.3): `load_complete_roster`, **`kernel_derived_end`**,
   `find_period_anomalies` and **`period_status`**. ⚠ **`kernel_derived_end` joins the freeze in
   revision 5**, because test 11 asserts on it by name and a closure is not a contract.
   ⚠ **`period_status` joins in revision 6 (R-S4):** revision 5 froze "the three signatures" and
   omitted it, while §8 listed it as a 234a deliverable and D2 shipped it. Its signature is
   normative and it constrains 234b:

   ```python
   def period_status(row: RosterRow, *, today: datetime.date) -> PeriodStatus
   ```

   ⚠ **It takes a `RosterRow`, NOT a `BillingPeriod`.** 234b builds §2.5's `referenced_periods`
   entries — which each carry a `status` — and must therefore reach for the roster's row tuples, not
   for ORM entities it happens to have in the session. That is deliberate: the row tuples are the
   ones `load_complete_roster` guarantees are complete and ASC.

   `find_period_anomalies` is **PURE and SYNC**:
   it takes no session, so `period_spend_window_end` and `period_effective_end` are both structurally
   unreachable from it. ⚠ **That unreachability is a fact about the FUNCTIONS, not about the
   SEMANTICS** (F4): a floored end is one pure line away, and what fences it is **test 11's fixture
   clause (e)**, not the signature.
2. **The `CompleteRoster` type and the completeness precondition it carries** (§2.2). One construction
   site in `backend/app/`, row tuples not ORM entities. ⚠ **Enforcement is test 14's AST guard**, not a
   type checker; the "type error" framing is struck (F3). Test 22 is the route-level belt, and
   `roster.period_count ≡ len(roster.rows)` (§2.5) is what makes that belt bite.
3. **`today` is injectable and required.** No `date.today()` inside `find_period_anomalies`,
   `kernel_derived_end` or `period_status`. The route resolves the clock once and passes it down
   (D8a). ⚠ **Fenced by test 14a**, added in revision 5; this item had no assigned test in revisions
   1 through 4.
4. **The marker payload schema** (§2.5's nine-kind table), including `gap`'s and `overlap`'s pinned
   dates, `overlap_emission_capped`'s `overlap_count`, the anomaly list's pinned **total** ordering,
   and §1.1's three marker classes. ⚠ **The ordering is 234a's, and §2.5's blanket "everything else
   is 234b" no longer sweeps it up** (revision 6, R-S4).
5. **Four module-level NAMES**, added in revision 6 (R-S4) because 234b's response model and its
   threshold tests need them and revision 5 named none of them: **`AnomalyKind`** (whose `Literal`
   declaration order **is** the sort order — reordering it silently reorders every response),
   **`PeriodStatus`**, **`OVERLAP_ANALYSIS_CAP`** and **`OVERLAP_EMISSION_CAP`**. 234b imports the
   two caps rather than restating `2000` / `5000`, or its threshold tests pin a copy of a constant
   instead of the constant.

**⚠ The freeze NO LONGER covers `open_row_ids` or the out-of-window predecessor convention. Both are
deleted.** `no_open` and `duplicate_open` derive from the complete roster like every other marker,
and there is no predecessor to fetch because there is no window to be outside of.

**Rationale.** Without these five, 234b would be forced to **rewrite the kernel**: a
fetching-and-windowing kernel cannot be reused by the sweep script, a roster-shaped input that is not
nominally complete silently degrades to whatever the caller passes, a `date.today()` kernel cannot be
clock-injected, and an unspecified payload gets reshaped by whoever renders it. A forced downstream
rewrite of an already-merged unit is precisely the failure mode the architects used to **reject
TBD-233**, and revision 3 came one sign-off away from repeating it inside this ticket's own split.
