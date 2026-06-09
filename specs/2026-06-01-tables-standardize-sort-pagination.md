# Standardize sort, records-per-page, and page-count pagination across all tables

**Status:** draft for review, 2026-06-01.
**Source:** product owner request 2026-06-01 — every table in the system must have sorting, a records-per-page selector, and pagination that shows the number of pages. No exceptions among true row-tables.

## Goal

Every user-facing **row-table** gets, via one shared component:

1. **Column sorting** (click header to sort, toggle asc/desc, visible sort indicator).
2. **Records-per-page selector** — options `10 / 25 / 50 / 100`, default **25**.
3. **Page-count pagination** — Prev/Next plus a visible "Page X of Y" and total-row count.

Consistent look, behavior, and accessibility everywhere. A single `<DataTable>` + `<Pagination>` replaces the current hand-rolled, inconsistent implementations.

## Scope — the 12 tables (from inventory)

| # | Table | File | Route | Today |
|---|---|---|---|---|
| 1 | Transactions | `app/transactions/page.tsx` | `/transactions` | client sort (persisted), fetches all, PAGE_SIZE=20 client paging, no per-page |
| 2 | Admin Users | `app/admin/users/page.tsx` | `/admin/users` | offset paginated (50), total shown, **no sort** |
| 3 | Admin Orgs | `app/admin/orgs/page.tsx` | `/admin/orgs` | offset paginated (50), **no sort** |
| 4 | Admin Subscriptions | `app/admin/subscriptions/page.tsx` | `/admin/subscriptions` | offset paginated (50), **no sort** |
| 5 | Admin Audit | `app/admin/audit/page.tsx` | `/admin/audit` | offset paginated (50), **no sort** |
| 6 | Admin Rate-Limit Overrides | `app/admin/rate-limit-overrides/page.tsx` | `/admin/rate-limit-overrides` | offset paginated (50), **no sort** |
| 7 | Admin Roles | `app/admin/roles/page.tsx` | `/admin/roles` | fetch all, **no pagination/sort** |
| 8 | System Plans | `app/system/plans/page.tsx` | `/system/plans` | fetch all, **no pagination/sort** |
| 9 | System Announcements | `app/system/announcements/page.tsx` | `/system/announcements` | fetch all, **no pagination/sort** |
| 10 | Members (Settings) | `components/settings/MembersSection.tsx` | `/settings/organization` | fetch all, **no pagination/sort** |
| 11 | AI Providers | `app/settings/ai-providers/page.tsx` | `/settings/ai-providers` | fetch all, **no pagination/sort** |
| 12 | Report Table widget | `components/reports/widgets/TableWidget.tsx` | `/reports/[id]` | in-memory client sort + 50/page prev/next |

**Out of scope (not row-tables):** Budgets (cards), Batch entry (form grid), Admin Analytics (charts). Confirmed with owner 2026-06-01.

**Categories is NOT a flat row-table — re-scoped.** Architect review found `app/categories/page.tsx` renders a two-level **master/subcategory tree** (grouped masters with nested sub-rows); the drag interaction is "move a subcategory to a different master" via `PATCH /api/v1/categories/{id}/move`, and there is **no `sort_order` column** on the backend `Category` model. A single flat `<Pagination>` + column-sort does not fit a grouped tree. **Proposal: treat Categories as out-of-scope** for this standardization (same bucket as Budgets cards), since it is not a row-table. Flagged for owner confirmation — this is the one place "every table" meets a surface that isn't actually a table. Net: **11** standardized row-tables, not 12 (and MembersSection is two — see below — so 12 again by a different count).

## Architecture

### Frontend (shared building blocks)

- `frontend/components/ui/DataTable.tsx` — generic table: takes `columns` (with `key`, `header`, `sortable`, `align`, `render`), `rows`, and a `sort` state + `onSortChange`. Renders sortable headers with `aria-sort` and keyboard activation.
- `frontend/components/ui/Pagination.tsx` — per-page `<select>` (10/25/50/100), Prev/Next, and "Page X of Y · N total". Disabled states at bounds.
- `frontend/lib/hooks/useTableState.ts` — holds `{ page, pageSize, sortBy, sortDir }`, defaults `pageSize=25`, optional localStorage persistence per table key (reuse the existing `usePersistedSort` pattern). URL-sync where a table already does it (admin tables) is preserved.

These are presentation + state only; each table wires its own columns and data fetch.

### Backend (uniform list contract)

Standard list query params on every list endpoint backing a table:

- `limit` (default 25; UI offers up to **100**). **Per-endpoint cap stays at its current value** — critically, `/api/v1/transactions` keeps its existing **200** cap because `fetchAll` (`lib/pagination.ts:18`, pageSize=200) and the dashboard month aggregate (`dashboard/page.tsx:255`, limit=200) depend on `>100`. The UI's max option is 100; the endpoint cap is a separate, higher ceiling.
- `offset` (>= 0),
- `sort_by` (string; **validated against a per-endpoint whitelist** of allowed columns — closed set, mirrors the reports AST discipline; unknown value -> 400). UI sort keys map to qualified SQL columns; joined columns (e.g. `category_name`, `account_name` on transactions) require explicit joins + collation-aware ordering.
- `sort_dir` (`asc` | `desc`, default per endpoint).

