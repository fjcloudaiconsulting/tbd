# Reports v3 — Phase 1 Implementation Plan (Backend Source Registry)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a pluggable `ReportSource` registry behind the existing reports query path, plus a `GET /api/v1/reports/sources` catalog endpoint — with the transactions query path behaving identically (parity).

**Architecture:** The `POST /api/v1/reports/query` AST already carries a `dataset` discriminator (`ReportsQuery.dataset`, today a single-value enum). Phase 1 inserts a registry indirection: the router dispatches on `dataset` to a registered `ReportSource`, and Phase 1 registers exactly one — `TransactionsSource` — which delegates to the existing `execute_query` compiler. A new `/sources` endpoint returns each registered source's dimensions + measures (derived from the existing closed enums) so the frontend can later drive the editor from data. No enum widening, no per-source measure validation, and no new wire fields yet — those land in Phase 5 when a second source exists. This phase is pure, behavior-preserving indirection + a read-only catalog.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic v2, pytest (async). Tests run in an **isolated compose project** per the parallel-agent rule: `docker compose -p team-reportsv3-p1 up -d backend mysql redis`, then `docker compose -p team-reportsv3-p1 exec backend pytest ...`.

**Reference files (read before starting):**
- `backend/app/services/reports_query_service.py` — `execute_query(db, ast: ReportsQuery, *, org_id: int) -> tuple[list[dict], QueryMeta]`; the compiler this phase wraps. **Read it fully first.**
- `backend/app/schemas/reports_query.py` — the AST: `Dataset`, `Aggregation`, `MeasureField`, `Dimension`, `Measure`, `ReportsQuery`, `QueryMeta`, `ReportsQueryResponse`.
- `backend/app/routers/reports.py:155-172` — the existing `run_query` route that calls `execute_query`; this is the call site we redirect through the registry.
- `backend/tests/services/test_reports_query_service.py` + `backend/tests/routers/test_reports.py` — the existing parity baseline (must stay green unchanged). New tests go in `backend/tests/services/` (unit) and `backend/tests/routers/` (endpoint).

---

## File Structure

- **Create** `backend/app/reports/sources/__init__.py` — registry: `ReportSource` protocol, `register`/`get_source`/`all_sources`, and the module that imports the concrete sources so they self-register.
- **Create** `backend/app/reports/sources/base.py` — `ReportSource` Protocol + the `Dimension`/`Measure` catalog dataclasses (`SourceDimension`, `SourceMeasure`) returned by `/sources`.
- **Create** `backend/app/reports/sources/transactions.py` — `TransactionsSource`: `key="transactions"`, `dimensions()`, `measures()`, `build_rows()` delegating to `execute_query`.
- **Create** `backend/app/schemas/report_sources.py` — response models for `GET /sources` (`SourceCatalogEntry`, `SourceDimensionOut`, `SourceMeasureOut`).
- **Modify** `backend/app/routers/reports.py` — redirect `run_query` through the registry dispatcher; add `GET /sources`.
- **Create** `backend/tests/services/test_report_sources_registry.py` — registry + catalog + dispatch tests.
- The existing reports-query parity tests stay green **unchanged** (no wire change in Phase 1).

> **Note on `reports/` package:** if `backend/app/reports/` already exists (it holds `templates.py`, imported in `routers/reports.py:51`), add the `sources/` subpackage inside it. If `app/reports/__init__.py` is missing, create it.

---

### Task 1: `ReportSource` protocol + catalog dataclasses

**Files:**
- Create: `backend/app/reports/sources/base.py`
- Test: `backend/tests/services/test_report_sources_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/services/test_report_sources_registry.py
from app.reports.sources.base import ReportSource, SourceDimension, SourceMeasure


def test_source_dimension_and_measure_are_simple_value_objects():
    dim = SourceDimension(key="category", label="Category", kind="category")
    meas = SourceMeasure(key="sum_amount", label="Total", agg="sum", field="amount", format="currency")
    assert dim.key == "category" and dim.kind == "category"
    assert meas.agg == "sum" and meas.format == "currency"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest tests/services/test_report_sources_registry.py::test_source_dimension_and_measure_are_simple_value_objects -v`
