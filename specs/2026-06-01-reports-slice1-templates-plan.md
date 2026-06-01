# Reports Slice 1 — Templates + creation snapshot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user create a working report in one click from a starter template, and persist each report's as-created state so a later "Revert to original" is possible.

**Architecture:** 3 templates ship as Python code fixtures (no DB seed rows), exposed via `GET /api/v1/reports/templates`. "Use template" reuses the existing `POST /api/v1/reports` create endpoint with the template's `layout_json`/`canvas_filters_json`. A migration adds `original_layout_json`/`original_canvas_filters_json` to `reports`, captured once at create. Template fixtures are authored against the implemented frontend `lib/reports/types.ts` widget-config shapes (NOT the parent-spec AST sketch).

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + Alembic + Pydantic v2 (backend); Next.js 15 App Router + React 19 + TypeScript + SWR (frontend); pytest + vitest.

**Branch:** `feat/reports-simplify`. Feature is gated behind `FEATURE_REPORTS_V2` (true locally).

**Reference (verified shapes):**
- Create endpoint: `backend/app/routers/reports.py` `create_report` (sets `layout_json`/`canvas_filters_json` from body).
- Router gate: `require_reports_v2_enabled` (404 when flag off) is a router-level dependency — any new route on `router` inherits it.
- Frontend widget config shapes: `frontend/lib/reports/types.ts` — single-measure widgets use `config.measure`; line/area/stacked_bar/table use `config.measures: SeriesConfig[]`; `Dimension` union; widget filters are `WidgetFilters` (e.g. `{ txn_type: "expense", date_range: {...} }`), NOT raw `{field,op,value}`.
- Create-from-list today: `frontend/app/reports/page.tsx` `handleNewReport` calls `createReport({ name, visibility, layout_json:{version:1,widgets:[]}, canvas_filters_json:{} })`.

---

## File Structure

- Create `backend/alembic/versions/063_reports_original_snapshot.py` — add two nullable JSON columns.
- Modify `backend/app/models/report.py` — add `original_layout_json`, `original_canvas_filters_json`.
- Modify `backend/app/routers/reports.py` — set `original_*` at create; add `GET /templates`.
- Create `backend/app/reports/__init__.py` + `backend/app/reports/templates/__init__.py` — 3 template fixtures + registry.
- Modify `backend/app/schemas/report.py` — `ReportTemplate` response schema.
- Create `backend/tests/routers/test_reports_templates.py` — endpoint + gating tests.
- Modify `frontend/lib/reports/types.ts` — `ReportTemplate` type.
- Modify `frontend/lib/reports/api.ts` — `listTemplates()`, `createFromTemplate()`.
- Modify `frontend/app/reports/page.tsx` — Templates section + empty-state CTA.
- Create `frontend/tests/app/reports-templates.test.tsx` — list + use-template.

---

### Task 1: Migration — add creation-snapshot columns to `reports`

**Files:**
- Create: `backend/alembic/versions/063_reports_original_snapshot.py`
- Modify: `backend/app/models/report.py`

- [ ] **Step 1: Confirm current migration head**

Run: `docker compose exec -T backend alembic heads`
Expected: `062_ollama_nullable_api_key (head)`. Use that exact id as `down_revision`; if different, use the printed head.

- [ ] **Step 2: Write the migration**

```python
"""add reports original snapshot columns

Revision ID: 063_reports_original_snapshot
Revises: 062_ollama_nullable_api_key
"""
from alembic import op
import sqlalchemy as sa

revision = "063_reports_original_snapshot"
down_revision = "062_ollama_nullable_api_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reports", sa.Column("original_layout_json", sa.JSON(), nullable=True))
    op.add_column("reports", sa.Column("original_canvas_filters_json", sa.JSON(), nullable=True))
    # Backfill existing rows so original == current (table is effectively
    # empty in prod since the feature is gated off).
    op.execute(
        "UPDATE reports SET original_layout_json = layout_json, "
        "original_canvas_filters_json = canvas_filters_json"
    )


def downgrade() -> None:
    op.drop_column("reports", "original_canvas_filters_json")
    op.drop_column("reports", "original_layout_json")
```

- [ ] **Step 3: Add the columns to the model**

