# Reports v3 Phase 5 — AccountsSource Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second `ReportSource` (`accounts`) to the Reports v3 registry so users can build widgets reporting on their accounts snapshot (balance / counts by account, type, currency, active status).

**Architecture:** Widen the closed query-AST enums (consolidated first into one shared module), move per-source validity from Pydantic into a required `ReportSource.validate()` method, publish filterable fields through the `/sources` catalog, and add a small org-scoped `accounts`+`account_types` compiler. Frontend wires the (currently disabled) Data-tab source picker to the catalog and drives dims/measures from it.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic v2 (backend); Next.js/React/TypeScript/SWR/Vitest (frontend). Backend tests run in an **isolated compose project** (`-p team-<name>`), never the default `pfv` stack.

**Reference spec:** `specs/2026-06-13-reports-v3-phase5-accounts-source-design.md`

**Product default (flag at review):** AccountsSource includes **all** accounts regardless of `is_active`, matching existing transactions-report precedent; `account_active` is exposed as a dimension + filter so users can exclude inactive accounts explicitly. Operator may flip the default to active-only in review.

---

## File structure

**Backend**
- `app/schemas/reports_enums.py` — **new.** Shared closed atoms: `Dataset`, `Aggregation`, `MeasureField`, `Dimension`. Imported by both query + layout schemas.
- `app/schemas/reports_query.py` — import shared atoms; widen `FilterField` (AST-only); relax `Measure` validator via `NUMERIC_MEASURE_FIELDS`.
- `app/schemas/report_layout.py` — import shared atoms instead of redefining.
- `app/reports/sources/base.py` — add `SourceFilter` value object; add `filters()` + `validate()` to the `ReportSource` Protocol; define the shared-canvas drop-set.
- `app/reports/sources/transactions.py` — implement `filters()` + `validate()`.
- `app/reports/sources/accounts.py` — **new.** Catalog + org-scoped compiler; self-registers.
- `app/reports/sources/__init__.py` — import `accounts` so it registers.
- `app/schemas/report_sources.py` — add `SourceFilterOut`; add `filters` to `SourceCatalogEntry`.
- `app/routers/reports.py` — `_run_source_query` calls `validate()` and maps `ValueError`→422; `/sources` serializes `filters()`.

**Frontend**
- `lib/reports/use-report-sources.ts` — **new.** SWR hook over `/api/v1/reports/sources`.
- `lib/reports/types.ts` — add `accounts` dataset + new dimension literals; `SourceCatalog*` types.
- `components/reports/config/DataTab.tsx` — live source picker; source-driven dimension options; reset on switch.
- `components/reports/config/controlConstants.ts` — helper to derive dimension/measure options from a catalog entry.
- `lib/reports/resolve.ts` — stamp `dataset`; omit canvas date filter for date-less sources.

**Tests**
- `tests/services/test_report_sources_registry.py` — extend (drift, exhaustiveness, validate).
- `tests/services/test_accounts_source.py` — **new.** Compiler correctness + org isolation + boolean labels.
- `tests/routers/test_report_sources_endpoint.py` — extend (filters in catalog).
- `tests/routers/test_reports_query_validation.py` — **new.** Cross-source 422 + date tolerance.
- `tests/schemas/test_reports_enums_consistency.py` — **new.** Enum drift guard.
- Frontend: `tests/components/reports/data-tab-source-picker.test.tsx`, `tests/lib/reports/resolve-dataset.test.tsx` — **new.**

---

## Task 1: Consolidate shared enums (behavior-preserving)

**Files:**
- Create: `app/schemas/reports_enums.py`
- Modify: `app/schemas/reports_query.py`, `app/schemas/report_layout.py`
- Test: `tests/schemas/test_reports_enums_consistency.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/schemas/test_reports_enums_consistency.py
"""Guard against the two-copy enum drift the shared module exists to kill."""
from app.schemas import reports_query, report_layout


def test_shared_enum_atoms_are_the_same_object():
    # After consolidation both modules re-export the SAME enum class.
    assert reports_query.Dataset is report_layout.Dataset
    assert reports_query.Dimension is report_layout.Dimension
    assert reports_query.MeasureField is report_layout.MeasureField
    assert reports_query.Aggregation is report_layout.Aggregation


def test_dataset_values_unchanged_for_now():
    assert {d.value for d in reports_query.Dataset} == {"transactions"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-accsrc exec backend pytest tests/schemas/test_reports_enums_consistency.py -v`
Expected: FAIL — `reports_query.Dataset is report_layout.Dataset` is False (two distinct classes today).

- [ ] **Step 3: Create the shared module**

```python
# app/schemas/reports_enums.py
"""Shared closed enum atoms for the reports query AST and the saved-layout
JSON validator. Both surfaces draw dataset / dimension / measure-field /
aggregation from these closed enums so a value cannot drift between "what a
saved widget references" and "what the live compiler accepts"."""
from __future__ import annotations

import enum


class Dataset(str, enum.Enum):
    TRANSACTIONS = "transactions"


class Aggregation(str, enum.Enum):
    SUM = "sum"
    COUNT = "count"
    AVG = "avg"
    DISTINCT = "distinct"


class MeasureField(str, enum.Enum):
    AMOUNT = "amount"
    ID = "id"
    CATEGORY_ID = "category_id"
    ACCOUNT_ID = "account_id"


class Dimension(str, enum.Enum):
    CATEGORY = "category"
    CATEGORY_MASTER = "category_master"
    ACCOUNT = "account"
    TAG = "tag"
    TXN_TYPE = "txn_type"
    STATUS = "status"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
```