Expected: FAIL with `ModuleNotFoundError: app.reports.sources.base`

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/reports/sources/base.py
"""Reports v3 — pluggable source layer.

A ``ReportSource`` answers three questions: which dimensions can you
group/filter by, which measures can you plot, and how do you build the
rows for a query. The registry dispatches the reports query AST on its
``dataset`` discriminator to the registered source. Phase 1 ships one
source (transactions) that delegates to the existing compiler; the
interface is what makes accounts / recurring / net-worth additive later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.reports_query import QueryMeta, ReportsQuery


@dataclass(frozen=True)
class SourceDimension:
    key: str       # matches the AST Dimension value, e.g. "category"
    label: str     # human label for the editor, e.g. "Category"
    kind: str      # control hint: category|account|status|type|tag|time|account_type


@dataclass(frozen=True)
class SourceMeasure:
    key: str       # stable id for the editor, e.g. "sum_amount"
    label: str     # human label, e.g. "Total amount"
    agg: str       # sum|count|avg|distinct
    field: str     # amount|id|category_id|account_id (AST MeasureField value)
    format: str    # currency|number|percent


@runtime_checkable
class ReportSource(Protocol):
    key: str
    label: str

    def dimensions(self) -> list[SourceDimension]: ...

    def measures(self) -> list[SourceMeasure]: ...

    async def build_rows(
        self, db: AsyncSession, org_id: int, query: ReportsQuery
    ) -> tuple[list[dict], QueryMeta]: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest tests/services/test_report_sources_registry.py::test_source_dimension_and_measure_are_simple_value_objects -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/reports/sources/base.py backend/tests/services/test_report_sources_registry.py
git commit -m "feat(reports): ReportSource protocol + catalog value objects"
```

---

### Task 2: Registry (register / get_source / all_sources)

**Files:**
- Create: `backend/app/reports/sources/__init__.py`
- Test: `backend/tests/services/test_report_sources_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/services/test_report_sources_registry.py
import pytest
from app.reports import sources as source_registry


def test_registry_resolves_transactions_and_rejects_unknown():
    src = source_registry.get_source("transactions")
    assert src.key == "transactions"
    assert "transactions" in {s.key for s in source_registry.all_sources()}
    with pytest.raises(KeyError):
        source_registry.get_source("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest tests/services/test_report_sources_registry.py::test_registry_resolves_transactions_and_rejects_unknown -v`
Expected: FAIL — `get_source`/`all_sources` not defined (and `transactions` source not yet built; this passes after Task 3 registers it).

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/reports/sources/__init__.py
"""Source registry. Concrete sources self-register on import."""
from __future__ import annotations

from app.reports.sources.base import ReportSource

_REGISTRY: dict[str, ReportSource] = {}


def register(source: ReportSource) -> None:
    if source.key in _REGISTRY:
        raise ValueError(f"duplicate report source key: {source.key!r}")
    _REGISTRY[source.key] = source


def get_source(key: str) -> ReportSource:
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise KeyError(f"unknown report source: {key!r}") from exc


def all_sources() -> list[ReportSource]:
    return list(_REGISTRY.values())


# Import concrete sources so they self-register. Kept at the bottom to
# avoid a circular import (transactions.py imports from .base).
from app.reports.sources import transactions as _transactions  # noqa: E402,F401
```

- [ ] **Step 4: Run test** — will still fail until Task 3 creates `transactions.py`. That is expected; proceed to Task 3, then re-run.

- [ ] **Step 5: Commit** (after Task 3 makes it green — commit registry + source together)

---

### Task 3: `TransactionsSource` (parity delegate)

**Files:**
- Create: `backend/app/reports/sources/transactions.py`
- Test: `backend/tests/services/test_report_sources_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# append to backend/tests/services/test_report_sources_registry.py
def test_transactions_source_catalog_matches_ast_enums():
    from app.reports import sources as source_registry
    from app.schemas.reports_query import Dimension as AstDimension

    src = source_registry.get_source("transactions")
    dim_keys = {d.key for d in src.dimensions()}
    # Every catalog dimension is a real AST Dimension value (no typos).
    assert dim_keys.issubset({d.value for d in AstDimension})
    assert {"category", "account", "status", "txn_type", "month"}.issubset(dim_keys)
    measure_keys = {m.key for m in src.measures()}
    assert {"sum_amount", "count_rows"}.issubset(measure_keys)
    # Currency measures advertise currency format for the formatter.
    sum_amount = next(m for m in src.measures() if m.key == "sum_amount")
    assert sum_amount.format == "currency" and sum_amount.field == "amount"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest tests/services/test_report_sources_registry.py -k transactions_source_catalog -v`
Expected: FAIL — `transactions.py` missing.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/reports/sources/transactions.py
"""Transactions source — wraps the existing reports query compiler.

