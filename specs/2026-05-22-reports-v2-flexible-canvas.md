# Reports v2 — flexible drag-and-drop canvas — design

**Status:** draft, ready for architect review 2026-05-22.
**Date:** 2026-05-22.
**Replaces:** the previously scoped fixed report pages (spending breakdown master/sub, income vs expense trends, monthly comparison, per-account cycle-aware views). Pre-launch, hard cut. No backcompat shims.

## Source

Product owner request 2026-05-22 (today). Wants users to assemble their own reports by dragging chart widgets onto a canvas, configuring each widget with filters and dimensions, and crossing all financial data (transactions, accounts, categories, tags, budgets, recurring, forecast, FX, dates).

Inspirations studied (not copied): Looker Studio, Metabase questions and dashboards, Notion databases, Grafana panels.

## Goal

Ship a Reports surface where any authed user (in their org) can:

1. Open a Reports page, see a list of their saved reports + org-shared reports + 3 preinstalled defaults.
2. Click "New report" and land on an empty canvas.
3. Drag a widget type (KPI card, bar chart, line, stacked bar, pie, area, sparkline, table) from a catalog onto the canvas.
4. Configure each widget — pick a measure, one or two dimensions, optional filters.
5. Optionally set canvas-wide filters (date range, accounts, categories, tags) that cascade into widgets.
6. Save, name, optionally share to org (read-only for other org members).
7. Reload, edit, duplicate, delete.

## Architecture summary (recommended path)

* **Canvas:** `react-grid-layout` (12-column responsive grid, snap-to-grid, drag, resize). Mobile (`< sm`) collapses to a single-column stack by `(y, x)`.
* **Data API:** ONE endpoint `POST /api/v1/reports/query` driven by a Pydantic-validated AST. Strict whitelist of measures, dimensions, filter operators. Server compiles AST to SQLAlchemy Core expressions with bound params. Never executes user-provided SQL.
* **Storage:** new `reports` table holds `{id, org_id, owner_user_id, name, description, visibility, layout_json, canvas_filters_json, ...}`. Layout JSON is a versioned schema.
* **Filter scope:** canvas-wide cascade with per-widget override. Per widget, an explicit field on the widget filter overrides the canvas filter on that field; absent fields inherit.
* **Permissions:** any org member can create reports. Owner + org admins can edit; other org members can view if `visibility = 'org'`. No public links in v1.
* **Aggregation engine:** live SQL queries in v1. Performance budget: widget p95 < 400 ms with 10k transactions. No materialized rollups, no Redis caching yet. Defer until measured. (Index plan below ensures the budget is achievable; revisit only if missed.)
* **Navigation:** `/reports` lands as a new top-level frame menu item between Forecast Plans and Categories. No umbrella parent, no tabs. Forecast Plans keeps its existing icon and path; AI Tier and Plans stay separate when they exist.

The rest of this document drills each axis.

## 1. Canvas UX shape

### Recommendation: react-grid-layout (12-col responsive grid)

* 12-column horizontal grid, row height ~ 60 px, vertical scroll.
* Drag from a left-rail widget palette onto the canvas. Drop snaps to grid cells.
* Resize via bottom-right corner handle. Min width 2 cols (KPI card), default 4 cols (bar / line). Min height 2 rows.
* Multi-row scroll: canvas grows downward, no horizontal scroll.
* "Edit mode" toggle in the report header. View mode = no drag handles, no chrome, just charts.
* Mobile fallback (`<sm`, ~640 px): single-column stack ordered by widget `(y, x)`. Resize handles hidden. Read-only on mobile in v1; editing is desktop-only.

### Rejected alternatives

* **Free-form xy positioning (Figma-style).** Way over budget pre-launch. Collision logic, alignment guides, copy-paste, snap helpers all need building. Mobile projection is harder because there is no implicit row structure to flatten.
* **Notion-style sections (vertical stack of horizontal widget bands).** Too rigid for "cross all data." Users want side-by-side comparisons (KPI row of 4, then 2 charts side-by-side, then a wide table). A grid does that natively.