- [ ] **Step 4: Re-export from `reports_query.py`**

In `app/schemas/reports_query.py`, DELETE the local `class Dataset`, `class Aggregation`, `class MeasureField`, `class Dimension` definitions (keep their docstrings as a comment if useful) and replace with an import near the top (after the existing imports):

```python
from app.schemas.reports_enums import Aggregation, Dataset, Dimension, MeasureField
```

Leave `FilterField`, `FilterOp`, `Measure`, etc. in place. Everything referencing these names continues to work.

- [ ] **Step 5: Re-export from `report_layout.py`**

In `app/schemas/report_layout.py`, DELETE the local `class Dataset`, `class Aggregation`, `class MeasureField`, `class Dimension` (lines ~62-89) and add near the top imports:

```python
from app.schemas.reports_enums import Aggregation, Dataset, Dimension, MeasureField
```

Keep `WidgetType`, `WidgetFormat`, `SortBy`, `SortDir` local (layout-only).

- [ ] **Step 6: Run the new test + the existing reports suites**

Run: `docker compose -p team-accsrc exec backend pytest tests/schemas/test_reports_enums_consistency.py tests/services/test_report_sources_registry.py tests/routers/test_report_sources_endpoint.py -v`
Expected: PASS (consolidation is behavior-preserving; the registry tests that import `from app.schemas.reports_query import Dimension` still resolve).

- [ ] **Step 7: Commit**

```bash
git add app/schemas/reports_enums.py app/schemas/reports_query.py app/schemas/report_layout.py tests/schemas/test_reports_enums_consistency.py
git commit -m "refactor(reports): extract shared query-AST enums into reports_enums"
```

---

## Task 2: Widen enums for accounts + relax the Measure validator

**Files:**
- Modify: `app/schemas/reports_enums.py`, `app/schemas/reports_query.py`
- Test: `tests/schemas/test_reports_enums_consistency.py` (extend), `tests/schemas/test_measure_numeric_validation.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
# tests/schemas/test_measure_numeric_validation.py
import pytest
from pydantic import ValidationError

from app.schemas.reports_query import (
    Aggregation, Measure, MeasureField, NUMERIC_MEASURE_FIELDS,
)


def test_sum_balance_is_numerically_sane():
    # balance is numeric → Pydantic accepts it (per-source layer gates source validity).
    m = Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE)
    assert m.field is MeasureField.BALANCE


def test_sum_id_still_rejected_at_pydantic():
    with pytest.raises(ValidationError):
        Measure(agg=Aggregation.SUM, field=MeasureField.ID)


def test_numeric_set_is_amount_and_balance():
    assert NUMERIC_MEASURE_FIELDS == {MeasureField.AMOUNT, MeasureField.BALANCE}
```

Append to `tests/schemas/test_reports_enums_consistency.py`:

```python
def test_accounts_dataset_and_new_dimensions_present():
    from app.schemas.reports_query import Dataset, Dimension, MeasureField
    assert "accounts" in {d.value for d in Dataset}
    assert {"account_type", "currency", "account_active"}.issubset({d.value for d in Dimension})
    assert "balance" in {f.value for f in MeasureField}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker compose -p team-accsrc exec backend pytest tests/schemas/test_measure_numeric_validation.py tests/schemas/test_reports_enums_consistency.py -v`
Expected: FAIL — `NUMERIC_MEASURE_FIELDS` undefined; `accounts`/`balance` not in enums.

- [ ] **Step 3: Widen the shared enums**

In `app/schemas/reports_enums.py`:

```python
class Dataset(str, enum.Enum):
    TRANSACTIONS = "transactions"
    ACCOUNTS = "accounts"
```

```python
class MeasureField(str, enum.Enum):
    AMOUNT = "amount"
    ID = "id"
    CATEGORY_ID = "category_id"
    ACCOUNT_ID = "account_id"
    BALANCE = "balance"
```

```python
class Dimension(str, enum.Enum):
    CATEGORY = "category"
    CATEGORY_MASTER = "category_master"
    ACCOUNT = "account"
    TAG = "tag"
    TXN_TYPE = "txn_type"
    STATUS = "status"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
    ACCOUNT_TYPE = "account_type"
    CURRENCY = "currency"
    ACCOUNT_ACTIVE = "account_active"
```

- [ ] **Step 4: Widen `FilterField` + relax the Measure validator in `reports_query.py`**

Add to `FilterField` (the AST-only enum in `reports_query.py`):

```python
    ACCOUNT_TYPE = "account_type"
    CURRENCY = "currency"
    ACCOUNT_ACTIVE = "account_active"
    BALANCE = "balance"
```

Add the module-level numeric set near the other caps (after `MAX_DATE_WINDOW_DAYS`):

```python
# Fields a SUM / AVG may target. Source-agnostic numeric sanity gate — the
# per-source validate() still rejects a field the source does not publish.
NUMERIC_MEASURE_FIELDS = {MeasureField.AMOUNT, MeasureField.BALANCE}
```

Replace `Measure._validate_agg_field` body:

```python
    @model_validator(mode="after")
    def _validate_agg_field(self):
        if self.agg in (Aggregation.SUM, Aggregation.AVG):
            if self.field not in NUMERIC_MEASURE_FIELDS:
                raise ValueError(
                    f"agg={self.agg.value} requires a numeric field "
                    f"{sorted(f.value for f in NUMERIC_MEASURE_FIELDS)}; "
                    f"got field={self.field.value!r}"
                )
        return self
```

- [ ] **Step 5: Extend `FilterField` scalar coercion**

In `_coerce_filter_scalar` (`reports_query.py`), add handling so the new filter fields coerce safely (place before the final unreachable raise):

```python
    if field is FilterField.ACCOUNT_TYPE:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("account_type must be an integer id") from exc
    if field is FilterField.CURRENCY:
        v = str(value).strip().upper()
        if not (len(v) == 3 and v.isalpha()):
            raise ValueError("currency must be a 3-letter code")
        return v
    if field is FilterField.ACCOUNT_ACTIVE:
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        if v in ("true", "1", "active"):
            return True
        if v in ("false", "0", "inactive"):
            return False
        raise ValueError("account_active must be a boolean")
    if field is FilterField.BALANCE:
        return _coerce_decimal(value)
```

(`account` filtering reuses the existing `ACCOUNT_ID` field/coercion — `account_type` here is the type id.)

- [ ] **Step 6: Run the tests**

Run: `docker compose -p team-accsrc exec backend pytest tests/schemas/ -v`
Expected: PASS.

Note: `test_every_dataset_enum_value_has_a_registered_source` in `tests/services/test_report_sources_registry.py` will now FAIL (accounts dataset has no source yet) — that is expected and fixed in Task 4. Do not run that file green here.

- [ ] **Step 7: Commit**

```bash
git add app/schemas/reports_enums.py app/schemas/reports_query.py tests/schemas/
git commit -m "feat(reports): widen query-AST enums for the accounts source"
```

---

## Task 3: Source catalog filters + Protocol `validate()`/`filters()` + router 422 wiring

**Files:**
- Modify: `app/reports/sources/base.py`, `app/reports/sources/transactions.py`, `app/schemas/report_sources.py`, `app/routers/reports.py`
- Test: `tests/routers/test_reports_query_validation.py` (new), `tests/routers/test_report_sources_endpoint.py` (extend)

- [ ] **Step 1: Write the failing validation test**

```python
# tests/routers/test_reports_query_validation.py
"""Per-source validity is enforced after Pydantic parse and surfaces as 422."""
import pytest

from app.reports import sources as registry
from app.schemas.reports_query import (
    Aggregation, Dataset, Dimension, Filter, FilterField, FilterOp,
    Measure, MeasureField, ReportsQuery,
)


def _q(dataset, measure, dims=None, filters=None):
    return ReportsQuery(
        dataset=dataset, measure=measure,
        dimensions=dims or [], filters=filters or [],
    )


def test_transactions_rejects_balance_measure():
    src = registry.get_source("transactions")
    q = _q(Dataset.TRANSACTIONS, Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE))
    with pytest.raises(ValueError):
        src.validate(q)


def test_transactions_rejects_currency_dimension():
    src = registry.get_source("transactions")
    q = _q(Dataset.TRANSACTIONS,
           Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
           dims=[Dimension.CURRENCY])
    with pytest.raises(ValueError):
        src.validate(q)


def test_transactions_accepts_its_own_surface():
    src = registry.get_source("transactions")
    q = _q(Dataset.TRANSACTIONS,
           Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
           dims=[Dimension.CATEGORY])
    src.validate(q)  # no raise
```

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose -p team-accsrc exec backend pytest tests/routers/test_reports_query_validation.py -v`
Expected: FAIL — `ReportSource` has no `validate` attribute.

- [ ] **Step 3: Add `SourceFilter`, Protocol methods, and the shared-canvas drop-set in `base.py`**

```python
# add to app/reports/sources/base.py

# Canvas-level shared filter fields (the #448 date bar + any canvas
# account/category scoping). A source that does not publish one of these
# SILENTLY DROPS it (it is a shared-bar artifact, not a malformed query);
# any OTHER unpublished field is rejected. See spec §6.
SHARED_CANVAS_FILTER_FIELDS = frozenset({"date", "account_id", "category_id"})


@dataclass(frozen=True)
class SourceFilter:
    field: str          # AST FilterField value, e.g. "currency"
    label: str          # human label for the editor
    ops: tuple[str, ...]  # allowed FilterOp values, e.g. ("eq", "in")
    kind: str           # control hint: account|account_type|currency|boolean|...
```

Add to the `ReportSource` Protocol (all required):

```python
    def filters(self) -> list[SourceFilter]: ...

    def validate(self, query: ReportsQuery) -> None:
        """Raise ValueError if the AST references a dimension / measure
        field / filter field this source does not publish. Shared-canvas
        fields that don't apply are dropped, not rejected (spec §6)."""
        ...