**Returning `total` — two patterns, chosen to avoid breaking existing consumers (pre-launch, but these breaks are gratuitous):**

- **Admin endpoints** (users, orgs, subscriptions, audit, rate-limit-overrides) already return a dict envelope `{items, total, ...}` (currently inconsistent: rate-limit-overrides omits limit/offset). PR0 introduces a shared **Pydantic** `ListEnvelope` model and standardizes all five on `{items, total, limit, offset}`. Their frontend callers already read `data.items`/`data.total`.
- **`/api/v1/transactions`** keeps its **bare-array** response body (so `fetchAll`, dashboard, accounts, and ~15 tests are untouched) and returns the count via an **`X-Total-Count` response header**. Only the transactions table page reads the header for page-count; aggregate consumers ignore it.
- **Currently-unpaginated endpoints** (roles already returns `{items}`; plans, announcements, members, invitations, ai-providers return bare arrays): migrate to the shared `ListEnvelope`. Each PR that does so **must update its enumerated frontend callers in the same PR** (e.g. `MembersSection.tsx:55,57`, `settings/ai-providers/page.tsx:133`, `system/plans/page.tsx:95`, `system/announcements`). Roles only needs `total` added.

`total` is a `COUNT(*)` under the same filters. At current volume this is cheap; revisit indexes only if a table shows up slow (note: the audit table has **separate** single-column indexes on `event_type` and `created_at`, not a composite — a `sort_by` other than the default may not be index-covered).

A shared helper (`backend/app/services/list_query.py`) centralizes limit/offset clamping + sort-whitelist application. **Contract:** it applies `org_id`/tenant scoping to BOTH the `COUNT(*)` and the page query, so `total` can never leak a cross-org count. Enforced by a PR0 test.

### Server-side vs client-side

- Tables backed by paginated endpoints (admin 1-6) and Transactions: **server-side** sort + page (send `sort_by`/`sort_dir`/`limit`/`offset`).
- Small always-small sets (roles, plans, announcements, members, invitations, ai-providers): still gain the **backend** envelope + sort params for uniformity, but the dataset is tiny so correctness is the point, not scale.
- Report Table widget: stays **in-memory** (data comes from the report query, capped 500). Only its pagination/sort **UI** is re-skinned to match the shared `<Pagination>` and gains the per-page selector.

## Phasing (subagent-driven, sequential with review gates; branch + PR each)

- **PR0 — Foundation + reference implementation.** `DataTable`, `Pagination`, `useTableState`, backend `list_query` helper, plus wire **Admin Users** end-to-end (server sort + per-page + page-count) as the canonical pattern. Backend + frontend tests. **Review gate** before fan-out.
- **PR1 — Admin paginated tables:** orgs, subscriptions, audit, rate-limit-overrides (add sort + per-page + page-count on top of existing pagination).
- **PR2 — Transactions (riskiest; NOT self-contained):** add `limit/offset/sort_by/sort_dir` to `/api/v1/transactions`, keeping the **bare-array body** + new **`X-Total-Count` header**; move sort server-side (join Account/Category for `*_name` sorts, collation-aware). Regression-verify `fetchAll`, `dashboard/page.tsx`, `accounts/page.tsx`; update transactions-page tests. Preserve persisted filters. Sequence after PR0 components are stable.
- **PR3 — Previously-unpaginated:** roles (only add `total`; already `{items}`), plans, announcements, members, invitations, ai-providers — backend `ListEnvelope` + sort + UI, **updating each endpoint's enumerated frontend caller in the same PR**. `MembersSection.tsx` holds **two** tables (members + pending invitations); each gets its own `<DataTable>`.
- **PR4 — Report Table widget UI re-skin** (already has header sort + 50/page prev/next; only add the 10/25/50/100 selector + "Page X of Y" wording — low risk). Categories is out-of-scope per the re-scope above (pending owner confirmation).

Each PR: tests (backend list-contract: limit clamp, sort whitelist, total correctness; frontend: sort toggle, per-page change, page bounds), accessibility (`aria-sort`, keyboard), no em-dashes in copy, update `codebase_shape.md`.

## Out of scope

Virtualized/infinite-scroll tables; CSV export of table contents (separate from reports CSV); saved per-user column layouts; multi-column sort.

## Open questions for review

1. **Categories** — confirm it's out-of-scope (it's a grouped master/sub tree with move-between-masters drag, not a row-table). If you still want pagination there, it needs a bespoke design, not the shared component.
2. Per-table localStorage persistence of page-size/sort — on by default, or only where it exists today (transactions)? (Proposed: on, keyed per table.)
3. Default sort column/direction per table — proposal: keep each table's current implicit order as the default `sort_dir` (e.g. transactions by date desc, audit by created_at desc).
