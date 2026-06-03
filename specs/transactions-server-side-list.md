# Transactions server-side list contract + shared pagination/sort

**Date:** 2026-06-03
**Status:** Spec — ready for implementation plan
**Branch target:** `feat/transactions-server-side-list` (off `main`)

## Problem

The transactions page is the last table in the standardization fan-out still on
its own bespoke pagination. The list endpoint
(`GET /api/v1/transactions`, `routers/transactions.py:65`) returns a **bare
list with no total count and no sort params**. The frontend
(`frontend/app/transactions/page.tsx`) fetches `limit=PAGE_SIZE+1&offset=...`
and derives `hasMore` from the +1 probe, rendering a bespoke
`Previous / Page N / Next` control (no per-page selector, no page count). Sorting
is done **client-side over only the current page** (`useMemo` on the visible
rows) — a latent bug that becomes obvious with real pagination ("sort by amount"
would reorder just one page of rows).

The other tables (Recurring, Accounts client-side; Admin Users server-side) use
the shared `Pagination` / `SortableHeader` / `useTableState` primitives shipped
in #388/#389. The shared `Pagination` component **requires a real `total`**
(it renders "Page X of Y · N total"), so adopting it forces a backend change.
Fetching all rows to slice client-side (the Recurring/Accounts pattern) is not
viable for a large, server-paginated dataset.

## Goal

Migrate transactions to the **#389 server-side `ListEnvelope` contract** (Admin
Users is the canonical reference): the endpoint returns `{items, total}` and
supports server-side sort via `resolve_order_by`; the page uses the shared
`Pagination` (numbered pages + per-page selector) and server-side
`SortableHeader`. Fix the page-local sort bug. Keep the page's existing
**localStorage** persistence model (`usePersistedSort` / `usePersistedFilters`),
not Admin Users' URL-state model. Preserve all existing behavior: filters, bulk
select/delete, inline edit, recurring-promote, transfer pairing.

## Decisions (locked 2026-06-03)

- **Full server-side** sort + pagination (not a total-count-only reskin).
- **Sort whitelist (all 6 current columns):** `date`, `amount`, `description`,
  `status`, `account_name`, `category_name`. `account_name`/`category_name`
  require inner joins to `Account`/`Category` in the page query.
- **`date` sorts by `effective_period_date_expr()`** (consistent with the
  existing date filter + period bucketing), not raw `Transaction.date`.
- **Stable ordering:** `tiebreaker=Transaction.id.desc()` always appended.
- **Default page size 25** (up from 20, a valid `PAGE_SIZE_OPTIONS` value),
  persisted in localStorage; **page number is ephemeral** (reset to 1 on
  filter/sort change; not persisted).
- **Selection clears on page navigation and on sort change** (filter change
  already resets the page). With server-side paging the client only holds the
  current page's rows, so cross-page selection would show an invisible
  "N selected".
- **Persistence stays localStorage** (`usePersistedSort`/`usePersistedFilters`),
  matching the transactions page today; URL params remain inbound-deep-link only.
- **No change** to single/bulk delete semantics, transfer pairing, inline edit,
  or recurring-promote.

## Current behavior (baseline)

- **Endpoint** `routers/transactions.py:65` — `response_model=list[TransactionResponse]`,
  params: `account_id, category_id, type, status, date_from, date_to, search,
  tags, tags_exclude, tag_match, limit (le=200, default 50), offset (ge=0)`. No
  sort params. Calls `svc.list_transactions(...)` and returns
  `[svc.to_response(tx) for tx in txns]`.
- **Service** `transaction_service.list_transactions` (`:1787`) — builds
  `q = select(Transaction).options(*_load_opts()).where(org_id==...)` then chains
  `.where(...)` per filter (incl. category master-includes-subs, `search`
  description+amount, tag subqueries, `effective_period_date_expr()` for dates),
  ends with order/limit/offset. `_load_opts()` uses `selectinload` for
  account/category/tags. Returns `list[Transaction]`. No count.
- **Frontend** `transactions/page.tsx` — `PAGE_SIZE = 20` (`:35`); `page`
  useState (`:88`); fetch builds `limit=${PAGE_SIZE+1}&offset=${p*PAGE_SIZE}`
  (`:270`), `hasMore = data.length > PAGE_SIZE`; bespoke controls (`:2039-2046`).
  `usePersistedFilters`/`usePersistedSort` (localStorage) at `:141`/`:172`;
  `SortField` = date|description|account_name|category_name|status|amount with
  per-column `SORT_DEFAULTS` (`:42-57`). Client-side sort `useMemo` (~`:837`).
  `setPage(0)` on filter change (`:362`). Selection: `selectedIds: Set<number>`
  (`:222`), `allPageSelected`/`togglePage` per visible page (`:555-579`),
  `clearSelection` (`:581`); bulk delete reloads `loadTransactions(page)`.
- **Reference (server-side):** Admin Users (`frontend/app/admin/users/page.tsx`)
  + `admin_users_search_service.list_users` + `resolve_order_by` +
  `UsersListResponse {items,total,limit,offset}`.