```

Add a reusable default validator helper in `base.py` (sources call it):

```python
def validate_against_catalog(source: "ReportSource", query: ReportsQuery) -> None:
    dim_keys = {d.key for d in source.dimensions()}
    for dim in query.dimensions:
        if dim.value not in dim_keys:
            raise ValueError(
                f"source {source.key!r} does not support dimension {dim.value!r}"
            )
    measure_fields = {m.field for m in source.measures()}
    if query.measure.field.value not in measure_fields:
        raise ValueError(
            f"source {source.key!r} does not support measure field "
            f"{query.measure.field.value!r}"
        )
    filter_fields = {f.field for f in source.filters()}
    for f in query.filters:
        if f.field.value in filter_fields:
            continue
        if f.field.value in SHARED_CANVAS_FILTER_FIELDS:
            continue  # shared-bar artifact → dropped at build time
        raise ValueError(
            f"source {source.key!r} does not support filter field {f.field.value!r}"
        )
```

- [ ] **Step 4: Implement `filters()` + `validate()` on `TransactionsSource`**

```python
# app/reports/sources/transactions.py
from app.reports.sources.base import (
    ReportSource, SourceDimension, SourceFilter, SourceMeasure,
    validate_against_catalog,
)

_FILTERS = [
    SourceFilter("date", "Date", ("between", "gte", "lte"), "time"),
    SourceFilter("amount", "Amount", ("between", "gte", "lte", "eq"), "amount"),
    SourceFilter("category_id", "Category", ("eq", "in"), "category"),
    SourceFilter("account_id", "Account", ("eq", "in"), "account"),
    SourceFilter("txn_type", "Type", ("eq", "in"), "type"),
    SourceFilter("status", "Status", ("eq",), "status"),
    SourceFilter("tag_name", "Tag", ("eq", "in"), "tag"),
]

# inside class TransactionsSource:
    def filters(self) -> list[SourceFilter]:
        return list(_FILTERS)

    def validate(self, query: ReportsQuery) -> None:
        validate_against_catalog(self, query)
```

- [ ] **Step 5: Run the validation test**

Run: `docker compose -p team-accsrc exec backend pytest tests/routers/test_reports_query_validation.py -v`
Expected: PASS.

- [ ] **Step 6: Surface filters through `/sources` + wire router 422**

In `app/schemas/report_sources.py`:

```python
class SourceFilterOut(BaseModel):
    field: str
    label: str
    ops: list[str]
    kind: str


class SourceCatalogEntry(BaseModel):
    key: str
    label: str
    dimensions: list[SourceDimensionOut]
    measures: list[SourceMeasureOut]
    filters: list[SourceFilterOut]
```

In `app/routers/reports.py` `list_sources`, add the filters serialization:

```python
            filters=[SourceFilterOut(**dataclasses.asdict(f)) for f in s.filters()],
```

(Import `SourceFilterOut` alongside the existing `SourceDimensionOut` import.)

In `_run_source_query`, call validate before build and map ValueError → 422:

```python
async def _run_source_query(db: AsyncSession, ast: ReportsQuery, *, org_id: int):
    source = get_source(ast.dataset.value)
    try:
        source.validate(ast)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await source.build_rows(db, org_id, ast)
```

- [ ] **Step 7: Extend the catalog-endpoint test**

In `tests/routers/test_report_sources_endpoint.py`, add an assertion that each entry has a non-empty `filters` list with `field`/`label`/`ops`/`kind` keys, and that the transactions entry includes a `date` filter.

- [ ] **Step 8: Run the router tests**

Run: `docker compose -p team-accsrc exec backend pytest tests/routers/test_report_sources_endpoint.py tests/routers/test_reports_query_validation.py -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/reports/sources/base.py app/reports/sources/transactions.py app/schemas/report_sources.py app/routers/reports.py tests/routers/
git commit -m "feat(reports): publish source filters + per-source validate() with 422 wiring"
```

---

## Task 4: AccountsSource (catalog + org-scoped compiler)

**Files:**
- Create: `app/reports/sources/accounts.py`
- Modify: `app/reports/sources/__init__.py`
- Test: `tests/services/test_accounts_source.py` (new), `tests/services/test_report_sources_registry.py` (extend)

- [ ] **Step 1: Write the failing compiler test**

```python
# tests/services/test_accounts_source.py
import pytest

from app.reports import sources as registry
from app.schemas.reports_query import (
    Aggregation, Dataset, Dimension, Filter, FilterField, FilterOp,
    Measure, MeasureField, ReportsQuery,
)


def _q(measure, dims=None, filters=None):
    return ReportsQuery(dataset=Dataset.ACCOUNTS, measure=measure,
                        dimensions=dims or [], filters=filters or [])


@pytest.mark.asyncio
async def test_sum_balance_by_account_type(db_session, accounts_fixture):
    """accounts_fixture seeds org with: Checking(EUR,500,active),
    Savings(EUR,1500,active), OldCard(EUR,-200,inactive) under types
    'Bank'(Checking,Savings) and 'Card'(OldCard)."""
    src = registry.get_source("accounts")
    q = _q(Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE),
           dims=[Dimension.ACCOUNT_TYPE])
    rows, meta = await src.build_rows(db_session, accounts_fixture.org_id, q)
    by_type = {r["account_type"]: r["value"] for r in rows}
    assert by_type["Bank"] == 2000.0   # 500 + 1500
    assert by_type["Card"] == -200.0
    assert meta["row_count"] == 2


@pytest.mark.asyncio
async def test_account_active_dimension_labels(db_session, accounts_fixture):
    src = registry.get_source("accounts")
    q = _q(Measure(agg=Aggregation.COUNT, field=MeasureField.ID),
           dims=[Dimension.ACCOUNT_ACTIVE])
    rows, _ = await src.build_rows(db_session, accounts_fixture.org_id, q)
    by_status = {r["account_active"]: r["value"] for r in rows}
    assert by_status == {"Active": 2, "Inactive": 1}


