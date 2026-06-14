# Reports v3 — Phase 5: AccountsSource

**Date:** 2026-06-13
**Status:** design, architect-reviewed (pre-plan)
**Scope:** add a second `ReportSource` (`accounts`) on the PR #441 registry. AccountsSource **only** this round; RecurringSource is a deliberate fast follow-on PR, not in scope here.

## Goal

Make the Reports v3 source registry genuinely pluggable by adding the first non-transactions source. Users can build widgets that report on their **accounts snapshot** — total/average balance and account counts, grouped by account, account type, currency, or active status. This proves the registry's extensibility (a source ships as one new file + a closed-enum widening, no new infra) and is the prerequisite shape for the harder NetWorth source (Phase 6, separate, decision-gated).

`accounts` is a **snapshot table** (one row per account). `accounts.balance` is a stored `Numeric` column, so balance reporting is a single-table read plus one join to `account_types` — **no transaction reconstruction** (that is NetWorth/Phase 6 territory).

## Non-goals

- RecurringSource (follow-on PR, same plumbing).
- NetWorthSource / asset-liability classification / balance reconstruction (Phase 6).
- Multi-currency correctness beyond exposing `currency` as a grouping lever (see §7). Locale-aware formatting / per-feature cross-currency safety remain the future i18n/multi-currency project.
- Time-series over accounts (no `created_at` bucketing — accounts are a snapshot).
- `opening_balance` measures (trivial to add later; left out to keep the surface tight).

## Reportable surface

### Dimensions (group by)
| Key | Label | `kind` | Source column |
|---|---|---|---|
| `account` | Account | `account` | `accounts.name` (existing dimension key) |
| `account_type` | Account type | `account_type` | `account_types.name` (via FK join) |
| `currency` | Currency | `currency` (new kind) | `accounts.currency` |
| `account_active` | Status | `boolean` (new kind) | `accounts.is_active` → `"Active"`/`"Inactive"` |

### Measures
| Key | Label | Agg | Field | Format |
|---|---|---|---|---|
| `sum_balance` | Total balance | sum | `balance` | currency |
| `avg_balance` | Average balance | avg | `balance` | currency |
| `count_accounts` | Account count | count | `id` | number |

### Filters (published in the `/sources` catalog — see §3, S1)
| Field | Ops | `kind` |
|---|---|---|
| `account` (`account_id`) | `in` | `account` |
| `account_type` | `eq`, `in` | `account_type` |
| `currency` | `eq`, `in` | `currency` |
| `account_active` | `eq` | `boolean` |
| `balance` | `between`, `gte`, `lte` | (numeric) |

**No `date` filter** — accounts have no date column. See §6 for how the canvas shared-date bar interacts.

## Architecture

### 1. Closed-enum widening — consolidate first, then widen
`Dataset`, `Dimension`, `MeasureField`, and `Aggregation` are currently **duplicated** in `backend/app/schemas/reports_query.py` (the live-query AST) and `backend/app/schemas/report_layout.py` (the saved-layout JSON validator). They are value-identical today but encode two contracts; widening four enums in lockstep across two files is a silent-drift hazard exactly when we exercise it.

**Step 1 (behavior-preserving):** extract the shared atoms — `Dataset`, `Dimension`, `MeasureField`, `Aggregation` — into a new `backend/app/schemas/reports_enums.py`; both `reports_query.py` and `report_layout.py` import from it. Layout-only enums (`WidgetType`, `WidgetFormat`) stay where they are. This is a mechanical no-op refactor and should be its own first task/commit so it reviews cleanly.

**Step 2 (the widening), on the now-single enum:**
- `Dataset += ACCOUNTS = "accounts"`
- `Dimension += ACCOUNT_TYPE = "account_type"`, `CURRENCY = "currency"`, `ACCOUNT_ACTIVE = "account_active"`
- `MeasureField += BALANCE = "balance"`
- `FilterField += ACCOUNT_TYPE`, `CURRENCY`, `ACCOUNT_ACTIVE`, `BALANCE` (`FilterField` is AST-only, lives in `reports_query.py`)

Enums stay closed → the AST still cannot describe SQL; anything off-whitelist is a 422 at Pydantic parse. **Belt-and-suspenders regardless of consolidation:** a test asserting `set(reports_query.Dataset) == set(report_layout.Dataset)` (and same for `Dimension`, `MeasureField`) so any future drift is a red test, not a blank widget.

**No Alembic migration.** `Dataset`/`Dimension`/`MeasureField` values live inside the `layout_json` / `canvas_filters_json` **JSON** columns, not native DB enums; the query AST is request-body-only and never persisted as a typed column. Widening Python enums touches no DDL. The only DB objects (`accounts`, `account_types`) already exist.

