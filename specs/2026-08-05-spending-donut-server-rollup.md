# Spending donut: consume the server rollup (TBD-221) — design note

**Status:** ready to build.
**Date:** 2026-08-05.
**Scope:** a design *note*, not a spec. The full architect ruling is recorded on
TBD-221; this captures only what a builder needs, plus the one thing that changes
production numbers.

---

## 1. What the ticket got wrong

TBD-221 says *"dashboard analytics aggregate client-side over a capped set; fix is
server-side aggregation."* Three corrections:

1. **Only the Spending-by-Category donut aggregates client-side.** Budget bars,
   forecast rows, projections, account/credit/loan tiles and pending totals are
   already server-aggregated and uncapped.
2. **The aggregation already exists and is discarded.** `GET /api/v1/forecast`
   returns a per-category rollup in SQL — `forecast_service.py:266-278`,
   `group_by(Transaction.category_id)` with `reportable_transaction_filter()`,
   uncapped, ungated. Its own comment reads *"Exclude transfer halves so Forecast
   by Category matches the dashboard donut."* Both shells already fetch it and
   type the field `categories: unknown[]`. **The work is to consume it.**
3. **Two defects are wrong today at any row count**, and the ticket names neither.

## 2. The three defects, in severity order

**D1 — wrong exclusion filter (live, every org that has used Adjust Balance).**
The donut filters only `linked_transaction_id == null` (`page.tsx:566`, inside the
`donutDataRaw` memo opening at `:559`).
Every server aggregate uses `reportable_transaction_filter()`, which also excludes
`is_manual_adjustment` and reverted `reconciliation_state`. So a downward balance
adjustment counts as spending in the donut and **not** in the budget bars beside
it. REJECTED rows from the delete-demotion rule likewise.

⚠ `is_manual_adjustment` **is** on the wire (`schemas/transaction.py:99`,
`types.ts:206`); only `reconciliation_state` is not. So this is *half* fixable
client-side — which is the trap. A client fix excludes adjustments, still counts
rejected rows, and then writes a fence pinning the surviving half.

**D2 — wrong window (live, every org with a lapsed period roster).**
`monthTo`'s open-period arm is `projectedPeriodEnd` (`lib/format.ts:49-56`), which
is bit-for-bit the calendar fallback TBD-243 **measured** as dropping a settled row
and replaced. `budget_service._compute_spent` is already bounded by
`period_spend_window_end`. **The budget bars already use the correct window; the
donut is the outlier.**

**D3 — the 200 cap (latent until a period exceeds 200 rows).**
`routers/transactions.py:104` caps `limit` at `le=200`, a hard 422 above. Both
shells request exactly 200 with no paging, ordered `date desc`, so past 200 the
**oldest** rows of the period are silently dropped. The envelope returns `total`
and both shells discard it, so no truncation signal exists.

## 3. The design

**One change fixes all three: the donut reads `forecastProjection.categories`.**
That payload is already fetched, already filtered correctly, already uncapped, and
already carries its own window.

### Window: `window_end` wins, and it costs nothing

`forecast_service.py:336` already returns `"period_end": window_end.isoformat()` on
the **same payload** as the rollup, and the client already types it
(`page.tsx:75`). Totals and window arrive together, so they cannot drift.
**Zero backend work for the window.**

`monthTo` is a display window; `period_spend_window_end` is the analysis domain.
Letting the former define the latter is the trap `reference_analysis_domain_vs_display_window`
names. `monthTo` keeps the *unfiltered* Recent Transactions list only, and its
comment must say "display window; never an analysis bound".

⚠ **On `/api/v1/forecast` failure the donut renders an error state**, reusing the
existing `projectionFailed` UI. It must **not** fall back to client aggregation —
a silent fallback to the wrong number is the defect being deleted.

### Drilldown: a server query, and `allTransactions` is deleted

`allTransactions` has exactly two consumers per shell — `donutDataRaw` and
`visibleTxs` — and zero outside those two files despite being exported on the
context. Both are what this change replaces. **Delete it and the `limit=200`
fetch. That is how the cap dies: by removing the thing that had one.**

```
GET /api/v1/transactions
    ?category_id=<id>&category_match=exact&reportable=true
    &type=expense&status=settled
    &date_from=<period_start>&date_to=<period_end>
    &limit=PAGE_SIZE&offset=…
```

Those predicates reproduce the rollup's WHERE clause exactly, so the paged `total`
equals the slice. **That equality is the fence.**

⚠ **Omit `collapse_transfers`.** `reportable` already excludes every non-null
`linked_transaction_id`, a strict superset. Sending both is a contradiction in the
URL and is how the next TBD-268 gets written.

⚠ **Ordering, and a live constraint on any future widening of this query.** The
URL omits `sort_by`, so the list endpoint applies its default `date desc` over
`effective_period_date_expr()` — `coalesce(settled_date, date)`. The rollup buckets
`executed` rows by `settled_date` **alone**. Those are identical for the
`status=settled` + `reportable=true` rows this query asks for, so the equality
fence holds as written.

**They stop being identical the moment a pending row enters the query.** A pending
row has no `settled_date`, so the coalesce falls back to `date` while the rollup
would not count it in `executed` at all. If a later change adds pending rows to the
drilldown — to match a donut that shows pending, say — the total equality breaks
silently. Re-derive the fence before widening; do not assume it survives.

### Grouping: `category_id`, forced not chosen

The rollup's identity is the id and the drilldown needs it. `chartFilter` becomes
`number | null`; the `tx.category_name === chartFilter` filters are **deleted**,
not converted — the server filters now. The `Filtering: …` badge looks the name up
from the rollup rows already in memory, so there is one source of truth.