### Library choice

`@dnd-kit/core` is already in `package.json`, but it is a generic DnD primitive; building a snap-grid on top is hundreds of lines. **Add `react-grid-layout`** (a tiny, mature, SSR-friendly dep purpose-built for this). It uses native HTML5 DnD under the hood and integrates cleanly with React 19 and Next 15 (client component). `@dnd-kit` stays in the bundle for transaction-tag chip drag and other ad-hoc DnD, not removed.

### ASCII mockup (desktop edit mode)

```
+--------------------------------------------------------------+
|  Reports / Monthly Review              [ View | Edit ]  Save |
+----------+---------------------------------------------------+
| Widgets  | [ Canvas filters: Jan 1 to Jan 31, 2026  Accts: All ] |
|          +---------------------------------------------------+
| KPI      | +-----+ +-----+ +-----+ +-----+                  |
| Bar      | | KPI | | KPI | | KPI | | KPI |                  |
| Line     | +-----+ +-----+ +-----+ +-----+                  |
| Stacked  | +-------------------+ +-------------------+      |
| Pie      | | Bar: spend by cat | | Line: income mo   |      |
| Area     | +-------------------+ +-------------------+      |
| Spark    | +-------------------------------------------+    |
| Table    | | Table: top 20 txns                        |    |
|          | +-------------------------------------------+    |
+----------+---------------------------------------------------+
```

## 2. Widget catalog v1

### Ship in v1

| Widget | Why ship |
|---|---|
| **KPI card** | Single-number with delta. Cheapest possible widget and the highest-traffic primitive. |
| **Bar (vertical)** | Categorical comparison. Bread and butter. |
| **Stacked bar** | Period-over-period breakdown by sub-dimension. |
| **Line** | Time-series. Required for "income vs expense trend." |
| **Area** | Same data as line, different visual register. Tiny addition over line. |
| **Pie / donut** | Share-of-total. Users expect it. We restrict to <= 8 slices auto-grouping the long tail into "Other." |
| **Sparkline** | Inline trend inside KPI-ish surfaces. Free once Line is built. |
| **Table** | Pivot-style or row-list. Configurable columns + sort + 50-row cap. |

### Defer to v2+

| Widget | Why defer |
|---|---|
| **Sankey** | Custom layout algorithm or heavy dep (`react-sankey`). Niche. Implementation cost outweighs v1 value. |
| **Treemap** | Visually appealing but hard to read at small sizes; not a typical finance-app primitive. |
| **Gauge** | Sub-case of KPI. Defer until users ask. |
| **Scatter** | Two-dimensional numeric for finance data is rare (price vs volume, etc.). Defer. |
| **Map / geo** | No location dimension in current schema. |

### Cut-line reasoning

V1 must cover: "show me a number," "show me a trend over time," "show me a breakdown by category," "show me share-of-total," "show me a list of rows." The eight v1 widgets cover all five. Deferred widgets are nice-to-have visual variety, not new analytical capability.

## 3. Widget configuration UX

### Where the config lives

**Right rail, slide-in.** When a widget is selected in edit mode, a 320-px right rail slides in with config. View mode hides it.

* Modal would block view of the canvas (defeats the iterate-and-see loop).
* Inline (under each widget) gets noisy with eight selected widgets.
* Right rail keeps focus on one widget at a time and gives stable real estate.

### Config sections (per widget)

