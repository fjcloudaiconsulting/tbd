# Transaction transfer collapse — server-side, before pagination (TBD-268)

Status: implemented
Date: 2026-07-30
Ticket: TBD-268

## 1. The defect, and its real severity

The server paginated; the client then hid one leg of each transfer pair. The
hide therefore ate rows out of an already-truncated page.

- `frontend/app/transactions/page.tsx` requested exactly `limit=${pageSize}`.
- It then built `selectionHiddenIds` = rows where
  `linked_transaction_id && id > linked_transaction_id`, and filtered both
  `selectableTxs` and `visibleTxs` through it.

The operator reported "25 per page renders 21". **That understates the bug by
an order of magnitude.**

`transaction_service.create_transfer` adds the expense leg before the income
leg and flushes both together, so **the income leg reliably takes the higher
id**. Under `?type=income`, or under `?account_id=<the account holding the
higher-id leg>`, *every returned row* satisfied `id > linked_transaction_id`.
The list rendered its empty state while Pagination said "N total" — a total
blackout, not a short page.

Fence `test_b3_partner_filtered_out_keeps_the_higher_id_leg` reproduces this
exactly; against the ticket's own proposed predicate it fails `assert [] == [2]`.

## 2. `linked_transaction_id` is NOT a transfer marker

This is the most important finding, and it invalidates the predicate the ticket
proposed.

The column has **two writers with different semantics**:

| Writer | Direction | Meaning |
| --- | --- | --- |
| `transaction_service._link_pair` | **bidirectional** | a real transfer pair (also import pairing); `unpair_transactions` clears both |
| `reconciliation_service._apply_match` | **one-way** (only `tx` points at the target) | "this imported row is the same thing as that existing one" |

The one-way-ness is load-bearing and documented as such in `_apply_match`'s
docstring and in the `Transaction.linked_transaction_id` model doc block:
`transaction_filters.balance_contribution_filter` uses that direction as a
discriminator to tell a reconcile match (drop from balance reconstruction)
apart from a real transfer leg (keep). Both docs say explicitly: do not "fix"
it into a bidirectional link.

**Consequence: the collapse predicate MUST test mutuality.** A predicate keyed
only on `id > linked_transaction_id` suppresses reconcile-matched rows as if
they were transfer legs. That was **already happening on `main`** — a
reconcile-matched imported row whose id exceeded its target's was invisible on
`/transactions` — and moving the rule to the server unexamined would have baked
it into `total` as well.

`transaction_filters.balance_contribution_filter` already encodes the mutuality
test with a module-level alias `_bcf_partner`; the new clause mirrors that shape.

## 3. The predicate

`transaction_service._transfer_collapse_clause(filtered_ids_subq)` keeps a row
when **any** of:

1. it is not linked at all;
2. it is self-linked (corrupt data — never drop a row we cannot pair);
3. its partner does not link back (a reconciliation match, a dangling link, or
   a cross-org link);
4. it is the lower-id leg;
5. its partner is not in the filtered set.

**Exactly-one proof.** For a reciprocal, same-org, non-self pair `(a, b)` both
inside the filtered set, branches 1/2/3/5 are false for both legs, and branch 4
is true for exactly one of them because ids are a strict total order and
`a.id != b.id`. Nothing in the predicate depends on the type/amount pair
invariants, so corrupt pair data **over-renders** (visible, fixable) rather
than vanishing.

**Branch 5 is what fixes the reported bug.** When a filter excludes the
partner, this leg is the transfer's only representative and must render, or the
row is reachable from nowhere.

**Branch 2 is not optional.** Trace a self-link without it: `IS NULL` false,
`id < id` false, and the partner (itself) *is* in the filtered set so the
mutuality `EXISTS` succeeds — the row disappears entirely. Unreachable via
sanctioned paths (`_link_pair` invariant 7; `_apply_match` rejects
`match_id == tx.id`) but reachable by direct SQL, and a vanishing row is the
worst failure mode for a ticket about vanishing rows.

Branch 5 uses `NOT IN` over a PK column: `select(Transaction.id)` is NOT NULL,
so it can never evaluate to the SQL NULL that silently swallows rows.

### Shape constraints

- **Correlated `EXISTS`, not `GROUP BY`.** Production is MySQL 8 with
  `ONLY_FULL_GROUP_BY` on by default while tests run on SQLite, and a grouped
  query cannot feed the `selectinload` eager loads in `_load_opts()` that
  `to_response` depends on.