Legend ambiguity (two same-named subcategories now render as two slices with one
label) is **TBD-326**, split out so a label question cannot gate a correctness fix.
Correct number with an ambiguous label beats wrong number with a unique one.

## 4. Two backend params — the whole backend change

| Param | Default | Why |
|---|---|---|
| `reportable: bool` | `False` | applies `reportable_transaction_filter()`. Without it the drilldown returns rows the slice excluded and exceeds its own total — D1 relocated. |
| `category_match: Literal["exact","subtree"]` | `"subtree"` | see the landmine below |

⚠ **THE LANDMINE — both architects found this independently.** `category_id` on
the list endpoint is **master-includes-subs** (`transaction_service.py:2610-2630`,
a regression guard for a 2026-05-13 user report), while the rollup groups by the
row's **own** `category_id`. Without `category_match=exact`, clicking a master
slice returns master **plus every sub**, and the list sums to more than the slice
it opened — silently, and only for orgs that put transactions directly on a
master. The default preserves the existing guard; the drilldown sends `exact`.

Both defaults preserve current behaviour, so the backend PR is **additive and
behaviour-preserving** and can merge on its own.

## 5. Explicitly out of scope

| | Why |
|---|---|
| Currency | **TBD-325.** Partitioning the donut alone makes it the only period tile that partitions, recreating the very disagreement this ticket removes. ⚠ Do **not** add `currency` to `TransactionResponse` — it hands the client the ingredient needed to rebuild client-side aggregation. |
| Legend labels | **TBD-326** |
| `le=200` at `routers/transactions.py:104` | **Leave it exactly as it is.** Under this design no total comes from that endpoint, so the cap stops affecting correctness without being touched. Raising it removes a DoS bound from a public endpoint and relocates the bug. |
| `UNIQUE(org_id, name)` on categories | A migration with a data-repair problem on live orgs, and id-keying already makes collisions harmless. |
| Two-shell deduplication | Both copies of the memo are **deleted** by this change. It resolves itself. |
| Any client-side `is_manual_adjustment` filter | It is on the wire, and it is the bait. Don't write it, don't fence it. |
| Truncation-signal plumbing | With no truncation there is nothing to signal. If this survives into the plan, the plan did not delete the snapshot. |

## 6. ⚠ Verification caveat — read before running the reconciliation

`period_spend_window_end`'s own docstring (`billing_service.py:620-630`) records an
**accepted residual**: on a lapsed roster the floored window `[start, today]`
overlaps the historic stubs and double-counts `[derived_end + 1, today]`, widening
by one day per day while the roster stays unconverged.

The donut does **not** have that residual today — `monthTo` is a plain calendar
end, so a lapsed org's donut currently **under**-counts. This design imports a
known **over**-count onto exactly that population.

**So: run the DoD reconciliation on a healthy on-grid account, and a lapsed one
separately as a known-residual check.** If a lapsed account reconciles high, this
design is the cause, and the answer is to converge the roster (TBD-241 /
`BillingCloseJob`) — **not** to move the donut back to `monthTo`, which would
restore two analysis bounds in one product.

## 7. Fences

**Backend (PR A):**

| id | Asserts | Wrong implementation killed |
|---|---|---|
| B1 | `sum(row.executed for row in categories) == executed_expense` on an org with >200 rows in a period | the rollup and the scalar diverging; this is the DoD made executable |
| B2 | `category_match=exact` on a master carrying direct rows **and** subs returns only the direct rows | the landmine — default `subtree` would return the superset |
| B3 | `reportable=true` excludes a manual adjustment and a REJECTED row that `reportable=false` returns | D1; needs **both** row kinds, since `is_manual_adjustment` alone is the half-fix |
| B4 | both params **absent** → byte-identical response to `main` (control) | a change that alters default behaviour on a PAT-reachable endpoint |

**Frontend (PR B):**

| id | Asserts | Wrong implementation killed |
|---|---|---|
| F1 | donut totals come from `forecastProjection.categories`, **not** from any `/transactions` fetch | the swap not actually happening |
| F2 | `apiFetch` is **never** called with `limit=200` | ⚠ inverts the existing fence at `dashboard-data-provider.test.tsx:1792`, which currently asserts it is called exactly once. **This is the fence that stops the cap creeping back.** |
| F3 | with >200 rows in the period, donut total equals the server figure | the donut math fence at `:848-895` has **never** had a >200-row case — which is why this shipped |
| F4 | clicking a slice issues a `category_match=exact` fetch and the list total equals the slice | the landmine, and the donut-vs-list contradiction |
| F5 | `/forecast` failure → donut error state, **no** client-side fallback total | a silent fallback to the wrong number |
| F6 | control: an org under 200 rows renders the same totals as before | a change that only works past the cap |

⚠ **Fences needing archaeology before code** — read what they *assert*, not what
they are named: `dashboard-data-provider.test.tsx:1792`, `:848-895`, `:1360-1400`
and `:1403-1450` (assert the routing being deleted; invert), and
`dashboard-transfer-collapse.test.tsx:214-241`, whose own comment documents the
coupling being removed — it is a fence pinning the design under replacement.

## 8. Sequence

**PR A — backend, additive, no user-visible change.** Two query params + B1-B4.
Safe to merge alone.

**PR B — frontend, atomic, both shells.** Donut reads the rollup; drilldown becomes
a server fetch; `allTransactions` and the `limit=200` request are deleted.

**PR B must be atomic.** There is no ordering of "donut first, list second" that
does not ship a screen where a total disagrees with the list it opens — which is
the DoD.