### 2. Validation split — Pydantic = sane, source = in-my-catalog
The current `Measure._validate_agg_field` (`reports_query.py:146`) hardcodes "sum/avg require `field=='amount'`". Widening `MeasureField` with `balance` breaks this. Replace with a **source-agnostic numeric set**:

```python
NUMERIC_MEASURE_FIELDS = {MeasureField.AMOUNT, MeasureField.BALANCE}  # module-level, importable by tests
```

sum/avg require `field in NUMERIC_MEASURE_FIELDS`; count/distinct accept any whitelisted field. This does **not** weaken the whitelist — `field` is still a closed enum typed by Pydantic. The validator was always a *semantic sanity gate*, not the whitelist.

**Per-source validity** moves to the source layer (keeps `schemas/` free of any import on `app.reports.sources`, which would be circular — sources already import the schemas):
- Add `def validate(self, query: ReportsQuery) -> None: ...` to the `ReportSource` Protocol in `base.py`. Make it a **required Protocol method** (not optional / not `getattr`-defaulted) so a future source can't silently ship with no gate. (`@runtime_checkable` checks method presence only, not signature — back it with the registry-exhaustiveness test.)
- Each source validates the AST against the catalog it already publishes: every `query.dimensions` key ∈ its `dimensions()`, `query.measure.field` valid for its `measures()`, every filter field ∈ its `filters()` (S1). On violation, raise `ValueError`.
- `TransactionsSource.validate()` therefore rejects `sum(balance)` and `currency`-dimension etc. with a `ValueError` → 422, even though Pydantic now considers `sum(balance)` numerically sane.

**Router wiring (the 422-vs-500 fix).** Today `run_query` → `_run_source_query` has no try/except; a raw `ValueError` would surface as **500** (confirmed: `main.py` has handlers for Pydantic `ValidationError`, `NotFoundError`, `ConflictError`, `RateLimitExceeded` — none catch a plain `ValueError`). `_run_source_query` must call `source.validate(ast)` before `build_rows` and map `ValueError` → `HTTPException(status_code=422, detail=...)`.

### 3. `/sources` catalog must publish filterable fields (architect S1)
Today the catalog publishes only dimensions + measures (`base.py` `SourceDimension`/`SourceMeasure`). The frontend filter editor and the date-applicability rule (§6) both need each source to declare **which filter fields it accepts, with which ops**. Without it the frontend hardcodes per-source filter knowledge — the exact drift the registry exists to kill.

- Add a `SourceFilter` value object to `base.py`: `field: str`, `label: str`, `ops: list[str]`, `kind: str`.
- Add `def filters(self) -> list[SourceFilter]: ...` to the Protocol.
- Surface it through `/sources`: new `SourceFilterOut` in the response model; `list_sources` serializes `s.filters()` alongside dims/measures.
- This catalog becomes the single source of truth read by BOTH `source.validate()` and the frontend editor.

### 4. AccountsSource.build_rows
New file `backend/app/reports/sources/accounts.py`, self-registering like `transactions.py`. A small compiler over `accounts` + `account_types` (single join on `account_type_id`, which is NOT NULL):
- **Org-scoping is on `accounts.org_id` only** (the hard rule). Join `account_types` purely on the FK; do **not** also constrain `account_types.org_id` (avoids inner-joining away valid accounts; the FK already guarantees same-org). A test must assert an account in org A never appears in org B's results.
- **Boolean → label normalization in Python after fetch**: MySQL returns `1`/`0`, SQLite `0`/`1`; map to stable `"Active"`/`"Inactive"` row keys in Python, never rely on the driver returning a Python bool from a GROUP BY expression. Mirror how transactions `status` (settled/pending) renders today.
- **`build_rows` compiles only its own published filter fields** and structurally ignores anything else — it must be incapable of emitting SQL for `date` (or any non-accounts field). A stray `date` filter results in "filter dropped," never a 500.
- Returns `(rows, meta)` with `row_count` / `truncated` / `query_ms`, coerced to `QueryMeta` at the router exactly like transactions.

### 5. `kind` taxonomy guard
`SourceDimension.kind` / `SourceFilter.kind` are free strings the frontend switches rendering on; a typo is a silent blank-control bug. Add new kinds `currency` and `boolean`, and add a test asserting every published `kind` across all sources ∈ a known set (`{category, account, status, type, tag, time, account_type, currency, boolean}`). (Closing it to an enum is optional; the test is the requirement.)