```
[ Right rail ]
Widget name       [ Spend by category, this month ]
Chart type        [ Bar  v ]

Data source       [ Transactions  v ]    <-- (dataset selector)

Measure           [ Sum of amount  v ]
   aggregation: sum | count | avg | distinct

Dimensions (X axis)
  Primary         [ Category  v ]
  Secondary       [ none  v ]    <-- (enables stacked / grouped)

Filters (this widget)
  + Add filter
   Date range:   [ inherits canvas / override ]
   Category:     [ Food, Transport, ... ]
   Tag:          [ ... ]
   Account:      [ ... ]
   Amount range: [ min ] [ max ]
   Txn type:     [ expense ]

Display
  Sort:        [ value desc ]
  Limit:       [ 10 ]
  Format:      [ currency / number / percent ]
```

### Filter primitives v1

| Filter | Type | UI |
|---|---|---|
| Date range | absolute (start, end) OR relative (last N days / months / this month / last month / YTD) | preset chips + custom date pickers |
| Category | multi-select with hierarchy aware (selecting a master selects all subs) | tree picker reusing existing `<CategoryPicker>` |
| Tag | multi-select with `tag_match=all\|any` semantics (default `all`), mirroring the transactions list (`backend/app/routers/transactions.py:90`, service at `backend/app/services/transaction_service.py:1697`) | chip picker |
| Account | multi-select | chip picker |
| Amount range | min / max numeric | two number inputs |
| Transaction type | enum: income, expense, transfer | radio group |
| Status | enum: settled, pending | radio group |
| Recurring source | bool: only recurring / only one-off / both | radio group |

### Aggregation picker

Per measure: `sum`, `count`, `avg`, `min`, `max`, `count_distinct`. The allowed aggregations are bound to the measure (e.g. `count` is valid on any dataset; `sum` is valid on `amount`; `count_distinct` on a column the AST whitelists, e.g. `category_id`, `account_id`).

## 4. Filter scope

### Recommendation: hybrid (canvas-wide cascades, per-widget overrides)

* Each report carries `canvas_filters` (a filter object stored on the report).
* Each widget carries `widget_filters` (a filter object stored on the widget config).
* Resolved filter at query time = `canvas_filters` merged with `widget_filters`, where any field present in `widget_filters` overrides the same field in `canvas_filters`.
* UI signal: when a widget's filter overrides a canvas filter on the same field, the right rail shows an "Overrides canvas" pill on that field.

### Why not per-widget only

Forces 8 widgets x N filters worth of duplicate setup. Painful for "this report is about January."

### Why not canvas-wide only

Breaks the "two perspectives in one report" use case: "show 2026 YTD totals (canvas date) and last-30-days categories (widget overrides date)."

## 5. Save, share, load

### Storage

New `reports` table:

```sql
CREATE TABLE reports (
    id INT NOT NULL AUTO_INCREMENT,
    org_id INT NOT NULL,
    owner_user_id INT NOT NULL,
    name VARCHAR(200) NOT NULL,
    description VARCHAR(500) NULL,
    visibility ENUM('private', 'org') NOT NULL DEFAULT 'private',
    layout_json JSON NOT NULL,
    canvas_filters_json JSON NOT NULL,
    schema_version INT NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY ix_reports_org (org_id),
    KEY ix_reports_owner (owner_user_id),
    CONSTRAINT fk_reports_org FOREIGN KEY (org_id) REFERENCES organizations (id) ON DELETE CASCADE,
    CONSTRAINT fk_reports_owner FOREIGN KEY (owner_user_id) REFERENCES users (id) ON DELETE CASCADE
);
```

Default reports (preinstalled): we DO NOT seed rows per organization. Instead a small set of canonical layouts ship as JSON fixtures registered in `backend/app/reports/templates/__init__.py` and appear in the "Templates" tab of the reports list. "Use template" creates a new private `reports` row owned by the calling user. This avoids dead seed rows and lets templates evolve in code without data migrations.

Templates v1 (three):

1. **Monthly review** — KPIs (net, income, expense), bar of spend by category, line of income vs expense this month.
2. **Cash flow trend** — line of net by month for the trailing 12 months, KPI of trailing-12-month average net.
3. **Category deep-dive** — pie of category share this month, table of top 20 transactions, stacked bar of category by month.

