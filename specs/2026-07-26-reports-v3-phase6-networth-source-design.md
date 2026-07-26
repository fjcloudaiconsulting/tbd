---
name: Reports v3 Phase 6 — NetWorthSource
description: The last Reports v3 source. A net-worth-OVER-TIME series reconstructed from opening_balance + signed settled deltas, cumulative running total per currency, cash-basis. Snapshot net worth is already served by the Accounts source; this source exists for the time-series.
type: project
status: design — APPROVED-WITH-CHANGES (both sign-offs folded); ready to build
---

# Reports v3 Phase 6 — NetWorthSource (`networth`)

**Roadmap:** Group A (last remaining report source). **Effort:** M–L. **Method:** architect-gated (2 design reviews + 2 spec sign-offs, all folded → TDD build → code review → PR). Autonomy grant 2026-07-26: operator is the merge gate, notified on green CI. Siblings: `specs/2026-06-13-reports-v3-phase5-accounts-source-design.md` + its plan.

## 1. The insight that shapes the whole source

`accounts.balance` **already stores liabilities negative** (`models/account.py`; `BalancesByTypeTile.tsx:20-22`). So **net worth = Σ (signed balance) across accounts, per currency** — assets − liabilities nets automatically from the stored sign; no classification needed for the core figure.

A *point-in-time* net worth is therefore **already expressible** via the Accounts source (`sum(balance)`, no dim). **NetWorthSource exists for the OVER-TIME series** — reconstructing what Σ balance *was* at the end of each past period.

## 2. Decisions (settled by the 2 design architects + 2 sign-offs; operator delegated)

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D-C | Point-in-time vs time-series | **TIME-SERIES** (cumulative running total over a period axis) | Point-in-time is already served by the Accounts source. |
| D-A | Asset/liability classification | **None in V1** (Σ signed balance nets correctly). Split = fast-follow via **slug map** (`liability = {credit_card, loan}`), NOT a migration. Custom/null-slug types = **asset at face-value sign** (never excluded). | No `is_liability` column exists; not justified for one optional breakdown. Mirrors `BalancesByTypeTile`/`LiabilityCards`. |
| D-B | Multi-currency | **Always per-currency; never a cross-currency sum.** `currency` = dimension + filter. No currency dim requested: single-currency org → one series; multi-currency org → per-currency series + a `meta.warning`. | Absolute house rule (no FX). |
| D-M | Measure identity/label | **New `MeasureField.NET_WORTH` (field = `net_worth`)**, measure key `net_worth`, label "Net worth". | Sign-off B: the FE measure picker/axis/tooltip/CSV label by **field** via `MEASURE_FIELD_LABELS`, NOT by measure key — reusing `balance` would render "Balance" on a net-worth chart. A distinct field gives the correct label. Costs 1 backend enum + 1 FE label edit beyond the minimum. |
| E-1 | Inactive/closed accounts | **Include all** (balance is real wealth; matches Accounts source). No `account_active` filter in V1. | Product may add an exclude toggle later. |
| E-2 | `date` filter semantics | **A display WINDOW, not a row filter** (§4). Published ops **`(between, gte, lte)`** (mirror `transactions.py`). | The shared canvas date bar windows the series. `gte` is user-reachable (start-only custom range) → must be published or it 422s. |
| E-3 | Sparse vs filled periods | **Sparse V1**: one point per period with a delta, carrying the running total. | Simplest + correct; filled spine = fast-follow. |
| Naming | dataset key | `"networth"`, `Dataset.NETWORTH`, source label "Net worth". | Single-token, matches `accounts`/`transactions`. |

**Operator-level (flagged, non-blocking, no code impact):** the operator's own **ING Checking/Savings have `opening_balance=0`** with the real start baked into `balance` + no backing transaction (memory `reference_account_balance_opening_invariant`; confirmed by sign-off A: `opening_balance_date` has `server_default=current_date`, so pre-column accounts were backfilled to the migration date, not their true open). NetWorthSource is correct, but those accounts' history will render understated / with a phantom jump until a one-time operator-authorized `opening_balance` repair. Note in the PR; the repair is a separate operator action.

## 3. Reconstruction formula + query shape

For account currency `c` and period `P`:

```
NetWorth(c, P) = Σ_{a: a.currency=c, period(opening_balance_date(a)) ≤ P}  opening_balance(a)
              + Σ_{tx: settled, balance-contributing, tx.account.currency=c, eff_period(tx) ≤ P}  signed(tx)
signed(tx) = +amount if INCOME, −amount if EXPENSE
eff_period(tx) = bucket(coalesce(settled_date, date))     # cash-basis
```