@pytest.mark.asyncio
async def test_org_isolation(db_session, accounts_fixture, other_org_account):
    src = registry.get_source("accounts")
    q = _q(Measure(agg=Aggregation.COUNT, field=MeasureField.ID))
    rows, _ = await src.build_rows(db_session, accounts_fixture.org_id, q)
    # other_org_account belongs to a different org and must not be counted.
    assert rows[0]["value"] == 3


@pytest.mark.asyncio
async def test_date_filter_is_dropped_not_errored(db_session, accounts_fixture):
    src = registry.get_source("accounts")
    q = _q(Measure(agg=Aggregation.COUNT, field=MeasureField.ID),
           filters=[Filter(field=FilterField.DATE, op=FilterOp.BETWEEN,
                           value=["2020-01-01", "2020-12-31"])])
    src.validate(q)  # tolerated (shared-canvas field)
    rows, _ = await src.build_rows(db_session, accounts_fixture.org_id, q)
    assert rows[0]["value"] == 3  # date predicate ignored


@pytest.mark.asyncio
async def test_currency_filter_and_dimension(db_session, accounts_fixture):
    src = registry.get_source("accounts")
    q = _q(Measure(agg=Aggregation.SUM, field=MeasureField.BALANCE),
           dims=[Dimension.CURRENCY],
           filters=[Filter(field=FilterField.CURRENCY, op=FilterOp.EQ, value="EUR")])
    rows, _ = await src.build_rows(db_session, accounts_fixture.org_id, q)
    assert {r["currency"] for r in rows} == {"EUR"}


def test_accounts_rejects_transactions_field():
    src = registry.get_source("accounts")
    q = _q(Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT))
    with pytest.raises(ValueError):
        src.validate(q)
```

> **Fixtures:** add `accounts_fixture` and `other_org_account` to `tests/conftest.py` (or the nearest reports conftest), mirroring existing account-seeding fixtures. `accounts_fixture` returns an object exposing `.org_id` and seeds the three accounts + two `account_types` described above. `other_org_account` seeds one account under a second org. Check `tests/conftest.py` for the existing org/account factory pattern and reuse it; do NOT hand-roll session/engine setup.

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose -p team-accsrc exec backend pytest tests/services/test_accounts_source.py -v`
Expected: FAIL — `get_source("accounts")` raises KeyError (not registered).

- [ ] **Step 3: Implement `accounts.py`**

```python
# app/reports/sources/accounts.py
"""Accounts source — a snapshot over accounts + account_types.

One row per account; no time dimension. balance / opening_balance are
stored columns, so this is a single join with no transaction reconstruction
(NetWorth/Phase 6 is the reconstruction case). Every query is scoped to
accounts.org_id; account_types is joined on the FK only.
"""
from __future__ import annotations

import time

from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountType
from app.reports.sources import register
from app.reports.sources.base import (
    ReportSource, SourceDimension, SourceFilter, SourceMeasure,
    validate_against_catalog,
)
from app.schemas.reports_query import (
    Aggregation, Dimension, FilterField, FilterOp, MeasureField, ReportsQuery,
)

_DIMENSIONS = [
    SourceDimension("account", "Account", "account"),
    SourceDimension("account_type", "Account type", "account_type"),
    SourceDimension("currency", "Currency", "currency"),
    SourceDimension("account_active", "Status", "boolean"),
]

_MEASURES = [
    SourceMeasure("sum_balance", "Total balance", "sum", "balance", "currency"),
    SourceMeasure("avg_balance", "Average balance", "avg", "balance", "currency"),
    SourceMeasure("count_accounts", "Account count", "count", "id", "number"),
]

_FILTERS = [
    SourceFilter("account_id", "Account", ("in",), "account"),
    SourceFilter("account_type", "Account type", ("eq", "in"), "account_type"),
    SourceFilter("currency", "Currency", ("eq", "in"), "currency"),
    SourceFilter("account_active", "Status", ("eq",), "boolean"),
    SourceFilter("balance", "Balance", ("between", "gte", "lte"), "number"),
]

# Active/inactive rendered as stable string keys in Python (drivers return
# 1/0 from a GROUP BY boolean expression — never rely on a Python bool).
_ACTIVE_LABEL = case((Account.is_active.is_(True), "Active"), else_="Inactive")

_DIM_EXPR = {
    Dimension.ACCOUNT: (Account.name, "account"),
    Dimension.ACCOUNT_TYPE: (AccountType.name, "account_type"),
    Dimension.CURRENCY: (Account.currency, "currency"),
    Dimension.ACCOUNT_ACTIVE: (_ACTIVE_LABEL, "account_active"),
}

_MEASURE_COL = {
    MeasureField.BALANCE: Account.balance,
    MeasureField.ID: Account.id,
}


def _measure_expr(measure):
    col = _MEASURE_COL[measure.field]
    if measure.agg is Aggregation.SUM:
        return func.coalesce(func.sum(col), 0).label("value")
    if measure.agg is Aggregation.AVG:
        return func.coalesce(func.avg(col), 0).label("value")
    if measure.agg is Aggregation.DISTINCT:
        return func.count(distinct(col)).label("value")
    return func.count(col).label("value")  # COUNT


def _apply_filter(stmt, f):
    # date / category_id / unknown shared-canvas fields are silently dropped.
    if f.field is FilterField.ACCOUNT_ID:
        return stmt.where(Account.id.in_(f.value)) if f.op is FilterOp.IN \
            else stmt.where(Account.id == f.value)
    if f.field is FilterField.ACCOUNT_TYPE:
        return stmt.where(Account.account_type_id.in_(f.value)) if f.op is FilterOp.IN \
            else stmt.where(Account.account_type_id == f.value)
    if f.field is FilterField.CURRENCY:
        return stmt.where(Account.currency.in_(f.value)) if f.op is FilterOp.IN \
            else stmt.where(Account.currency == f.value)
    if f.field is FilterField.ACCOUNT_ACTIVE:
        return stmt.where(Account.is_active.is_(bool(f.value)))
    if f.field is FilterField.BALANCE:
        if f.op is FilterOp.BETWEEN:
            return stmt.where(Account.balance.between(f.value[0], f.value[1]))
        if f.op is FilterOp.GTE:
            return stmt.where(Account.balance >= f.value)
        if f.op is FilterOp.LTE:
            return stmt.where(Account.balance <= f.value)
    return stmt  # dropped


class AccountsSource:
    key = "accounts"
    label = "Accounts"

    def dimensions(self): return list(_DIMENSIONS)
    def measures(self): return list(_MEASURES)
    def filters(self): return list(_FILTERS)

    def validate(self, query: ReportsQuery) -> None:
        validate_against_catalog(self, query)

    async def build_rows(self, db: AsyncSession, org_id: int, query: ReportsQuery):
        dim_exprs = [_DIM_EXPR[d] for d in query.dimensions]
        cols = [expr.label(key) for expr, key in dim_exprs]
        stmt = (
            select(*cols, _measure_expr(query.measure))
            .select_from(Account)
            .join(AccountType, AccountType.id == Account.account_type_id)
            .where(Account.org_id == org_id)
        )
        for f in query.filters:
            stmt = _apply_filter(stmt, f)
        if dim_exprs:
            stmt = stmt.group_by(*[expr for expr, _ in dim_exprs])
        stmt = stmt.limit(query.limit)

        started = time.perf_counter()
        result = await db.execute(stmt)
        rows = result.mappings().all()
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        out = []
        for r in rows:
            d = {key: r.get(key) for _, key in dim_exprs}
            val = r.get("value")
            d["value"] = float(val) if hasattr(val, "as_tuple") else val
            out.append(d)
        meta = {"row_count": len(out), "truncated": len(out) >= query.limit,
                "query_ms": elapsed_ms}
        return out, meta


register(AccountsSource())
```