### Layout JSON schema (v1)

```jsonc
{
  "version": 1,
  "widgets": [
    {
      "id": "w_01",                     // local stable id, uuidv4 client-generated
      "type": "bar",                    // kpi | bar | stacked_bar | line | area | pie | sparkline | table
      "title": "Spend by category",
      "grid": { "x": 0, "y": 0, "w": 6, "h": 4 },
      "config": {
        "dataset": "transactions",
        "measure": { "agg": "sum", "field": "amount" },
        "dimensions": ["category"],     // 1 or 2 entries
        "filters": {                    // widget-level filters; merged over canvas
          "txn_type": ["expense"]
        },
        "sort": { "by": "value", "dir": "desc" },
        "limit": 10,
        "format": "currency"
      }
    }
    // ...
  ]
}
```

Canvas filters live in their own column (`canvas_filters_json`) to keep the layout pure layout. Schema:

```jsonc
{
  "date_range": { "kind": "relative", "preset": "last_30_days" },
  "accounts": [12, 14],
  "categories": [],
  "tags": []
}
```

### Visibility model

* `private`: only owner can see. Owner can read / edit / delete.
* `org`: any org member can read; only owner + org admins (`owner`, `admin` roles) can edit. Anyone with edit rights can change visibility.
* No public links in v1. (Rejected: adds CSRF, sharing-link rotation, abuse-of-link-as-data-export surface. Not worth pre-launch.)

### List page UX

`/reports` shows three sections:

1. **Yours** — reports `owner_user_id = me`.
2. **Shared by your org** — reports where `visibility = 'org' AND org_id = my_org AND owner_user_id != me`.
3. **Templates** — code-shipped layouts; click to instantiate as a new private report.

Search by name, sort by `updated_at desc` by default.

## 6. Backend data API

### Endpoint shape (recommended)

**One endpoint:** `POST /api/v1/reports/query`. Body is a query AST. Returns rows + a metadata block.

### Why a single AST endpoint, not many narrow endpoints

Many narrow endpoints (`/spending-by-category`, `/income-trend`) cannot express the "cross all data" requirement without combinatorial expansion. Every new widget shape would need a new endpoint. The AST contract makes the backend a constrained query compiler; the frontend (and any future BI tool we expose) builds AST objects without server changes.

### Why not a "datasets + client-side filter" hybrid

Sending raw transactions to the client to filter client-side leaks row-level data on every widget, breaks at scale (10k+ transactions), and gives no way to honor measure caps without first hitting the network. Rejected.

### Security model — this is the load-bearing section

The AST is a closed, enum-driven Pydantic structure. It cannot describe SQL. The server compiles it to SQLAlchemy Core expressions using bound parameters. **At no point is a user-supplied string concatenated into SQL.**

```python
# backend/app/schemas/reports_query.py (sketch)

from enum import Enum
from pydantic import BaseModel, Field, conint

class Dataset(str, Enum):
    TRANSACTIONS = "transactions"     # v1: only this dataset
    # v2: BUDGETS, RECURRING, FORECAST

class Measure(BaseModel):
    agg: Literal["sum", "count", "avg", "min", "max", "count_distinct"]
    field: Literal["amount", "id", "category_id", "account_id"]

class Dimension(str, Enum):
    CATEGORY = "category"
    CATEGORY_MASTER = "category_master"
    ACCOUNT = "account"
    TAG = "tag"
    TXN_TYPE = "txn_type"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"

class FilterOp(str, Enum):
    EQ = "eq"
    IN = "in"
    BETWEEN = "between"   # for dates and amounts only
    GTE = "gte"
    LTE = "lte"

class Filter(BaseModel):
    field: Literal[
        "date", "amount", "category_id", "account_id",
        "tag_name", "txn_type", "status", "is_recurring"
    ]
    op: FilterOp
    value: Any   # validator narrows by field+op

class ReportsQuery(BaseModel):
    dataset: Dataset
    measure: Measure
    dimensions: list[Dimension] = Field(max_length=2)
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    sort: SortSpec | None = None
    limit: conint(ge=1, le=500) = 100
```