Reconciles exactly to the live cache at `P = today` (invariant `balance == opening_balance + Σ settled(income−expense)` — the exact `balance_contribution_filter` docstring guarantee). `opening_balance` = a **dated opening event** in the period of `opening_balance_date` (`account.py:133`, exists, `nullable=False`), NOT a t=0 baseline — correctly handles accounts opened mid-history.

**Query shape** (bespoke `build_rows(db, org_id, query)` returning `(rows, meta)` — confirmed the source has full control, `accounts.py` is the precedent; does NOT call the generic `execute_query`):
- **Stream 1 — opening events** over `accounts` (org-scoped): `bucket(opening_balance_date) → period, currency, SUM(opening_balance)`.
- **Stream 2 — tx deltas** over `transactions JOIN accounts` (currency is on `accounts`; `Transaction` has no currency column): `bucket(eff_date) → period, accounts.currency, SUM(CASE type WHEN income THEN amount ELSE -amount END)`, gated `status == SETTLED` **AND** `balance_contribution_filter()`.
- **Combine + cumulate in Python:** merge streams by `(period, currency)`, sort, per-currency running total. (Not `SUM() OVER` — avoids MySQL/SQLite window-fn divergence; O(periods), bounded by `MAX_LIMIT=500`.)

**Bucket helper (IMP-2 from sign-off A):** `reports_query_service._dimension_expr` is hard-wired to the tx effective date, so it covers Stream 2 ONLY. Stream 1 buckets `Account.opening_balance_date` → write a small **dialect-aware helper parameterized by the date column** (`strftime` on SQLite / `date_format` on MySQL, mirroring `_dimension_expr`), and have `build_rows` **self-detect dialect** via `db.get_bind().dialect.name` (mirror `execute_query`). Apply the `MAX_EXECUTION_TIME(5000)` MySQL hint (`_apply_query_timeout`, no-op on SQLite) to both streams.

**Reconstruct with `balance_contribution_filter()`, NOT `reportable_transaction_filter()`** (`services/transaction_filters.py`, confirmed no-arg, composable, does not gate status, keeps transfer legs + manual adjustments, drops reconcile dupes/skipped/rejected). Status gated separately (`WHERE status = SETTLED`). This intentionally **counts transfers-as-reconstruction and manual adjustments** (both moved the real balance) — differs from a normal tx report; document loudly.

## 4. The `date` windowing semantics (critical build detail)