- [ ] **Step 4: Register the module**

In `app/reports/sources/__init__.py`, add next to the transactions import at the bottom:

```python
from app.reports.sources import accounts as _accounts  # noqa: E402,F401
```

- [ ] **Step 5: Run the accounts tests**

Run: `docker compose -p team-accsrc exec backend pytest tests/services/test_accounts_source.py -v`
Expected: PASS.

- [ ] **Step 6: Extend the registry tests + run them green**

In `tests/services/test_report_sources_registry.py`, add:

```python
def test_accounts_source_catalog():
    src = source_registry.get_source("accounts")
    assert src.key == "accounts"
    assert {d.key for d in src.dimensions()} == {
        "account", "account_type", "currency", "account_active"}
    assert {m.key for m in src.measures()} == {
        "sum_balance", "avg_balance", "count_accounts"}
    assert {f.field for f in src.filters()} == {
        "account_id", "account_type", "currency", "account_active", "balance"}


def test_all_catalog_keys_are_known_kinds():
    known = {"category", "account", "status", "type", "tag", "time",
             "account_type", "currency", "boolean", "amount", "number"}
    for s in source_registry.all_sources():
        for d in s.dimensions():
            assert d.kind in known, f"{s.key}:{d.key} bad kind {d.kind}"
        for f in s.filters():
            assert f.kind in known, f"{s.key}:{f.field} bad kind {f.kind}"


def test_every_source_catalog_keys_subset_of_closed_enums():
    from app.schemas.reports_query import Dimension, MeasureField, FilterField
    dims = {d.value for d in Dimension}
    fields = {f.value for f in MeasureField}
    filt = {f.value for f in FilterField}
    for s in source_registry.all_sources():
        assert {d.key for d in s.dimensions()}.issubset(dims)
        assert {m.field for m in s.measures()}.issubset(fields)
        assert {f.field for f in s.filters()}.issubset(filt)
```

Run: `docker compose -p team-accsrc exec backend pytest tests/services/test_report_sources_registry.py -v`
Expected: PASS — including `test_every_dataset_enum_value_has_a_registered_source` (accounts now registered).

- [ ] **Step 7: Commit**

```bash
git add app/reports/sources/accounts.py app/reports/sources/__init__.py tests/services/
git commit -m "feat(reports): add AccountsSource (balance/count over accounts snapshot)"
```

---

## Task 5: Backend suite sweep

**Files:** none new — verification gate.

- [ ] **Step 1: Run the full reports backend surface**

Run: `docker compose -p team-accsrc exec backend pytest tests/schemas/ tests/services/test_report_sources_registry.py tests/services/test_accounts_source.py tests/routers/test_report_sources_endpoint.py tests/routers/test_reports_query_validation.py -v`
Expected: all PASS.