Compilation:

```python
# backend/app/services/reports_query_service.py (sketch)

DIMENSION_TO_COLUMN = {
    Dimension.CATEGORY: Category.name,
    Dimension.MONTH: func.date_format(Transaction.date, "%Y-%m"),
    # ...
}

FILTER_FIELD_TO_COLUMN = {
    "date": Transaction.date,
    "amount": Transaction.amount,
    "category_id": Transaction.category_id,
    # ...
}

async def execute_query(db, org_id: int, q: ReportsQuery) -> QueryResult:
    stmt = select(...).where(Transaction.org_id == org_id)   # org_id is ALWAYS appended, server-side
    for f in q.filters:
        col = FILTER_FIELD_TO_COLUMN[f.field]   # whitelist lookup; KeyError -> 400
        stmt = stmt.where(_compile_filter(col, f.op, f.value))   # bound params
    # ... group_by from dimensions, agg from measure
    stmt = stmt.limit(min(q.limit, 500))
    rows = (await db.execute(stmt)).all()
    return QueryResult(rows=rows, meta={"row_count": len(rows), "truncated": len(rows) == 500})
```

Hard caps enforced server-side:

* `limit <= 500` — even if AST tries 5000.
* `date BETWEEN` window <= 5 years.
* `filters` list <= 20.
* `dimensions` list <= 2.
* AST request body <= 8 KB.
* Per-request query timeout 5 s (SQLAlchemy `execution_options(timeout=...)` where supported; otherwise statement-level `MAX_EXECUTION_TIME` hint on MySQL).
* Rate limit: 60 queries / minute / user (slowapi, same substrate as existing rate-limited endpoints).

`org_id` is taken from `get_current_user().org_id` and appended to the `WHERE` clause inside `execute_query`. The AST has no way to express `org_id` or `user_id` — those fields are not in the filter field whitelist. This is the bright line that keeps a widget config from ever reading another org's data.

### Response shape

```jsonc
{
  "rows": [
    { "category": "Food", "value": 412.50 },
    { "category": "Transport", "value": 198.00 }
  ],
  "meta": {
    "row_count": 2,
    "truncated": false,
    "query_ms": 87
  }
}
```

### Reports CRUD endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/reports` | authed | list reports visible to this user (own + org-shared) |
| `GET` | `/api/v1/reports/{id}` | authed, must be visible | get one |
| `POST` | `/api/v1/reports` | authed | create. Body: `{name, description?, visibility, layout_json, canvas_filters_json}` |
| `PATCH` | `/api/v1/reports/{id}` | authed, must be editable | update |
| `DELETE` | `/api/v1/reports/{id}` | authed, must be editable | hard delete |
| `POST` | `/api/v1/reports/{id}/duplicate` | authed, must be visible | clone as a new private report owned by me |
| `POST` | `/api/v1/reports/query` | authed | execute an AST against org data |
| `GET` | `/api/v1/reports/templates` | authed | static fixture list (returns from code, not DB) |

## 7. Aggregation engine + performance

### v1: live SQL queries, no caching

Reasoning: pre-launch dataset is small (every test org has tens to thousands of transactions). Materialized rollups add invalidation complexity (every transaction insert / update / delete fires invalidation). Redis caching only pays off above a certain QPS.

Performance budget:

* **p95 single-widget query < 400 ms** with 10k transactions in the table for that org.
* **p95 full report (8 widgets) < 1.5 s wall clock** with parallel client-side requests.

If we miss the budget post-launch, add (in order):

1. Composite indexes (see below).
2. Per-query Redis cache keyed by `(org_id, hash(AST))` with 60 s TTL. Invalidate on transaction write for that org.
3. Materialized monthly rollup table `transaction_aggregates_monthly` populated by a background job.