- **Shared primitives:** `Pagination` props `{page, pageSize, total, onPageChange,
  onPageSizeChange, pageSizeOptions?}` (1-based, requires `total`);
  `pageCount(total, pageSize)`, `PAGE_SIZE_OPTIONS=[10,25,50,100]` in
  `lib/hooks/use-table-state.ts`.

## Proposed behavior

### 1. Backend

**`transaction_service.list_transactions`** — refactor to:
- Build the list of WHERE clauses once into a local list (extracting the current
  inline `.where(...)` chain), so both queries share identical filters.
- Page query: `select(Transaction).options(*_load_opts())` + the where clauses
  + (when sorting by `account_name`/`category_name`) `.join(Account)` /
  `.join(Category)` + `.order_by(*resolve_order_by(...))` + `.limit/.offset`.
- Count query: `select(func.count()).select_from(Transaction)` + the **same**
  where clauses (no joins needed unless a filter requires them; the existing tag
  filters use `Transaction.id.in_(subquery)` so no join is needed for count).
- New signature params: `sort_by: str | None = None, sort_dir: str | None = None`.
- `resolve_order_by(sort_by, sort_dir, allowed=ALLOWED_TX_SORT,
  default_key="date", default_dir="desc", tiebreaker=Transaction.id.desc())`
  where `ALLOWED_TX_SORT = {"date": effective_period_date_expr(), "amount":
  Transaction.amount, "description": Transaction.description, "status":
  Transaction.status, "account_name": Account.name, "category_name":
  Category.name}`.
- Return `tuple[list[Transaction], int]` (items, total).

**`routers/transactions.py`** — `list_transactions`:
- `response_model=ListEnvelope[TransactionResponse]` — reuse the generic
  `ListEnvelope[T]` shared contract from `schemas/common.py` (that is the point
  of #389; verify its field names — expected `items: list[T]; total: int;
  limit: int; offset: int` — and match the frontend to them). If the admin
  reference turned out to use a concrete `UsersListResponse` instead of the
  generic, prefer the generic here unless it does not serialize cleanly as a
  FastAPI `response_model`, in which case mirror the concrete admin shape. The
  plan must confirm which during implementation by reading `schemas/common.py`
  and the admin router.
- Add `sort_by: str | None = Query(default=None)`, `sort_dir: Literal["asc",
  "desc"] | None = Query(default=None)`.
- Wrap the service call so a `ValidationError` (invalid_sort_by/dir) maps to
  HTTP 400, mirroring `admin_users`.
- Return `TransactionListResponse(items=[to_response(tx) for tx in items],
  total=total, limit=limit, offset=offset)`.

### 2. Frontend (`transactions/page.tsx`)

- Fetch parses `{items, total}`; store `total` in state; drop the `+1` probe and
  `hasMore`.
- Send `sort_by=sortField&sort_dir=sortDir` on every fetch; add them to the
  fetch effect deps. **Remove the client-side sort `useMemo`** (rows render in
  server order).
- `page` is 1-based component state; the fetch offset is `(page-1)*pageSize`.
  Reset `page` to 1 on filter change (existing effect) and on sort change.
- Add `pageSize` state (default 25) persisted in localStorage (small dedicated
  key alongside the existing persisted-keys); `onPageSizeChange` sets size +
  resets page to 1.
- Replace `:2039-2046` bespoke controls with
  `<Pagination page={page} pageSize={pageSize} total={total}
  onPageChange={setPage} onPageSizeChange={...} />`, rendered when
  `total > pageSize || page > 1`.
- After every refetch (incl. post-mutation), clamp:
  `if (page > pageCount(total, pageSize)) setPage(pageCount(total, pageSize))`.
- `clearSelection()` on page change and on sort change.
- `SortableHeader` usage stays; `handleSort` updates `usePersistedSort` and
  resets page to 1.

### 3. Preserved (must not regress)
Filters (all current params + tag filters + period), bulk select/delete (+
empty-page snap-back), inline edit, recurring-promote, transfer
link/mark/unpair modals, refetch-on-mutation. These keep working off the new
`{items}`.

## Out of scope
- URL-as-state (transactions stays localStorage-persisted).
- New filter controls (separate paused reports-filters thread).
- Changing delete/transfer/recurring semantics.
- Cross-page "select all matching" (selection stays per-page).

## Testing

**Backend (`backend/tests/...`, in-memory SQLite)**
- Envelope shape: `{items, total, limit, offset}`; `total` is the full filtered
  count, independent of `limit`.
- `total` mirrors filters: applying account/category/type/status/date/search/tag
  filters changes `total` consistently with `items`.
- Server-side sort per whitelisted key (asc + desc), including
  `account_name`/`category_name` ordering via joins.
- Tiebreaker: rows with equal sort values come back in stable `id desc` order;
  pagination across pages has no gaps/dupes.
- `sort_by`/`sort_dir` invalid → 400.
- Org-scoping: a second org's rows never counted in `total` or returned.

**Frontend (`frontend/tests/app/...`, Vitest/RTL)**
- Renders shared `Pagination` showing real total / page count.
- Page navigation refetches with the correct `offset`.
- Sort header click sends `sort_by`/`sort_dir`, resets to page 1, and clears
  selection.
- Per-page selector changes size, resets to page 1, persists across remount.
- Selection clears on page navigation.
- Deleting the last row on the final page snaps back to the new last page.