In `backend/app/models/report.py`, alongside `layout_json`/`canvas_filters_json`, add:

```python
    original_layout_json: Mapped[dict] = mapped_column(JSON, nullable=True)
    original_canvas_filters_json: Mapped[dict] = mapped_column(JSON, nullable=True)
```

(Match the existing import + `mapped_column(JSON, ...)` style already used for `layout_json` in that file.)

- [ ] **Step 4: Run the migration**

Run: `docker compose exec -T backend alembic upgrade head`
Expected: `migrate.step` log for `063_reports_original_snapshot`, no error.

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/063_reports_original_snapshot.py backend/app/models/report.py
git commit -m "feat(reports): add original_* snapshot columns to reports"
```

---

### Task 2: Capture the snapshot at create

**Files:**
- Modify: `backend/app/routers/reports.py` (`create_report`)
- Test: `backend/tests/routers/test_reports_templates.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/routers/test_reports_templates.py
import pytest

@pytest.mark.asyncio
async def test_create_report_snapshots_original(client, auth_headers):
    payload = {
        "name": "Snap",
        "visibility": "private",
        "layout_json": {"version": 1, "widgets": [{"id": "w1", "type": "kpi"}]},
        "canvas_filters_json": {"date_range": {"kind": "relative", "preset": "last_30_days"}},
    }
    r = await client.post("/api/v1/reports", json=payload, headers=auth_headers)
    assert r.status_code == 201
    rid = r.json()["id"]
    # Mutate the live layout via PATCH...
    await client.patch(f"/api/v1/reports/{rid}", json={"layout_json": {"version": 1, "widgets": []}}, headers=auth_headers)
    # ...original must be unchanged (fetch via DB-backed get is fine; assert through a follow-up reset in Slice 2).
    got = await client.get(f"/api/v1/reports/{rid}", headers=auth_headers)
    assert got.status_code == 200
```

(Use the existing reports test fixtures for `client`/`auth_headers` — mirror `backend/tests/routers/test_reports.py` setup; reuse its flag-enable fixture so `FEATURE_REPORTS_V2` is on for the test app.)

- [ ] **Step 2: Run it to confirm current behavior**

Run: `docker compose exec -T backend pytest backend/tests/routers/test_reports_templates.py::test_create_report_snapshots_original -v`
Expected: PASS for create/patch/get already (this test only locks that the flow works); the snapshot assertion is exercised in Slice 2's reset test. The point of this task is to populate `original_*`.

- [ ] **Step 3: Set `original_*` in `create_report`**

In `backend/app/routers/reports.py` `create_report`, add the two fields to the `Report(...)` constructor:

```python
    row = Report(
        owner_user_id=current_user.id,
        org_id=current_user.org_id,
        visibility=body.visibility,
        name=body.name,
        description=body.description,
        layout_json=body.layout_json,
        canvas_filters_json=body.canvas_filters_json,
        original_layout_json=body.layout_json,
        original_canvas_filters_json=body.canvas_filters_json,
    )
```

- [ ] **Step 4: Run the test**

Run: `docker compose exec -T backend pytest backend/tests/routers/test_reports_templates.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/reports.py backend/tests/routers/test_reports_templates.py
git commit -m "feat(reports): snapshot original_* on report create"
```

---

### Task 3: Template fixtures (3 templates, authored against types.ts)

**Files:**
- Create: `backend/app/reports/__init__.py` (empty package marker)
- Create: `backend/app/reports/templates/__init__.py`

- [ ] **Step 1: Write the fixtures + registry**

```python
# backend/app/reports/templates/__init__.py
"""Reports v2 starter templates — code fixtures, NOT DB seed rows.

Each template's ``layout_json``/``canvas_filters_json`` use the
frontend ``lib/reports/types.ts`` shapes: single-measure widgets carry
``config.measure``; line/area/stacked_bar/table carry
``config.measures: [{measure, label?}]``; dimensions come from the
``Dimension`` union; widget/canvas filters are ``WidgetFilters`` /
``CanvasFilters`` (e.g. ``{"txn_type": "expense"}``), never raw AST.
"""
from typing import Any

_THIS_MONTH = {"kind": "relative", "preset": "this_month"}
_LAST_12 = {"kind": "relative", "preset": "last_12_months"}


