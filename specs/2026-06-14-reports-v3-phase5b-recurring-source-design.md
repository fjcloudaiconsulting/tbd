# Reports v3 — Phase 5b: RecurringSource

**Date:** 2026-06-14
**Status:** design approved (inline brainstorm), building
**Scope:** add a third `ReportSource` (`recurring`) — the symmetric follow-on to AccountsSource (Phase 5, #450) on the same registry. All shared plumbing (enum widening → per-source `validate()` with op-enforcement → catalog-driven picker → sort → date-omission) is proven; this is a near-mechanical mirror.

## Goal
Let users build report widgets over their **recurring-transaction templates** — total/average recurring amount and template counts, grouped by category, account, currency, type, frequency, or active status. Proves the registry scales to a third source and completes the "new data sources" remainder of Reports v3.

`recurring_transactions` (one row per template): `amount` (Numeric, in the account's currency), `type` (income/expense — NO transfer), `frequency` (Frequency enum: weekly/biweekly/monthly/quarterly/yearly), `account_id`, `category_id`, `next_due_date`, `auto_settle`, `is_active`. **No tags** (obs 20439). Source = `recurring_transactions JOIN accounts JOIN categories`, org-scoped on `recurring_transactions.org_id`.

## Reportable surface

### Dimensions
| Key | Source column | Note |
|---|---|---|
| `category` | `categories.name` | existing key |
| `account` | `accounts.name` | existing key |
| `currency` | `accounts.currency` | existing key — cross-currency grouping lever |
| `txn_type` | `recurring_transactions.type` | existing key (income/expense) |
| `frequency` | `recurring_transactions.frequency` | **new** (weekly…yearly) |
| `recurring_active` | `recurring_transactions.is_active` → "Active"/"Inactive" | **new**, kind `boolean` (mirrors `account_active`) |

### Measures
| Key | Label | Agg | Field | Format |
|---|---|---|---|---|
| `sum_amount` | Total recurring amount | sum | `amount` | currency |
| `avg_amount` | Average amount | avg | `amount` | currency |
| `count_recurring` | Recurring count | count | `id` | number |

No new `MeasureField` (reuses `amount`/`id`).

### Filters
`account_id` (in), `category_id` (eq/in), `currency` (eq/in), `txn_type` (eq/in), `frequency` (eq/in — new), `recurring_active` (eq — new), `amount` (between/gte/lte). **No `date`** — recurring is treated date-less for the canvas bar (the canvas date range is transaction-date scoped, not template due-date). `next_due_date` filtering is a possible follow-on, out of scope.

## Product decision (operator-confirmed 2026-06-14)
**Include all templates by default; expose `recurring_active` as a dimension + filter** so users can exclude paused ones — matching the AccountsSource precedent (consistent across sources). A "total recurring" includes paused templates unless filtered.

## Architecture
- **Enum widening** (`reports_enums.py` + `reports_query.py`): `Dataset.RECURRING="recurring"`; `Dimension.FREQUENCY="frequency"`, `Dimension.RECURRING_ACTIVE="recurring_active"`; `FilterField.FREQUENCY="frequency"`, `FilterField.RECURRING_ACTIVE="recurring_active"`. Filter coercion: `frequency` → validated against the Frequency enum values; `recurring_active` → bool (mirror `account_active`). **No migration** (JSON-column enums).
- **`app/reports/sources/recurring.py`** — `RecurringSource`: catalog + org-scoped compiler over `recurring_transactions JOIN accounts JOIN categories`. `recurring_active` normalized via SQL `case(...)` to "Active"/"Inactive". Honors `query.sort` + `RecurringTransaction.id` tiebreaker. Filters op-enforced via the shared `validate_against_catalog`. `validate()` delegates to `validate_against_catalog`. Self-registers.
- **Frontend** (mostly free — picker is catalog-driven): `types.ts` `Dataset += "recurring"`, `Dimension += "frequency" | "recurring_active"`; add labels for `frequency`/`recurring_active` to the dimension label maps + `DIMENSION_HEADERS` (`series.ts`) so `tsc` is satisfied. `pruneFiltersToSource` already drops tag/date when switching to recurring (no new mapping — `frequency`/`recurring_active` are not WidgetFilters keys). The per-widget filter EDITOR keeps its existing controls (a `frequency` filter control is a possible follow-on; grouping by frequency works immediately via the catalog-driven dimension picker).

## De-scoped (flagged, not silently dropped)
- **`category_master`** grouping for recurring (needs the parent-category self-join + helper extraction from `reports_query_service`) — follow-on; plain `category` covers the common case.
- **`next_due_date`** "due-in-period" filter / time dimensions — follow-on.
- **Per-widget `frequency` filter UI control** — the field is published in the catalog; the editor UI control is a follow-on. Grouping by frequency is available now.

## Tests
Backend: catalog exactness; org isolation; `frequency` + `recurring_active` dimension grouping (boolean → "Active"/"Inactive" stable strings); sum/avg/count measures; IN filters (account_id/category_id/currency/txn_type/frequency); `recurring_active` eq filter; amount between; op-reject (e.g. `frequency between` → 422); cross-source reject (`balance` measure / `frequency` dim on transactions → 422); date-drop tolerance; enum drift guard; registry exhaustiveness (`recurring` registered). Frontend: source picker lists Recurring + switches dims/measures; switching to recurring prunes tag/date filters; multi-series + KPI switch.

## Process
Subagent-driven, review gate per task; final fleet/targeted review before merge (mirrors a pattern already fleet-reviewed in #450). Backend tests in the isolated `-p team-recsrc` stack; frontend full `eslint . --quiet` + `tsc --noEmit` + `vitest run`.