### Index plan (ships in PR 1)

The query patterns are predictable. We add:

```sql
-- Most queries filter org_id + date range, group by category or month.
CREATE INDEX ix_transactions_org_date ON transactions (org_id, date);
CREATE INDEX ix_transactions_org_category_date ON transactions (org_id, category_id, date);
CREATE INDEX ix_transactions_org_account_date ON transactions (org_id, account_id, date);
```

(Verify each is not already present before adding; existing migrations may already cover the first one. The reports-router migration only adds what is missing.)

### Frontend fetching

Each widget owns its own SWR key `(report_id, widget_id, hash(resolvedQuery))`. Widget mounts -> fires query in parallel with other widgets. Canvas filter change -> re-fires every widget that doesn't override that field. Failure of one widget shows an inline error inside that widget and does not block the others.

## 8. Permissions model

* **Create a report:** any authed org member.
* **View a report:** owner always; org members if `visibility = 'org'` and they belong to the same org.
* **Edit / delete a report:** owner always; org `owner` or `admin` role can edit any org-shared report in their org (this matches how org-shared content behaves elsewhere). Other org members cannot edit even if they can view.
* **Change visibility:** anyone who can edit.
* **Duplicate:** anyone who can view. Duplicate is always created as `private`, owned by the duplicator.
* **Templates:** read-only fixtures shipped in code. Instantiate creates a normal report.

### Owner-change, member-removal, user-delete, org-delete semantics

`reports.owner_user_id` does NOT cascade-delete on `users` delete from the user's perspective. Concrete rules:

* **Org-owner change** (org transfers from user A to user B). Org-shared reports (`visibility = 'org'`) authored by the previous owner stay attached to the org and become owned by the new owner. The previous owner's private reports follow the same rule as any other removed member (see below).
* **Member removal from an org** (member M leaves or is removed). Org-shared reports authored by M transfer ownership to the org owner. Private reports authored by M are hard-deleted. Handled in the existing org-member-removal service; mirrors how budgets and forecast plans treat ownership on member removal.
* **System-level user delete** (`DELETE /api/v1/admin/users/{user_id}` in `backend/app/routers/admin_users.py`). Private reports authored by the deleted user are hard-deleted. Org-shared reports authored by the deleted user transfer ownership to the org owner.
* **Org delete.** Out of scope. Organizations are not deleted today, so there is no cascade rule to define.

## 9. Migration (pre-launch hard cut)

* **No legacy report routes to preserve.** The old fixed-reports surface was scoped but never shipped. Nothing to remove.
* **Single Alembic migration** in PR 1 creates `reports`, adds composite indexes on `transactions`.
* **No data backfill.** Existing organizations see "No reports yet. Start from a template."
* **`.do/app.yaml`** unchanged; no new env vars.

## 10. Navigation — Reports as a top-level item

Reports is a **new top-level frame menu item**. No umbrella parent ("Planning", "Insights", etc.), no tabs. Forecast Plans keeps its existing icon and path; this wave does not repath `/forecast-plans` or swap any other menu icons.

**Order in the frame menu** (`frontend/components/AppShell.tsx:55`):

```
Dashboard
Transactions
Accounts
Recurring
Budgets
Forecast Plans
Reports          <-- new in this spec
Categories
```

AI Tier, when its spec ships, lands as its own separate top-level item above Reports (between Forecast Plans and Reports). That decision is locked in the "Decisions locked" section.

### Rejected alternative

* **Tabs under one parent.** Hides first-class surfaces behind a parent click. Reports (exploratory data viz), Plans (scenario modeling), and AI Tier (ML-generated insights) have distinct mental models. Forcing them into tabs implies they are facets of one thing; they are not.

## 11. Phased rollout

Four PRs. PR 1 is days, not weeks. Each PR is independently mergeable and shippable behind a `FEATURE_REPORTS_V2` env flag until PR 4 lights it up.