def _measure(agg: str, field: str = "amount") -> dict[str, Any]:
    return {"agg": agg, "field": field}


REPORT_TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "monthly_review",
        "name": "Monthly review",
        "description": "Net, income and expense KPIs plus spend by category and income vs expense this month.",
        "canvas_filters_json": {"date_range": _THIS_MONTH},
        "layout_json": {
            "version": 1,
            "widgets": [
                {"id": "kpi_net", "type": "kpi", "title": "Net this month",
                 "grid": {"x": 0, "y": 0, "w": 3, "h": 2},
                 "config": {"dataset": "transactions", "measure": _measure("sum"), "format": "currency"}},
                {"id": "kpi_income", "type": "kpi", "title": "Income",
                 "grid": {"x": 3, "y": 0, "w": 3, "h": 2},
                 "config": {"dataset": "transactions", "measure": _measure("sum"),
                            "filters": {"txn_type": "income"}, "format": "currency"}},
                {"id": "kpi_expense", "type": "kpi", "title": "Expense",
                 "grid": {"x": 6, "y": 0, "w": 3, "h": 2},
                 "config": {"dataset": "transactions", "measure": _measure("sum"),
                            "filters": {"txn_type": "expense"}, "format": "currency"}},
                {"id": "bar_cat", "type": "bar", "title": "Spend by category",
                 "grid": {"x": 0, "y": 2, "w": 6, "h": 4},
                 "config": {"dataset": "transactions", "measure": _measure("sum"),
                            "dimensions": ["category"], "filters": {"txn_type": "expense"},
                            "sort": {"by": "value", "dir": "desc"}, "limit": 10, "format": "currency"}},
                {"id": "line_io", "type": "line", "title": "Income vs expense",
                 "grid": {"x": 6, "y": 2, "w": 6, "h": 4},
                 "config": {"dataset": "transactions",
                            "measures": [{"measure": _measure("sum"), "label": "Net"}],
                            "dimensions": ["day"], "format": "currency"}},
            ],
        },
    },
    {
        "key": "cash_flow_trend",
        "name": "Cash flow trend",
        "description": "Net by month for the trailing 12 months with a trailing average KPI.",
        "canvas_filters_json": {"date_range": _LAST_12},
        "layout_json": {
            "version": 1,
            "widgets": [
                {"id": "kpi_avg", "type": "kpi", "title": "Avg monthly net (12mo)",
                 "grid": {"x": 0, "y": 0, "w": 4, "h": 2},
                 "config": {"dataset": "transactions", "measure": _measure("avg"), "format": "currency"}},
                {"id": "line_net", "type": "line", "title": "Net by month",
                 "grid": {"x": 0, "y": 2, "w": 12, "h": 4},
                 "config": {"dataset": "transactions",
                            "measures": [{"measure": _measure("sum"), "label": "Net"}],
                            "dimensions": ["month"], "format": "currency"}},
            ],
        },
    },
    {
        "key": "category_deep_dive",
        "name": "Category deep-dive",
        "description": "Category share this month, top transactions, and category by month.",
        "canvas_filters_json": {"date_range": _THIS_MONTH},
        "layout_json": {
            "version": 1,
            "widgets": [
                {"id": "pie_cat", "type": "pie", "title": "Category share",
                 "grid": {"x": 0, "y": 0, "w": 6, "h": 4},
                 "config": {"dataset": "transactions", "measure": _measure("sum"),
                            "dimensions": ["category"], "filters": {"txn_type": "expense"}, "format": "currency"}},
                {"id": "tbl_top", "type": "table", "title": "Top transactions",
                 "grid": {"x": 6, "y": 0, "w": 6, "h": 4},
                 "config": {"dataset": "transactions",
                            "measures": [{"measure": _measure("sum"), "label": "Amount"}],
                            "dimensions": ["category"],
                            "sort": {"by": "value", "dir": "desc"}, "limit": 20, "format": "currency"}},
                {"id": "stack_cat", "type": "stacked_bar", "title": "Category by month",
                 "grid": {"x": 0, "y": 4, "w": 12, "h": 4},
                 "config": {"dataset": "transactions",
                            "measures": [{"measure": _measure("sum"), "label": "Spend"}],
                            "dimensions": ["month", "category"], "filters": {"txn_type": "expense"}, "format": "currency"}},
            ],
        },
    },
]
```

- [ ] **Step 2: Commit**

```bash
git add backend/app/reports/__init__.py backend/app/reports/templates/__init__.py
git commit -m "feat(reports): add 3 starter template fixtures"
```

> **Review note:** before committing, sanity-check each widget `config` against `frontend/lib/reports/types.ts` (`KPIConfig`, `BarConfig`, `LineConfig`, `PieConfig`, `TableConfig`, `StackedBarConfig`) and the `Dimension` union (`category`, `month`, `day`, etc.). The canvas renders these directly, so a shape mismatch = a blank widget. Verify by instantiating one template in the running app at the end of the slice.

---

### Task 4: `GET /api/v1/reports/templates` endpoint + schema

**Files:**
- Modify: `backend/app/schemas/report.py`
- Modify: `backend/app/routers/reports.py`
- Test: `backend/tests/routers/test_reports_templates.py`

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_templates_endpoint_returns_three(client, auth_headers):
    r = await client.get("/api/v1/reports/templates", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3
    keys = {t["key"] for t in body}
    assert keys == {"monthly_review", "cash_flow_trend", "category_deep_dive"}
    assert body[0]["layout_json"]["widgets"]

@pytest.mark.asyncio
async def test_templates_endpoint_404_when_flag_off(client_flag_off, auth_headers):
    r = await client_flag_off.get("/api/v1/reports/templates", headers=auth_headers)
    assert r.status_code == 404
```