Net worth at the start of the window must include **all prior history**. `build_rows` interprets `date` (it reads the raw AST; nothing pre-applies date as a WHERE row-filter for a bespoke source):
- **Upper bound** (`lte` / `between.hi`) → reconstruction cutoff (`eff_period ≤ hi`, opening events too).
- **Lower bound** (`between.lo`) → **output slice only**: accumulate from account inception, drop output periods before `lo`; the total entering the first visible period carries full pre-window history.
- **`gte` (start-only)** → lower bound = output-slice; cutoff = today (no upper bound). Must be handled (it's reachable).
- **`lte` (end-only)** → cutoff = the date; no lower slice.

Frontend needs no special handling: the canvas stamps a normal `date` filter; `build_rows` owns interpretation. This is the single most-likely-wrong line → its own tests (§9).

## 5. Source contract

Implements `ReportSource` Protocol (`reports/sources/base.py`); self-registers like `accounts.py`. `key = "networth"`, `label = "Net worth"`.

**Dimensions** (all reuse EXISTING `Dimension` enum values):
| key | kind | | key | kind |
|---|---|---|---|---|
| `month` | time | | `day` | time |
| `week` | time | | `currency` | currency |

**Measures** — exactly one, using the NEW field:
| key | label | agg | field | format |
|---|---|---|---|---|
| `net_worth` | Net worth | sum | **`net_worth`** | currency |

`agg`/`field` are **nominal** — `build_rows` ignores the generic sum machinery and always computes the reconstruction. `net_worth` must be added to `MeasureField` + `NUMERIC_MEASURE_FIELDS` (so Pydantic's `sum(net_worth)` validates) + the FE `MeasureField` union + `MEASURE_FIELD_LABELS` ("Net worth").

**Filters:**
| field | ops | kind | semantics |
|---|---|---|---|
| `currency` | eq, in | currency | true data filter (API/validate-only — no `FilterEditor` control today, same as accounts) |
| `account_id` | in | account | optional subset |
| `date` | **between, gte, lte** | time | windowing (§4) |

**Rows:** `[{"<time-key>": "2026-06", "currency": "EUR", "value": 15234.50}, ...]`. **KPI / no-dimension contract:** when `query.dimensions == []` (`limit:1`), return the **latest cumulative total per currency as-of the window upper bound** (build_rows must special-case empty dims, else the KPI shows nothing). Multi-currency + `limit:1` truncates to one currency (same wart as the accounts KPI) — emit the `meta.warning`. `compare_prior_period` KPIs compare **cumulative as-of endpoints** (net worth as-of prior hi vs current hi) — a correct period-over-period delta; don't "fix" it into in-window deltas.

**Meta:** `{row_count, truncated, query_ms, warning?}`. `warning` is the multi-currency notice. **`truncated` is measured on the POST-cumulation period-row count vs `MAX_LIMIT=500`**, not the raw stream sizes. Decimal→float coercion as in `accounts.py`.

## 6. Registration checklist (folded with sign-off findings)

Backend:
| # | File · symbol | Change | Guard |
|---|---|---|---|
| B1 | `schemas/reports_enums.py` · `Dataset` | `+ NETWORTH = "networth"` | **[TEST]** registry-exhaustiveness |
| B1b | `schemas/reports_enums.py` · `MeasureField` | `+ NET_WORTH = "net_worth"` | catalog subset test |
| B1c | `schemas/reports_query.py` · `NUMERIC_MEASURE_FIELDS` | `+ MeasureField.NET_WORTH` (so `sum(net_worth)` passes) | Pydantic `Measure` validator |
| B1d | `schemas/reports_query.py` · `QueryMeta` | **add `warning: Optional[str] = None`** (extra is ignored, so the dict key is dropped without this — B-1) | new meta test |
| B2 | `reports/sources/networth.py` (NEW) | `NetWorthSource` (2-stream bespoke `build_rows` + dialect detect + own opening-date bucket helper + KPI no-dim case + windowing) + bottom `register(NetWorthSource())`; `validate` → `validate_against_catalog` | **[TEST]** new source tests |
| B3 | `reports/sources/__init__.py` | `+ from app.reports.sources import networth as _networth  # noqa` | **[TEST]** registry-drift |
| B4 | `tests/.../test_reports_enums_consistency.py` | **edit the exact-set assertion** to include `"networth"` (IMP-3) — a REQUIRED edit, not a passive guard | this test |
| B5 | `routers/reports.py`, `schemas/report_sources.py`, `report_layout.py` | **NO CHANGE** (generic; enum flows through) | consistency test |

No Alembic migration (dataset/field values live in `layout_json`/`canvas_filters_json` JSON).

Frontend (tsc-chained; ~4 small edits):
| # | File · symbol | Change |
|---|---|---|
| F1 | `lib/reports/types.ts` · `Dataset` union | `+ "networth"` |
| F2 | `components/reports/config/DataTab.tsx` · `DATASET_FALLBACK_LABELS` | `+ networth: "Net worth"` |
| F3 | `lib/reports/types.ts` · `MeasureField` union | `+ "net_worth"` |
| F4 | `lib/reports/series.ts` · `MEASURE_FIELD_LABELS` | `+ net_worth: "Net worth"` (axis/tooltip/CSV/measure-picker label) |
| F5 | `lib/reports/types.ts` · `QueryMeta` | add `warning?: string` (mirror B1d) |

Everything else catalog-driven (source picker, dim/measure pickers, CSV, the `date` filter/chip/editor — `date` reuses existing plumbing; **published ops now include `gte`** so no 422). Optional default: seed new networth widgets with `title: "Net worth"`.

**Feature gating:** inherited automatically (`require_feature(Feature.REPORTS)` router dep + FE `features.reports`). No new flag. CSV client-side; no apex `/api/v1` purity concern. Saved `layout_json` with a `networth` widget round-trips with no `report_layout.py` change.

## 7. Edge cases

Account opened mid-history → opening enters at its period. Closed/inactive → included (balance is real; most are 0). Transfers → net to zero within a currency (paired legs cancel; `balance_contribution_filter` keeps both). Manual adjustments → counted. Pending → excluded (settled only). Reconcile skipped/rejected + one-way matched → excluded by the filter. Custom/null-slug type → included at face-value sign. Zero accounts → `[]`, `row_count=0`. Multi-currency, no currency dim → per-currency rows + `meta.warning`, never a summed number. ING `opening_balance=0` gap → §2 operator flag.

## 8. Performance

Group M "200-tx cap" does NOT apply (that's a client-side dashboard fetch limit; this aggregates server-side in SQL, ≤500 period rows regardless of tx volume). Two grouped aggregations (tiny `accounts` scan + a `transactions` scan filtered by org+status+`balance_contribution_filter`, grouped by effective-period+currency). Apply `MAX_EXECUTION_TIME(5000)`. `balance_contribution_filter` correlated `EXISTS` adds a self-join cost, acceptable at personal-finance scale. Index note (non-blocking): benefits from `transactions(org_id, settled_date)`/`(org_id, date)` — verify existing coverage.

## 9. Test plan

Backend in an **isolated compose project** `-p team-<name>` (never `pfv`). Mirror `tests/services/test_accounts_source.py`.

**Reconstruction:** single account opening 1000@2026-01, +500 income settled 2026-02, −200 expense settled 2026-03 → `{2026-01:1000, 2026-02:1500, 2026-03:1300}`. **Reconciles:** `NetWorth(latest) == Σ Account.balance` per currency. **Cash-basis:** 2026-01-dated / 2026-02-settled buckets into 2026-02. **Mid-history opening:** account B opened 2026-03 (opening 5000) → steps up only from 2026-03. **Cumulative across a gap:** assert the documented sparse emission.

**Exclusions:** transfer pair (same currency) → zero net; manual adjustment → counted; pending → excluded; reconcile skipped/rejected + one-way matched → excluded.

**Multi-currency:** EUR+USD, no currency dim → two series + `meta.warning`, no cross-currency sum. `currency eq EUR` → EUR only.

**Windowing (highest-value):** `date between [2026-02, 2026-03]` → first visible period 2026-02 shows the **full** net worth (carries 2026-01 opening). `date lte 2026-02` → ends at 2026-02, correct as-of. **`date gte 2026-02`** (start-only) → no 422, output sliced from 2026-02 to today carrying prior history.

**KPI:** `dimensions:[] limit:1` → latest cumulative total per currency (not empty). `compare_prior_period` → as-of-endpoint delta.

**Contract/registry:** `get_source("networth")` resolves; catalog keys ⊆ closed enums; kinds ∈ known set; `validate()` rejects `sum(amount)` / a `category` dim / a `txn_type` filter (422); org isolation; `GET /reports/sources` includes `networth`; a `networth` `layout_json` round-trips; **`test_reports_enums_consistency` updated + green**; `QueryMeta.warning` round-trips.

**Frontend:** source picker shows/selects "Net worth"; switching a widget → `networth` resets measure to `net_worth` + drops incompatible dims/filters; measure axis/tooltip/CSV read "Net worth" (not "Balance"); canvas date range windows the widget; `DATASET_FALLBACK_LABELS.networth` + `MEASURE_FIELD_LABELS.net_worth` present. Full `eslint . --quiet` + `tsc --noEmit` + `vitest run`.

## 10. Deferred fast-follows (documented, not V1)

Asset/liability split dimension (slug map; `is_liability` column only if users must mark custom types as liabilities — a product call) · `account_active` exclude filter · filled period-spine · a `currency` `FilterEditor` control (currently API-only, same as accounts) · the ING `opening_balance` data repair (operator-authorized, separate).

## 11. Architect sign-off

- Design A (data model / classification / currency / time-series): folded §1–§4, §7, §8.
- Design B (registration / frontend / semantics): folded §5, §6, §9.
- **Spec sign-off A (backend): APPROVE-WITH-CHANGES** — folded B-1 (`QueryMeta.warning`), IMP-1 (`gte` op + case), IMP-2 (own opening-date bucket helper + dialect detect), IMP-3 (enum-consistency test edit), MIN-2 (`truncated` post-cumulation). Confirmed: bespoke `build_rows` control, `date` windowing feasibility, `balance_contribution_filter`/`effective_period_date_expr`/`opening_balance_date` all exist as assumed.
- **Spec sign-off B (registration/FE): APPROVE-WITH-CHANGES** — folded the `gte` Blocking (dup of IMP-1), the measure-label Important (→ `MeasureField.NET_WORTH`, D-M, F3/F4), the KPI no-dim contract, the currency-filter-no-control + `compare_prior_period` minors. Confirmed: `Dataset.NETWORTH` + `MeasureField.NET_WORTH` are the only enum widenings; feature-gate/CSV/apex/saved-layout all no-change.