- [ ] **Step 2: Run the existing reports query suite (regression)**

Run: `docker compose -p team-accsrc exec backend pytest tests/ -k "report" -v`
Expected: all PASS — transactions path byte-for-byte unchanged.

- [ ] **Step 3: Commit (only if any fixup was needed)**

```bash
git commit -am "test(reports): backend suite green for accounts source" || true
```

---

## Task 6: Frontend — source picker driven by the catalog

**Files:**
- Create: `lib/reports/use-report-sources.ts`
- Modify: `lib/reports/types.ts`, `components/reports/config/DataTab.tsx`, `components/reports/config/controlConstants.ts`
- Test: `tests/components/reports/data-tab-source-picker.test.tsx`

- [ ] **Step 1: Add types**

In `lib/reports/types.ts`: extend the `Dataset` union to `"transactions" | "accounts"`; add the new dimension literals (`"account_type" | "currency" | "account_active"`) to the `Dimension` union; add catalog types:

```typescript
export interface SourceCatalogFilter { field: string; label: string; ops: string[]; kind: string; }
export interface SourceCatalogDimension { key: string; label: string; kind: string; }
export interface SourceCatalogMeasure { key: string; label: string; agg: string; field: string; format: string; }
export interface SourceCatalogEntry {
  key: string; label: string;
  dimensions: SourceCatalogDimension[];
  measures: SourceCatalogMeasure[];
  filters: SourceCatalogFilter[];
}
```

- [ ] **Step 2: Write the failing test**

```tsx
// tests/components/reports/data-tab-source-picker.test.tsx
import { render, screen, fireEvent } from "@testing-library/react";
import DataTab from "@/components/reports/config/DataTab";
// Use the project's catalog-mock helper. The hook must be mockable; mock
// useReportSources to return a two-source catalog (transactions + accounts).

vi.mock("@/lib/reports/use-report-sources", () => ({
  useReportSources: () => ({
    sources: [
      { key: "transactions", label: "Transactions",
        dimensions: [{ key: "category", label: "Category", kind: "category" }],
        measures: [], filters: [] },
      { key: "accounts", label: "Accounts",
        dimensions: [{ key: "account_type", label: "Account type", kind: "account_type" }],
        measures: [], filters: [] },
    ],
    isLoading: false,
  }),
}));

test("source picker is enabled and lists catalog sources", () => {
  const widget = { type: "bar", config: { dataset: "transactions", measure: { agg: "sum", field: "amount" } } } as any;
  render(<DataTab widget={widget} onUpdate={() => {}} />);
  const picker = screen.getByLabelText("Data source") as HTMLSelectElement;
  expect(picker.disabled).toBe(false);
  expect(screen.getByRole("option", { name: "Accounts" })).toBeInTheDocument();
});

test("switching source resets dimension to the new source's first option", () => {
  const updates: any[] = [];
  const widget = { type: "bar", config: { dataset: "transactions", measure: { agg: "sum", field: "amount" }, dimensions: ["category"] } } as any;
  render(<DataTab widget={widget} onUpdate={(w) => updates.push(w)} />);
  fireEvent.change(screen.getByLabelText("Data source"), { target: { value: "accounts" } });
  const last = updates[updates.length - 1];
  expect(last.config.dataset).toBe("accounts");
  expect(last.config.dimensions ?? []).not.toContain("category"); // invalid dim dropped
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `docker compose exec frontend npx vitest run tests/components/reports/data-tab-source-picker.test.tsx`
Expected: FAIL — picker is `disabled`; no `useReportSources`.

- [ ] **Step 4: Implement the SWR hook**

```typescript
// lib/reports/use-report-sources.ts
import useSWR from "swr";
import { apiFetch } from "@/lib/api";
import type { SourceCatalogEntry } from "@/lib/reports/types";

const SOURCES_KEY = "/api/v1/reports/sources";

export function useReportSources() {
  const { data, isLoading } = useSWR<SourceCatalogEntry[]>(
    SOURCES_KEY,
    (url: string) => apiFetch(url).then((r) => r.json()),
    { revalidateOnFocus: false },
  );
  return { sources: data ?? [], isLoading };
}
```

> Match the project's real fetch idiom — check `components/reports/filters/AccountFilter.tsx` for how it calls the API (apiFetch vs a typed wrapper) and mirror it exactly.

- [ ] **Step 5: Wire the picker + reset-on-switch in `DataTab.tsx`**

Replace the disabled `<select>` (DataTab.tsx:44-53) with a live one populated from `useReportSources()`. On change, call `onUpdate` with the new dataset AND drop any `dimensions` / measure not present in the new source's catalog (reset primary dimension to the new source's first dimension key). Drive the primary/secondary dimension `<option>`s from the selected source's catalog dimensions instead of the static `DIMENSION_OPTIONS`. Add a `dimensionOptionsFor(entry)` helper in `controlConstants.ts` that maps catalog dimensions → `{value,label}`.

- [ ] **Step 6: Run the test**

Run: `docker compose exec frontend npx vitest run tests/components/reports/data-tab-source-picker.test.tsx`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add lib/reports/use-report-sources.ts lib/reports/types.ts components/reports/config/DataTab.tsx components/reports/config/controlConstants.ts tests/components/reports/data-tab-source-picker.test.tsx
git commit -m "feat(reports): catalog-driven source picker in the widget Data tab"
```