## 6. Shared date bar interaction (architect Q5 — two-tier)
The canvas shared-date bar (#448) is meaningless for an accounts widget. Lock **both** halves:
- **Primary contract — frontend omits.** `resolve.ts` does not stamp the canvas date filter onto a widget whose source does not declare `date` in its catalog `filters()`. Driven by the catalog, not a hardcoded "accounts has no date."
- **Defense-in-depth — source tolerates.** `source.validate()` uses a **two-tier** rule:
  - A **shared-canvas field** (`date`, and the `account_ids`/`category_ids` carried by `CanvasFilters`) that is not applicable to this source → **silently drop**, do not 422. Rationale: a layout saved on a date-filtered canvas, or a resolve/bar race, must not break the widget.
  - A **non-shared field** in the global `FilterField` enum but not in this source's catalog (e.g. `txn_type` sent to `accounts`) → **reject** (`ValueError` → 422). That is a malformed query, not a shared-bar artifact.
- Define the shared-canvas drop-set explicitly in one place. `build_rows` must also structurally ignore dropped fields (§4). Test: a query with a `date` filter against `accounts` returns rows (date dropped) — not 422, not 500.

## 7. Multi-currency `sum_balance` — documented limitation
`accounts.currency` is per-account and free (`String(3)`, default `EUR`). `sum(balance)` across mixed-currency accounts adds EUR+USD into a meaningless number, yet `format="currency"`. Per operator decision, the chosen mitigation is **expose `currency` as a groupable/filterable dimension** so users can group balance by currency (and avoid mixing) — the lightest option, consistent with the app-wide "grouped, no symbol" stance.

This spec **documents the limitation explicitly**: balance measures are only numerically meaningful when grouped by `currency` (or filtered to a single currency). A non-blocking `meta` warning when a balance measure is requested without a `currency` dimension and the org holds >1 currency is noted as an **optional follow-up**, not in scope (the operator chose the plain dimension over a default-group hint). Proper cross-currency safety is the future multi-currency project.

## 8. Frontend
- **New SWR hook** `frontend/lib/hooks/use-report-sources.ts` (or `lib/reports/`) fetching `/api/v1/reports/sources`. First consumer of the catalog.
- **Source picker** in `components/reports/config/DataTab.tsx`: the `Data source` `<select>` is currently a `disabled` placeholder hardcoded to `transactions` (`DataTab.tsx:44-53`) — make it live, options from the catalog.
- **Data-driven dims/measures:** today dimensions come from the hardcoded `DIMENSION_OPTIONS` in `controlConstants.ts` and measures from `SingleMeasureEditor`/`MeasuresEditor`. Drive the available dimension + measure options from the **selected source's** catalog entry. Switching source **resets** now-invalid dims/measures to the new source's defaults (do not leave a `category` dimension on an `accounts` widget).
- **`resolve.ts`** stamps `dataset` from the widget config and applies the §6 date-omission for date-less sources.
- New/existing widgets default to `dataset: "transactions"`.

## 9. Test coverage (architect S4)
- Per-source `validate()` rejects cross-source measure/dim: `sum(amount)` on `accounts` → 422; `category` dimension on `accounts` → 422; `sum(balance)` on `transactions` → 422.
- Relaxed `NUMERIC_MEASURE_FIELDS` still rejects `sum(id)` / `sum(category_id)` at the Pydantic layer (422).
- Date-tolerance: `date` filter against `accounts` returns rows (dropped), not 422/500. A non-shared inapplicable field (`txn_type` on `accounts`) → 422.
- Enum set-equality test (§1).
- Registry exhaustiveness assertion (extends the existing one): `accounts` is registered, and its catalog keys (dims/measures/filters) are a subset of the closed enums.
- Org-isolation: an account in org A never appears in org B's accounts-source results.
- Boolean-dimension key normalization across SQLite/MySQL → stable `"Active"`/`"Inactive"`.
- `kind` taxonomy test (§5).
- AccountsSource correctness: `sum_balance` grouped by `account_type` / `currency`; `count_accounts`; `avg_balance`; balance `between` filter.
- Frontend: source picker renders from catalog; switching source resets invalid dims/measures; `resolve.ts` omits date for a date-less source; round-trip an accounts widget through save/load.

## Files touched
**Backend:** `schemas/reports_enums.py` (new), `schemas/reports_query.py`, `schemas/report_layout.py`, `reports/sources/base.py`, `reports/sources/accounts.py` (new), `reports/sources/__init__.py` (register), `routers/reports.py` (`_run_source_query` validate+422, `/sources` filters serialization, `SourceFilterOut`).
**Frontend:** `lib/hooks/use-report-sources.ts` (new), `components/reports/config/DataTab.tsx`, `components/reports/config/controlConstants.ts`, `components/reports/config/SingleMeasureEditor.tsx` / `MeasuresEditor.tsx` (source-aware options), `lib/reports/resolve.ts`, `lib/reports/types.ts`.
**No migration.**

## Build / review process
Subagent-driven execution with a review gate per task (per [[feedback_subagent_driven]] / [[feedback_subagent_execution_guardrails]]). Backend tests in an **isolated compose project** (`-p team-<name>`), never the default `pfv` stack. Frontend verification = `eslint . --quiet` + `tsc --noEmit` + full `vitest run` (all three; eslint is a CI gate). Fleet/self-review before merge, matching the rest of the Reports v3 wave.