### PR 1 — Backend AST + query engine (3 to 5 days)

Lands first. No frontend changes visible to the user.

* `backend/app/schemas/reports_query.py` — Pydantic AST.
* `backend/app/services/reports_query_service.py` — AST → SQLAlchemy Core compiler.
* `backend/app/routers/reports.py` — `POST /api/v1/reports/query` plus a skeleton CRUD that returns 501 for now.
* Alembic migration: new `reports` table + composite indexes.
* Tests:
  * AST validation rejects unknown dimensions, unknown filter fields, oversize bodies, out-of-range limits.
  * Compiler: org_id always appended; bound params used (introspect compiled SQL); whitelist enforced.
  * Integration: seed 10k synthetic transactions, run each measure + dimension combination, assert correctness + p95 latency.
  * Security harness: attempt SQL-injection-like values inside filter `value` field; all are rejected by Pydantic or bound-param-escaped (cannot reach SQL parse).
* Env flag `FEATURE_REPORTS_V2=false` default. When the flag is off, ALL `/api/v1/reports/*` routes return 404 via a router-level dependency that raises `HTTPException(404)`, and the router is excluded from OpenAPI. The frontend additionally hides the nav item and routes when the corresponding `NEXT_PUBLIC_FEATURE_REPORTS_V2` flag is off. Both sides gate together; the backend gate is the load-bearing one.
* LOC estimate: ~1,200 (split: 600 service + 200 schemas + 100 router + 300 tests).

### PR 2 — Canvas substrate + 2 widget types (5 to 7 days)

* `frontend/app/reports/page.tsx` — list page (Yours / Org / Templates tabs).
* `frontend/app/reports/[id]/page.tsx` — single-report canvas.
* `frontend/components/reports/Canvas.tsx` — react-grid-layout wrapper.
* `frontend/components/reports/WidgetShell.tsx` — drag handle, title, error boundary.
* `frontend/components/reports/widgets/KpiCard.tsx`.
* `frontend/components/reports/widgets/BarChart.tsx`.
* `frontend/components/reports/ConfigRail.tsx` — right rail, minimal config for these two widget types.
* `frontend/lib/reports/types.ts` — TS types matching the layout JSON schema.
* `frontend/lib/reports/api.ts` — typed client for `/reports` CRUD + `/reports/query`.
* Save / load layout JSON. Edit / view mode toggle.
* Add `react-grid-layout` to deps.
* LOC estimate: ~1,500.

### PR 3 — Full widget catalog + filter primitives (5 to 7 days)

* Remaining widgets: `LineChart`, `StackedBar`, `Pie`, `Area`, `Sparkline`, `Table`.
* Filter primitive components: `DateRangeFilter`, `CategoryFilter` (reuses existing picker), `TagFilter`, `AccountFilter`, `AmountRangeFilter`, `TxnTypeFilter`.
* Canvas-wide filter bar at top of canvas.
* Per-widget filter override UI with "Overrides canvas" pill.
* LOC estimate: ~1,500.

### PR 4 — Sharing, templates, navigation, mobile polish (3 to 5 days)

* Visibility toggle in report header (private / org).
* Permission gates wired in CRUD endpoints.
* Templates fixtures + "Use template" button.
* Mobile responsive: single-column stack on `<sm`; read-only.
* Add `Reports` to `AppShell` frame menu (Forecast Plans keeps its current icon and path).
* Flip `FEATURE_REPORTS_V2` default to `true` in `.env.example` (still gated in prod via `.do/app.yaml` until owner signs off).
* CSV export per widget (one button on each widget in view mode; returns the same rows the widget rendered). Uses the same Bearer / cookie session auth as the rest of `/api/v1`; no separate one-shot signed-URL or token machinery.
* LOC estimate: ~800.

### Total