---

## Task 7: Frontend — resolve.ts stamps dataset + omits date for date-less sources

**Files:**
- Modify: `lib/reports/resolve.ts`
- Test: `tests/lib/reports/resolve-dataset.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// tests/lib/reports/resolve-dataset.test.tsx
import { resolveWidgetQuery } from "@/lib/reports/resolve"; // match the real export name

test("date canvas filter is omitted for a source without a date filter", () => {
  const accountsWidget = { type: "bar", config: { dataset: "accounts", measure: { agg: "sum", field: "balance" }, dimensions: ["account_type"] } } as any;
  const canvas = { date_range: { start: "2026-01-01", end: "2026-12-31" } } as any;
  // Pass the catalog so resolve knows accounts has no `date` filter.
  const ast = resolveWidgetQuery(accountsWidget, canvas, /* sourcesCatalog */ [
    { key: "accounts", label: "Accounts", dimensions: [], measures: [],
      filters: [{ field: "currency", label: "Currency", ops: ["eq"], kind: "currency" }] },
  ]);
  expect(ast.dataset).toBe("accounts");
  expect((ast.filters ?? []).some((f: any) => f.field === "date")).toBe(false);
});

test("date canvas filter is kept for transactions", () => {
  const txWidget = { type: "bar", config: { dataset: "transactions", measure: { agg: "sum", field: "amount" }, dimensions: ["category"] } } as any;
  const canvas = { date_range: { start: "2026-01-01", end: "2026-12-31" } } as any;
  const ast = resolveWidgetQuery(txWidget, canvas, [
    { key: "transactions", label: "Transactions", dimensions: [], measures: [],
      filters: [{ field: "date", label: "Date", ops: ["between"], kind: "time" }] },
  ]);
  expect((ast.filters ?? []).some((f: any) => f.field === "date")).toBe(true);
});
```

> Inspect `lib/reports/resolve.ts` first: confirm the real export name and current signature. If `resolve` does not currently receive the sources catalog, thread it through from the call sites (the report pages already have it once `useReportSources` lands). Keep the change minimal — a date filter is only emitted when the resolved source publishes a `date` filter field.

- [ ] **Step 2: Run to verify it fails**

Run: `docker compose exec frontend npx vitest run tests/lib/reports/resolve-dataset.test.tsx`
Expected: FAIL — date filter still stamped on the accounts widget (or signature mismatch).

- [ ] **Step 3: Implement**

In `resolve.ts`, when building the filter list, only append the canvas date filter if the resolved source's catalog entry has a filter with `field === "date"`. Stamp `dataset` from `widget.config.dataset`.

- [ ] **Step 4: Run the test**

Run: `docker compose exec frontend npx vitest run tests/lib/reports/resolve-dataset.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lib/reports/resolve.ts tests/lib/reports/resolve-dataset.test.tsx
git commit -m "feat(reports): resolver omits canvas date filter for date-less sources"
```

---

## Task 8: Frontend verification gate

**Files:** none — full-suite gate ([[reference_frontend_full_suite_verification]], [[reference_eslint_ci_gate_misses]]).

- [ ] **Step 1: ESLint (CI gate — tsc+vitest green is NOT enough)**

Run: `docker compose exec frontend npx eslint . --quiet`
Expected: clean.

- [ ] **Step 2: Type-check**

Run: `docker compose exec frontend npx tsc --noEmit`
Expected: clean.

- [ ] **Step 3: Full vitest run (NOT just touched files)**

Run: `docker compose exec frontend npx vitest run`
Expected: all pass (known flake: `settings-ai-providers-page > "PUTs the default routing payload on save"` — passes in isolation; not a regression).

- [ ] **Step 4: Commit any fixups**

```bash
git commit -am "chore(reports): lint/type fixups for accounts source" || true
```

---

## Final: review + PR

- [ ] Self/fleet review per the Reports v3 wave standard (multi-dimension adversarial review → per-finding verification → fold confirmed). Surface the `is_active` default (include-all vs active-only) to the operator explicitly.
- [ ] Open PR titled `feat(reports): accounts data source for report widgets` (conventional-commits — the title is the release/deploy gate). Concise body, no test-plan section, no AI attribution.

---

## Self-review checklist (completed by plan author)

- **Spec coverage:** §1 enum-widening → Tasks 1–2; §2 validation split → Tasks 2–3; §3 catalog filters → Task 3; §4 AccountsSource → Task 4; §5 kind guard → Task 4 Step 6; §6 date two-tier → Tasks 3 (drop-set), 4 (build drop), 7 (frontend omit); §7 multi-currency → currency dim in Task 4 + documented; §8 frontend → Tasks 6–7; §9 tests → Tasks 1–7 (TDD). ✓
- **Type consistency:** `validate_against_catalog`, `SourceFilter(field,label,ops,kind)`, `SHARED_CANVAS_FILTER_FIELDS`, `NUMERIC_MEASURE_FIELDS`, `useReportSources`, `resolveWidgetQuery` used consistently across tasks. ✓ (`resolveWidgetQuery` export name to be confirmed against `resolve.ts` in Task 7 Step 1.)
- **No migration:** confirmed — enum values live in JSON columns. ✓
- **Open verification points flagged inline:** real fetch idiom (Task 6), resolve.ts signature/export (Task 7), conftest account-factory reuse (Task 4).