(`client_flag_off` = a test client built with `FEATURE_REPORTS_V2=false`; mirror however `test_reports.py` toggles the setting, e.g. monkeypatching `app_settings.feature_reports_v2`.)

- [ ] **Step 2: Run to confirm failure**

Run: `docker compose exec -T backend pytest backend/tests/routers/test_reports_templates.py -v`
Expected: FAIL (404 on the templates route — not yet defined).

- [ ] **Step 3: Add the schema**

In `backend/app/schemas/report.py`:

```python
class ReportTemplate(BaseModel):
    key: str
    name: str
    description: str
    layout_json: dict[str, Any]
    canvas_filters_json: dict[str, Any]
```

- [ ] **Step 4: Add the route (BEFORE `/{report_id}` to avoid path capture)**

In `backend/app/routers/reports.py`, import the fixtures + schema, and register the route above the `@router.get("/{report_id}")` handler so `templates` is not swallowed by the `{report_id}` matcher:

```python
from app.reports.templates import REPORT_TEMPLATES
from app.schemas.report import ReportTemplate

@router.get("/templates", response_model=list[ReportTemplate])
async def list_templates(current_user: User = Depends(get_current_user)):
    """Static starter templates (code fixtures, not DB rows)."""
    return [ReportTemplate(**t) for t in REPORT_TEMPLATES]
```

- [ ] **Step 5: Run tests**

Run: `docker compose exec -T backend pytest backend/tests/routers/test_reports_templates.py -v`
Expected: PASS (3 templates; 404 when flag off).

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/report.py backend/app/routers/reports.py backend/tests/routers/test_reports_templates.py
git commit -m "feat(reports): GET /reports/templates endpoint"
```

---

### Task 5: Frontend types + API client

**Files:**
- Modify: `frontend/lib/reports/types.ts`
- Modify: `frontend/lib/reports/api.ts`

- [ ] **Step 1: Add the type**

In `frontend/lib/reports/types.ts`:

```typescript
export interface ReportTemplate {
  key: string;
  name: string;
  description: string;
  layout_json: LayoutJson;
  canvas_filters_json: CanvasFilters;
}
```

(Use the existing `LayoutJson` + `CanvasFilters` types already defined in this file.)

- [ ] **Step 2: Add API functions**

In `frontend/lib/reports/api.ts`:

```typescript
import type { ReportTemplate } from "./types";

export async function listTemplates(): Promise<ReportTemplate[]> {
  return apiFetch<ReportTemplate[]>("/api/v1/reports/templates");
}