* PRs: 4 (down from the "4 to 6" sketch in the prompt — three of the dimensions collapse into single PRs cleanly).
* LOC: ~5,000 across backend + frontend + tests. ~1,200 backend, ~3,000 frontend, ~800 tests.
* Time: ~3 to 4 weeks single-developer, less if PRs 2 and 3 can run in parallel after PR 1.

## Naming + cross-references

* Backend: `backend/app/models/report.py`, `backend/app/schemas/reports_query.py` + `report.py`, `backend/app/services/reports_query_service.py`, `backend/app/routers/reports.py`.
* Frontend: `frontend/app/reports/`, `frontend/components/reports/`, `frontend/lib/reports/`.
* `[[specs/configurable-dashboard-widgets.md]]` — adjacent concept (configurable Dashboard cards). The two share the philosophy "user-chosen layout" but diverge on substrate: Dashboard widgets are pre-built tiles with fixed queries; Reports widgets are user-composed AST queries. They should NOT share components in v1; revisit in v2 if the Dashboard widget framework lands.
* `[[reference_do_spec_sync.md]]` — no new env vars in PRs 1 to 3. PR 4 adds `FEATURE_REPORTS_V2` to `.env.example` and (when lit) to `.do/app.yaml`.

## Decisions locked (architect, 2026-05-22)

1. **`FEATURE_REPORTS_V2` flag scope.** Backend route-disable AND frontend hide. ALL `/api/v1/reports/*` routes return 404 when `FEATURE_REPORTS_V2=false` via a router-level dependency that raises `HTTPException(404)`; the frontend hides the nav item and routes too. Route-disable hardens the surface during the feature-flag window so partial-rollout reports can't be queried via dev tools.
2. **Templates are code fixtures, not DB seed rows.** `backend/app/reports/templates/__init__.py` registers each template; "use template" creates a new private report owned by the calling user. No per-org seed, no data migration.
3. **CSV export auth.** Normal Bearer / session auth, same as the rest of `/api/v1`. No separate one-shot signed URL or token machinery.
4. **Tag filter `all` vs `any`.** Mirrors the existing transactions list semantics: query param `tag_match=all|any`, default `all`. Reference implementation: `backend/app/routers/transactions.py:90` (router) and `backend/app/services/transaction_service.py:1697` (service).
5. **AI Tier nav placement.** AI Tier is a separate top-level frame-menu item, placed above Reports (between Forecast Plans and Reports) when its spec ships. AI Tier is consumption ("here are insights for you"); Reports is authoring.

## Open questions for architect

(none — all five open questions resolved above.)

## Out of scope (v1)

* **Public read-only share links.** Adds auth-bypass surface; defer until an org asks.
* **Real-time refresh** (SSE / websocket). User reloads or revisits.
* **Cross-org reports** (operator wants to compare across organizations). Superadmin tool, not a v1 user feature.
* **Drill-down** (clicking a pie slice to filter the rest of the report). Nice-to-have v2.
* **Saved widget presets** (reusable widget configs across reports). v2.
* **Forecast / budget / recurring datasets.** v1 dataset is `transactions` only. v2 adds these (the AST already has room via the `dataset` enum).
* **Sankey / treemap / gauge / scatter widgets.** See section 2.
* **Materialized rollups or Redis cache.** Live SQL is fine for v1 volume. Revisit only if the p95 budget is missed.
* **In-canvas annotations / text blocks.** Reports v1 is charts only. Notes / markdown blocks are a v2 widget type.
* **Mobile editing.** v1 is read-only on mobile. Editing on a 6-inch screen with drag-and-drop is unpleasant; defer until users ask.
* **i18n.** v1 ships in English. Reports name + description are user-authored so they carry whatever language the user types.
* **Audit logging of report changes.** Reports are user content, not sensitive admin actions. The `[[specs/2026-05-21-notification-system-sensitive-ops.md]]` substrate stays focused on sensitive ops; reports CRUD does not trigger notifications or audit events.