build_rows delegates to ``execute_query`` verbatim, so the transactions
query path is byte-for-byte identical to pre-registry behavior. The
catalog (dimensions/measures) is derived from the closed AST enums so it
cannot drift from what the compiler actually accepts.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.reports.sources import register
from app.reports.sources.base import ReportSource, SourceDimension, SourceMeasure
from app.schemas.reports_query import QueryMeta, ReportsQuery
from app.services.reports_query_service import execute_query

_DIMENSIONS = [
    SourceDimension("category", "Category", "category"),
    SourceDimension("category_master", "Category group", "category"),
    SourceDimension("account", "Account", "account"),
    SourceDimension("tag", "Tag", "tag"),
    SourceDimension("txn_type", "Type", "type"),
    SourceDimension("status", "Status", "status"),
    SourceDimension("month", "Month", "time"),
    SourceDimension("week", "Week", "time"),
    SourceDimension("day", "Day", "time"),
]

_MEASURES = [
    SourceMeasure("sum_amount", "Total amount", "sum", "amount", "currency"),
    SourceMeasure("avg_amount", "Average amount", "avg", "amount", "currency"),
    SourceMeasure("count_rows", "Transaction count", "count", "id", "number"),
]


class TransactionsSource:
    key = "transactions"
    label = "Transactions"

    def dimensions(self) -> list[SourceDimension]:
        return list(_DIMENSIONS)

    def measures(self) -> list[SourceMeasure]:
        return list(_MEASURES)

    async def build_rows(
        self, db: AsyncSession, org_id: int, query: ReportsQuery
    ) -> tuple[list[dict], QueryMeta]:
        return await execute_query(db, query, org_id=org_id)