export async function createFromTemplate(t: ReportTemplate): Promise<ReportSummary> {
  return createReport({
    name: t.name,
    visibility: "private",
    layout_json: t.layout_json,
    canvas_filters_json: t.canvas_filters_json,
  });
}
```

- [ ] **Step 3: Type-check**

Run: `docker compose exec -T frontend npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/lib/reports/types.ts frontend/lib/reports/api.ts
git commit -m "feat(reports): template types + api client"
```

---

### Task 6: Templates section + "Use template" on the list page

**Files:**
- Modify: `frontend/app/reports/page.tsx`
- Test: `frontend/tests/app/reports-templates.test.tsx`

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/tests/app/reports-templates.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import ReportsListPage from "@/app/reports/page";

vi.mock("@/components/AppShell", () => ({ default: ({ children }: any) => <div data-testid="app-shell">{children}</div> }));
const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: vi.fn() }) }));
vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: () => ({ user: { id: 1 }, loading: false, featureReportsV2: true }) }));
vi.mock("@/lib/reports/api", () => ({
  listReports: vi.fn().mockResolvedValue([]),
  listTemplates: vi.fn().mockResolvedValue([
    { key: "monthly_review", name: "Monthly review", description: "x", layout_json: { version: 1, widgets: [] }, canvas_filters_json: {} },
  ]),
  createFromTemplate: vi.fn().mockResolvedValue({ id: 42 }),
  createReport: vi.fn(),
}));

test("shows templates and instantiates on click", async () => {
  render(<ReportsListPage />);
  expect(await screen.findByText("Monthly review")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /use template/i }));
  await waitFor(() => expect(push).toHaveBeenCalledWith("/reports/42"));
});
```

- [ ] **Step 2: Run to confirm failure**

Run: `docker compose exec -T frontend npm test -- tests/app/reports-templates.test.tsx`
Expected: FAIL (no "Monthly review" / no "Use template" button yet).

- [ ] **Step 3: Implement the Templates section**

In `frontend/app/reports/page.tsx`: load templates in the existing effect (`listTemplates().then(setTemplates)`), add a `templates` state, render a **Templates** section above the reports list with a card per template (name, description, a "Use template" button). The button calls `createFromTemplate(t)` then `router.push(\`/reports/${created.id}\`)`. Also update the empty-state copy to add a "Start from a template" CTA that scrolls to / highlights the templates section. Match the existing Tailwind tokens used on the page (`bg-surface`, `border-border`, `text-text-*`, `bg-accent`).

- [ ] **Step 4: Run the test**

Run: `docker compose exec -T frontend npm test -- tests/app/reports-templates.test.tsx`
Expected: PASS.

- [ ] **Step 5: Type-check + commit**

```bash
docker compose exec -T frontend npx tsc --noEmit
git add frontend/app/reports/page.tsx frontend/tests/app/reports-templates.test.tsx
git commit -m "feat(reports): templates section + use-template on list page"
```

---

### Task 7: End-to-end manual verification

- [ ] **Step 1: Verify in the running app**

Log in (`demo`/`demo1234`) at `http://localhost`, open Reports, confirm the 3 templates render. Click "Use template" on each of the three; confirm the canvas opens and **every widget renders data** (the seed has 94 transactions). If any widget is blank, the fixture's `config` shape is wrong — fix the fixture in `backend/app/reports/templates/__init__.py` against `types.ts` and re-verify. This is the acceptance gate for the slice.

- [ ] **Step 2: Run the full reports test suites**

Run: `docker compose exec -T backend pytest backend/tests/routers/test_reports*.py -v`
Run: `docker compose exec -T frontend npm test -- tests/app/reports`
Expected: all PASS.

- [ ] **Step 3: Update codebase shape doc**

Append the new `backend/app/reports/templates/` module + `GET /reports/templates` to `~/.claude/projects/-Users-flamarion-src-tbd/codebase_shape.md`.

---

## Self-review notes

- **Spec coverage:** templates (fixtures + endpoint + UI), one-click instantiate via existing create endpoint, empty-state CTA, creation snapshot columns (enables Slice 2 revert). All covered.
- **Out of this slice (Slice 2):** ConfigRail tiering, tooltips, save toast, streamlined add flow, edit/cancel-editing, the `POST /{id}/reset` revert action/UI.
- **Risk:** template `config` shape drift vs `types.ts` → blank widgets. Task 7 Step 1 is the explicit guard.