- **`_apply_transaction_filters` was NOT modified.** The filtered id set is
  materialised by calling that same function a third time with the same
  `filter_kwargs`, so its documented guarantee ("used for BOTH the page query
  and the count query so `total` is computed over the same filtered set") is
  preserved by construction. The rejected alternative — parameterising it on
  `model=` so the filters could be inlined into a correlated `EXISTS` — is more
  elegant and is noted in the code as the fallback if profiling ever disagrees,
  but it is a ~12-site mechanical rewrite of the single function that *is* the
  `items`/`total` agreement contract.

### Measured correction to the design brief

The brief asserted the org clause inside the `EXISTS` guards against cross-org
collapse. **Measured during the injection gate, that is not individually true.**
As called from `list_transactions` the two org clauses are *mutually
redundant*: removing only the `filtered_ids` org clause leaves the `EXISTS` org
clause to fail the row open; removing only the `EXISTS` org clause leaves
branch 5 to do it, because a cross-org partner can never be inside an
org-scoped filtered set. `test_b7` goes red only when **both** are removed.

Both clauses are kept anyway — `_transfer_collapse_clause` is a reusable clause
builder and the `EXISTS` must stand on its own if ever paired with a
differently scoped subquery — but the redundancy is now recorded in the code
and in the fence's docstring rather than assumed away.

## 4. `total`, and the join asymmetry

The **same clause object** is applied to `page_q` and `count_q`, so `total`
equals the number of rows the client will render.

Separately fixed: the `account_name` / `category_name` sort attached an
**INNER** join to `page_q` only. An inner join can drop rows from `items` that
`count_q` still counts, breaking the guarantee this ticket exists to establish.
Both are now `isouter=True`. Adding the join to `count_q` instead was rejected:
that would make the count depend on the sort key, which is worse.
`to_response` already coerces a missing account to `""`.

`backend/app/schemas/common.py` was NOT touched: its `ListEnvelope` docstring
stays literally true because the collapse IS part of the filtered query, and
the generic is shared with six other routers.

## 5. Opt-in, default false

`GET /api/v1/transactions?collapse_transfers=true`. **Default false is
load-bearing**, for three reasons:

1. It closes the entire regression class for the sum consumers with one word.
   Every aggregate caller that reduces by `tx.account_id` needs BOTH legs,
   because each leg sits on a **different** account — collapsing would zero an
   account's column.
2. It fails safe in both directions.
3. `GET /api/v1/transactions` is reachable by **superadmin Personal Access
   Token** (`deps.py` routes `pat_`-prefixed bearers through `auth/pat.py`), so
   this is an external contract with unknown consumers.

### Which callers opt in

| Caller | Flag | Why |
| --- | --- | --- |
| `transactions/page.tsx` list request | **on** | the rendered list |
| `dashboard/page.tsx` page fetch + `limit=200` snapshot | **on** | see below |
| `DashboardDataProvider` page fetch + snapshot | **on** | see below |
| `accounts/page.tsx` `fetchAll(status=pending)` | **off** | `pendingByAccount` reduce keyed by `account_id` |
| `dashboard/page.tsx` + provider pending `fetchAll` | **off** | same |
| `lib/pagination.ts` | **never** | generic helper |

The snapshots opt in because they are **not** one-shot sum sources: a single
memo serves both (`const txSource = chartFilter ? allTransactions : transactions`),
so under a chart filter the snapshot *becomes* the rendered source. Collapsing
both let the dedupe memo be deleted rather than forked. Proven safe for the
snapshot's other consumer: `donutDataRaw` already filters
`tx.linked_transaction_id == null`, and the collapse only ever removes rows
*with* a non-null link, so the donut is bit-identical.

## 6. `linked_account_name`

After the collapse the partner is **never** in the page, so a page-local
`txMap` lookup returns `undefined` on every transfer and the account cell
degrades to a bare name.

Of the four consumers of the partner object, three already handled its
absence (`startEdit` and `openUnpairModal` fall back to
`GET /api/v1/transactions/{id}`; the two folds are synchronous). The two folds
need only `account_name`, so **one scalar** was added rather than a nested
object — nesting a `TransactionResponse` in itself invites unbounded recursion,
and the async paths already fetch a richer partner on user action.

`to_response` is shared by every transaction endpoint and must not trigger a
lazy load (`MissingGreenlet` in async), so it probes the loaded relationship via
`tx.__dict__.get("linked_transaction")` — the same idiom `_link_pair` already
uses. The eager load is gated on `collapse_transfers` inside
`list_transactions`; `_load_opts()` is untouched so other call sites pay
nothing. `selectinload` batches: two extra queries per page, not N+1.

The field is populated **only for a mutual, same-org, non-self link**, which
makes it the client's mutuality-verified "this is a real transfer" signal.

## 7. Client rules

**The invariant, written down: the client renders every row the server
returned; it never removes one.** Any client-side removal after a server LIMIT
reintroduces short pages.

Deleted from `transactions/page.tsx`: `selectionHiddenIds`, `selectableTxs`,
`visibleTxs`, `txMap` — re-pointed at `transactions`, with no
`const visibleTxs = transactions` alias left behind (an alias invites the next
reader to re-add a filter). Same in `dashboard/page.tsx` and
`DashboardDataProvider.tsx`, whose `txMap` was also removed from the context
type.

Both dashboards' empty states were keyed off the RAW page array
(`transactions.length === 0`), so a chart-filtered page could render zero rows
with no empty state — a blank card. Both now key off the rendered list.

### The arrow rendered backwards

`{tx.account_name} → {linkedTx.account_name}` rendered on whichever leg
survived. `create_transfer` happens to give the expense (source) leg the lower
id so it read correctly, but `pair_existing_transactions` and
`convert_and_create_leg` link arbitrary rows — and **when the income leg holds
the lower id, the arrow rendered destination → source**. Now derived from
`type`, not from survivor identity (the partner's type is the opposite by
`_link_pair` invariant 3):

```tsx
const [fromAcct, toAcct] = tx.type === "expense"
  ? [tx.account_name, tx.linked_account_name]
  : [tx.linked_account_name, tx.account_name];
```

### The Unlink gate — a live data-corruption path closed

"Unlink transfer" and `openUnpairModal` now gate on `linked_account_name != null`
rather than `linked_transaction_id !== null`.

Previously: a reconcile match against a *newer* row survived the client hide,
rendered as a transfer, offered Unlink — and `unpair_transactions` never checks
reciprocity, so it silently rewrote **both** rows' `category_id`,
recategorizing an unrelated canonical transaction.

**The server-side reciprocity check in `unpair_transactions` is a separate
ticket.** This change only closes the path that reaches it from the list.

## 8. Sort order — accepted cost

The survivor is chosen **by id, never by sort key**; the collapsed row sorts by
the surviving leg's key. A sort-dependent survivor cannot be expressed in
`count_q` (which has no ordering) and would make `LIMIT`/`OFFSET` paging
produce gaps and duplicates when a pair's survivor flips between windows.

So under `sort_by=account_name` a pair spanning Zeta/Alpha sorts under the
**surviving leg's** account, and may not appear where an alphabetical scan
expects it. `amount` is order-neutral (both legs positive and equal by pair
invariant 4); `category_name` is shared by `_link_pair`. Documented in the
`Query(description=)`, the `list_transactions` docstring, and the
`models/transaction.py` doc block.

## 9. Fence table + injection evidence

Every fence was confirmed RED against the specific wrong implementation it
names. Green-against-unmodified-`main` is this repo's most-repeated defect.

### Backend — `backend/tests/services/test_transaction_collapse_transfers.py`

| Fence | Kills | Injected | Observed RED |
| --- | --- | --- | --- |
| B1 | server-side no-op; collapse on `page_q` only | collapse on `page_q` only | `assert 10 == 8` |
| B2 | unconditional collapse | flag ignored, collapse always on | `assert 8 == 10` |
| B3 | branch 5 omitted | branch 5 removed | `assert [] == [2]` |
| B4 | predicate without the mutuality `EXISTS` | branch 3 removed | `assert {1} == {1, 2}` |
| B5 | fail-closed predicate | ticket's predicate | `assert [] == [2]` |
| B6 | branch 2 omitted | branch 2 removed | `assert [] == [1]` |
| B7 | org scoping dropped | both org clauses removed | `assert [] == [2]` |
| B8 (12 params) | sort-dependent survivor; join asymmetry | collapse on `page_q` only | `assert 10 == 13` on all 12 |
| B9 | INNER join on the `account_name` sort | `isouter` reverted | `assert {1} == {1, 2}` |
| B10 | collapse after the LIMIT / count not collapsed | collapse on `page_q` only | `assert 30 == 24` |
| B11 | `__dict__` probe regressing to attribute access | probe → `tx.linked_transaction` | see note below |
| B11b | mutuality not gating the response field | branch 3 removed | `KeyError: 2` |
| B12 | ungated eager load | eager load ungated | `assert all(...)` false |
| B13 (router) | default flipped to true | router default `True` | `assert 5 in {1, 2, 3, 4}` |

**Note on B11.** Replacing the `__dict__` probe with plain attribute access
(`tx.linked_transaction`) does **not** redden B11 itself — in the eager-loaded
path the attribute is already populated, so B11 still passes. It reddens **B12
and B13** instead (`assert all(...)` false on both), because the lazy load then
fires on the *ungated* path and populates `linked_account_name` for callers
that never asked for it. So the probe is fenced, but by its siblings rather
than by B11; B11 on its own is a characterization guard. Recorded here rather
than papered over.

**The highest-value single injection** is the ticket's own proposed predicate
(`or_(linked IS NULL, id < linked)`), which reddens **seven** fences at once —
B3, B4, B5, B6, B7, B11b and B13c — five of them with a total blackout
(`assert [] == [x]`).

### Frontend

| Fence | Kills | Injected | Observed RED |
| --- | --- | --- | --- |
| F1 | leftover client hide | hide restored | `expected 24 to be 25` |
| F1/F2/F3 | request not opting in | `collapse_transfers` dropped | `expected 25 to be 24` |
| F5 | `selectableTxs` not re-pointed | hide restored | `expected 24 to be 25` |
| F6 | partner resolved from a page-local map | fold requires a page-local partner object (the pre-fix shape) | text `Checking → Savings` not found |
| F7 | fold direction from survivor identity | direction from survivor | text `Checking → Savings` not found |
| F7b | Unlink gate on the raw column | gate on `linked_transaction_id` | Unlink button present on a reconcile match |
| F4 | aggregate caller opting in | flag added to pending `fetchAll` | `expected length 2, got 1` — ING Joint's pending zeroed |
| F8 | legacy dashboard hide surviving | hide restored | `expected 8 to be 10` |
| F9d | provider hide surviving | hide restored | `expected '1' to be '2'` |

Two of these fences were **themselves wrong first and caught by the injection
gate**, which is the argument for running it:

- **B5** originally pointed the dangling link at id `999999` — *higher* than the
  row's own id, so branch 4 rescued the row and the fence passed against
  predicates it was supposed to kill. Fixed to delete the partner out from
  under a *lower*-id link.
- **F1**'s marker row was appended last in server-sort order and so never landed
  on page 1, where the count is asserted. Moved to the front.

A third process hazard was hit and is worth recording: the full backend suite
was once launched in the background and injections were then run against the
same worktree while it was still executing (it was at 24% when caught). Editing
a source file under a running `pytest` produces a result that cannot be trusted
either way, so that run was killed and re-run cleanly. Relatedly, killing a
`docker compose exec` kills only the local client — the `pytest` inside the
container keeps running and competes for CPU. `ps` is not installed in the
image; scan `/proc/*/cmdline` instead.

### Selector notes (the ticket's selectors were wrong)

- `getAllByRole("row")` returns **zero** matches in this suite: the transactions
  table is built from divs, not a `<table>`.
- Counting by `aria-label="Select transaction {id}"` or `"Delete: {description}"`
  returns **2x** the row count: the desktop grid is `hidden md:block` and the
  mobile list is `md:hidden`, which is CSS-only, so jsdom renders **both**. The
  delete label additionally collides across both legs of a `create_transfer`
  pair, which share one auto-generated description.
- Correct selectors are the existing per-row test ids —
  `tx-row-desktop-{id}` / `tx-row-mobile-{id}` on transactions, and
  `dash-settled-{id}` on both dashboard surfaces.

### Vacuity traps designed around

- **F1** — the corpus must contain a pair surviving via its **higher-id** leg,
  or a leftover client hide is inert and F1 passes with the bug present.
- **F3** — 30 raw / 26 collapsed would be vacuous, since
  `ceil(26/25) == ceil(30/25) == 2`. Used 30 raw / 24 collapsed. Note the real
  page mounts `<Pagination>` only when `total > pageSize`, so the fence asserts
  the bar is **absent** (24 ≤ 25) where the broken build shows
  "Page 1 of 2 / 30 total".
- **F4** — asserts on the account holding the **higher-id** leg, with two
  distinct accounts and a non-zero amount; both legs on one account net to zero
  and right and wrong agree.
- **F6** — the fixture holds **exactly one** row. With the partner also present,
  a leftover `txMap` lookup resolves and right and wrong agree.
- **B3** — asserts the returned **id**, not just the count; only non-vacuous
  because the income leg holds the higher id.

## 10. Follow-ups deliberately not done here

- `unpair_transactions` does not check reciprocity server-side. The client path
  is closed; the server guard needs its own ticket.
- `_apply_transaction_filters(model=...)` parameterisation, if profiling ever
  shows the materialised id subquery is too slow.