_INSTANCE: ReportSource = TransactionsSource()
register(_INSTANCE)
```

- [ ] **Step 4: Run the full registry test file**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest tests/services/test_report_sources_registry.py -v`
Expected: all PASS (Task 2's test now green too).

- [ ] **Step 5: Commit**

```bash
git add backend/app/reports/sources/__init__.py backend/app/reports/sources/transactions.py backend/tests/services/test_report_sources_registry.py
git commit -m "feat(reports): source registry + TransactionsSource parity delegate"
```

---

### Task 4: Dispatch `POST /query` through the registry

**Files:**
- Modify: `backend/app/routers/reports.py:155-172` (the `run_query` route)
- Test: existing reports-query parity test module (must stay green) + a new dispatch assertion in `test_report_sources_registry.py`

- [ ] **Step 1: Write the failing test** (dispatch indirection is used, not `execute_query` directly)

```python
# append to backend/tests/services/test_report_sources_registry.py
@pytest.mark.asyncio
async def test_run_query_dispatches_via_registry(monkeypatch):
    """The route resolves the source from the AST dataset and calls its
    build_rows — proving the indirection, not a direct execute_query call."""
    from app.routers import reports as reports_router
    from app.schemas.reports_query import (
        Aggregation, Dataset, Dimension, Measure, MeasureField, QueryMeta, ReportsQuery,
    )

    called = {}

    async def fake_build_rows(db, org_id, query):
        called["org_id"] = org_id
        called["dataset"] = query.dataset.value
        return ([{"category": "Food", "value": 12}], QueryMeta(row_count=1, truncated=False, query_ms=1))

    src = reports_router.get_source("transactions")
    monkeypatch.setattr(src, "build_rows", fake_build_rows)

    ast = ReportsQuery(
        dataset=Dataset.TRANSACTIONS,
        measure=Measure(agg=Aggregation.SUM, field=MeasureField.AMOUNT),
        dimensions=[Dimension.CATEGORY],
    )
    rows, meta = await reports_router._run_source_query(_db_stub(), ast, org_id=42)
    assert called == {"org_id": 42, "dataset": "transactions"}
    assert rows[0]["category"] == "Food" and meta.row_count == 1


def _db_stub():
    return object()  # build_rows is monkeypatched; the session is never touched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest tests/services/test_report_sources_registry.py -k run_query_dispatches -v`
Expected: FAIL — `reports_router._run_source_query` / `get_source` not imported in router.

- [ ] **Step 3: Write minimal implementation** — edit `backend/app/routers/reports.py`

Add the import near the other `app.services` import (replace the direct `execute_query` import):

```python
from app.reports.sources import get_source, all_sources
```

Add a dispatcher helper above `run_query`:

```python
async def _run_source_query(db, ast, *, org_id: int):
    """Resolve the AST's dataset to a registered ReportSource and run it.

    Unknown dataset is impossible from the wire (the Dataset enum is
    closed and validated by Pydantic), so a KeyError here is a server
    bug, not user input — let it surface as 500.
    """
    source = get_source(ast.dataset.value)
    return await source.build_rows(db, org_id, ast)
```

Change the body of `run_query` to call the dispatcher:

```python
    rows, meta = await _run_source_query(db, body, org_id=current_user.org_id)
    return ReportsQueryResponse(rows=rows, meta=meta)
```

(Remove the now-unused `from app.services.reports_query_service import execute_query` import — `TransactionsSource` owns that call now. Confirm nothing else in the router references `execute_query`.)

- [ ] **Step 4: Run the dispatch test AND the full existing reports-query suite**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest tests/services/test_report_sources_registry.py tests/services/test_reports_query_service.py -v`
(substitute the real parity test module name discovered in pre-reading)
Expected: dispatch test PASS; **every pre-existing parity test PASS unchanged** (proves no behavior change).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/reports.py backend/tests/services/test_report_sources_registry.py
git commit -m "feat(reports): dispatch /query through the source registry"
```

---

### Task 5: `GET /api/v1/reports/sources` catalog endpoint

**Files:**
- Create: `backend/app/schemas/report_sources.py`
- Modify: `backend/app/routers/reports.py` (add route ABOVE `GET /{report_id}` so the literal path isn't captured by the int matcher — same reason as `/templates` at line 223)
- Test: a router/integration test (mirror the existing reports router test's auth + client setup)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/routers/test_report_sources_endpoint.py
# Reuse the existing reports-router test fixtures (authed async client + a
# user in an org + feature_reports_v2 enabled). Copy the fixture wiring
# from the existing reports router test module discovered in pre-reading.
import pytest


@pytest.mark.asyncio
async def test_sources_endpoint_lists_transactions_catalog(authed_client):
    resp = await authed_client.get("/api/v1/reports/sources")
    assert resp.status_code == 200
    body = resp.json()
    keys = {s["key"] for s in body}
    assert "transactions" in keys
    tx = next(s for s in body if s["key"] == "transactions")
    assert tx["label"] == "Transactions"
    assert any(d["key"] == "category" for d in tx["dimensions"])
    assert any(m["key"] == "sum_amount" and m["format"] == "currency" for m in tx["measures"])


@pytest.mark.asyncio
async def test_sources_endpoint_404_when_flag_off(authed_client_flag_off):
    resp = await authed_client_flag_off.get("/api/v1/reports/sources")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest tests/routers/test_report_sources_endpoint.py -v`
Expected: FAIL — 404 (route not defined) for the first test.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/schemas/report_sources.py
from pydantic import BaseModel


class SourceDimensionOut(BaseModel):
    key: str
    label: str
    kind: str


class SourceMeasureOut(BaseModel):
    key: str
    label: str
    agg: str
    field: str
    format: str


class SourceCatalogEntry(BaseModel):
    key: str
    label: str
    dimensions: list[SourceDimensionOut]
    measures: list[SourceMeasureOut]
```

In `backend/app/routers/reports.py`, add the import:

```python
from app.schemas.report_sources import (
    SourceCatalogEntry, SourceDimensionOut, SourceMeasureOut,
)
```

And the route, placed immediately ABOVE `GET /{report_id}` (after `/templates`):

```python
@router.get("/sources", response_model=list[SourceCatalogEntry])
async def list_sources(current_user: User = Depends(get_current_user)):
    """Catalog of reportable sources with their dimensions + measures.

    Drives the widget editor's source/dimension/measure pickers so the
    frontend hardcodes nothing about a source's shape. Auth-gated +
    behind the reports-v2 flag like every route on this router.
    """
    return [
        SourceCatalogEntry(
            key=s.key,
            label=s.label,
            dimensions=[SourceDimensionOut(**vars(d)) for d in s.dimensions()],
            measures=[SourceMeasureOut(**vars(m)) for m in s.measures()],
        )
        for s in all_sources()
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest tests/routers/test_report_sources_endpoint.py -v`
Expected: PASS (both — the flag-off 404 is provided by the existing `require_reports_v2_enabled` router dependency).

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/report_sources.py backend/app/routers/reports.py backend/tests/routers/test_report_sources_endpoint.py
git commit -m "feat(reports): GET /reports/sources catalog endpoint"
```

---

### Task 6: Full-suite green + branch verification

- [ ] **Step 1: Run the entire backend reports test surface**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest tests/ -k "report" -v`
Expected: all PASS — new registry/catalog tests + every pre-existing reports test (parity proof).

- [ ] **Step 2: Run the full backend suite (catch unrelated breakage from the import change)**

Run: `docker compose -p team-reportsv3-p1 exec backend pytest -q`
Expected: no new failures vs. the pre-change baseline.

- [ ] **Step 3: Tear down the isolated stack**

Run: `docker compose -p team-reportsv3-p1 down -v`

- [ ] **Step 4: Open the PR** (branch `feat/reports-v3`, conventional title)

PR title: `feat(reports): pluggable source registry + /sources catalog (Reports v3 phase 1)`
Body: 2-3 lines, no test-plan section, no AI attribution (per project rules).

---

## Self-Review

**Spec coverage (Phase 1 rows):** registry ✓ (Tasks 1-3) · `/query` dispatch ✓ (Task 4) · `/sources` catalog ✓ (Task 5) · TransactionsSource parity ✓ (Task 3 delegate + Task 4/6 parity runs). Enum widening + per-source measure validation are **correctly deferred to Phase 5** (no second source exists yet) — noted in Architecture so a reader doesn't expect them here.

**Placeholder scan:** none — every code step has complete code. The one runtime unknown (the exact parity test module name) is flagged explicitly as a pre-reading lookup, not left silent.

**Type consistency:** `SourceDimension`/`SourceMeasure` (dataclasses, Task 1) → consumed by `TransactionsSource` (Task 3) → serialized via `SourceDimensionOut`/`SourceMeasureOut` (`vars(d)` maps dataclass fields to the Pydantic fields 1:1, Task 5). `get_source`/`all_sources` defined Task 2, imported into the router Task 4/5. `_run_source_query` defined + tested Task 4. `build_rows` signature identical across base.py, transactions.py, and the dispatch test.

**Out-of-scope guard:** no frontend changes, no `report_layout.py` change, no `Dataset` enum change, no migration — all consistent with "behavior-preserving Phase 1."
