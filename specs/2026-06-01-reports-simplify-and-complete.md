# Reports — make it usable (templates + builder simplification), then complete PR4

**Status:** draft for review, 2026-06-01.
**Builds on:** `specs/2026-05-22-reports-v2-flexible-canvas.md` (architect-locked). PR1-PR3 of that train shipped (#343, #350, #352): backend AST query engine, canvas, all 8 widgets, filter primitives. The surface is now enabled locally (`FEATURE_REPORTS_V2=true`).

## Motivation

The owner could not generate a single report. A read-only investigation found **no blocking bugs** — the problem is **UX friction**:

- "New report" drops the user on a blank canvas. Editor empty state (`app/reports/[id]/page.tsx:448`) just says "No widgets yet. Click Add widget to start." The list-page empty state is separate (`app/reports/page.tsx:118-129`).
- Adding a widget interposes a picker modal with 8 jargon-labeled types, no default/preview/recommendation (`components/reports/WidgetPicker.tsx:46-115`). **Note:** once a type is chosen, the inserted widget is already fully configured and renders immediately (`emptyBar()` etc., `app/reports/[id]/page.tsx:63-180`) — so the friction is the picker step, NOT an empty/unconfigured widget.
- The config rail shows 15+ controls at once — aggregation, measure, dimension, and 6 filter sub-sections (`components/reports/ConfigRail.tsx:218-374`) — using terms like "distinct count" and "master category" with no explanation.
- "No data" and "Saved" give no actionable feedback.

The single highest-impact fix is the one PR4 piece never built: **templates** (one click -> a working report). Estimated to remove ~80% of the pain.

## Plan (priority-ordered; lowest effort / highest impact first)

### Slice 1 — Templates (the core fix)

- **Backend:** `backend/app/reports/templates/__init__.py` registers 3 code fixtures (architect lock #2 — code fixtures, NOT DB seed rows): **Monthly review**, **Cash flow trend**, **Category deep-dive**. **Author the fixtures against the implemented frontend `lib/reports/types.ts` shape** (`config.measure` for single-measure widgets, `config.measures[]` for line/area/stacked_bar/table, dimensions from the `Dimension` union, filters as `WidgetFilters` like `{txn_type:"expense", date_range:{...}}`) — **NOT** the raw AST `{field,op,value}` sketch in the parent spec §5, which predates the `WidgetFilters` indirection and would not render. `GET /api/v1/reports/templates` returns them (correctly gated by the existing `require_reports_v2_enabled` 404 dependency since it mounts on the same router).
- **Instantiate:** "Use template" reuses the **existing** `createReport` endpoint (`api.ts:30-38` accepts `layout_json`+`canvas_filters_json`+`visibility`) to create a new **private** report from the template. No new instantiate endpoint needed — only `GET /reports/templates` is new.
- **Frontend:** a **Templates** section on `/reports`; cards show name + description; "Use template" -> instantiate -> open canvas with charts already rendering against the user's data. Empty state becomes "Start from a template" + "Build from scratch".
- **Capture the creation snapshot (enables "Revert to original" in Slice 2).** Migration: add nullable `original_layout_json` + `original_canvas_filters_json` to `reports`, set **once at create** and never overwritten on edit. Blank create snapshots the empty canvas; template instantiation snapshots the template's layout. Backfill existing rows with their current values (feature gated off in prod, so the table is effectively empty). Captured here in Slice 1 because this is the create path we're already touching.

### Slice 2 — Builder simplification + editing-lifecycle controls

- **Streamline the add flow:** the inserted widget is already pre-configured and renders (see Motivation), so the work is to **skip/streamline the picker modal** — e.g. a one-click "Add chart" that inserts the default Bar immediately, with type-switching available in the rail afterward. (Re-scoped from "add defaults" — defaults already exist.)
- **Empty-state CTA:** hero copy explaining what you can build + buttons to templates / add-first-widget. Editor empty state at `app/reports/[id]/page.tsx:448`; list-page empty state at `app/reports/page.tsx:118-129`.
- **ConfigRail tiering:** group **Basic** (chart type, measure, dimension) always visible; collapse **Filters** + **Advanced** (sort, limit, format) into a disclosure, closed by default (`ConfigRail.tsx:218-374`).
- **Tooltips on jargon:** reuse `HelpTooltip` / `lib/help/tooltips.ts` (has a master-category entry already; **add new keys for aggregation types** `sum`/`count`/`avg`/`distinct`) for aggregation, master category, dimensions.
- **Actionable empty/no-data + save feedback:** "No data" widget message suggests adjusting filters / dimension / period (`BarWidget.tsx:66` and siblings); a 2-second success toast on save.

**Editing-lifecycle controls (requested 2026-06-01):**

- **Edit** — a clear, explicit "Edit" affordance to enter edit mode on a saved report. The view/edit toggle already exists in `app/reports/[id]/page.tsx`; surface it prominently (today it's easy to miss).
- **Cancel editing** — a "Cancel" / "Discard changes" control in edit mode that drops the current unsaved session and re-loads the **last-saved** report state from the server. Frontend-only (re-fetch + reset local layout/filter state); no backend change. Should no-op or be hidden when there are no unsaved changes.
- **Revert to original** — reset the report to its **as-created** state (undo every edit since creation), using the Slice-1 snapshot. Backend: `POST /api/v1/reports/{id}/reset` copies `original_layout_json`/`original_canvas_filters_json` -> live `layout_json`/`canvas_filters_json`; gated by edit rights (owner always; org owner/admin for org-shared). Frontend: a "Revert to original" button behind a **typed/confirm modal** (it permanently discards customizations). Distinct from "Cancel editing": Cancel drops one unsaved session; Revert rolls a *saved* report back to creation state.

### Slice 2b — View-mode toolbar + report versioning (decided 2026-06-01, supersedes the `original_*`/`/reset` design)

User feedback after using Slices 1-2: the editor toolbar is incoherent (lands in edit mode with Save disabled but everything else enabled), the View button is broken, and "Revert to original" is ambiguous. Decisions:

**Toolbar (frontend):**
- Existing reports **open in View mode** (charts only). View toolbar = **Edit**, **History**, **Delete**. A report with zero widgets (blank) may open in edit mode so the user can start.
- **Edit** enters edit mode: **Add widget**, **Save** (enabled only when there are unsaved changes), **Cancel** (shown/enabled only when there are unsaved changes), **History**, **Delete**, **Done** (back to View).
- **Fix the View/Done toggle** (currently a no-op bug). `editMode` + a `dirty` flag already exist (Slice 2) — drive button enablement off `dirty`, default `editMode=false` for non-empty reports.

**Versioning (replaces `original_layout_json`/`original_canvas_filters_json` + `POST /{id}/reset`):**
- New `report_versions` table: `id, report_id FK (ON DELETE CASCADE), is_original BOOL, layout_json JSON, canvas_filters_json JSON, created_at`. Migration also **drops** the `original_*` columns from migration 063 (pre-launch, no backcompat); backfill one `is_original=True` version per existing report from `original_layout_json` (fallback to live `layout_json`).
- On report **create**: insert the `is_original=True` version.
- On each **Save** (layout/filters change): insert a new `is_original=False` version. Retention = **pin the original + keep the 4 most recent non-original** (evict oldest non-original when the count exceeds 4). Max 5 total.
- Endpoints (inherit `require_reports_v2_enabled`): `GET /api/v1/reports/{id}/versions` (newest-first, original flagged), `POST /api/v1/reports/{id}/versions/{version_id}/restore` (copies that version's layout/filters into the live report; edit-rights gated; does NOT itself create a version — the next Save does). Reimplement `POST /{id}/reset` as sugar that restores the pinned original version so the existing "Revert to original" button keeps working.
- **History panel (frontend):** lists versions (Original pinned + recent, with timestamps), each with **Restore**. "Revert to original" becomes the Restore action on the Original entry.

This slice replaces the Slice-1 snapshot columns and the Slice-2 `/reset` internals. Update/replace their tests accordingly.

### Slice 3 — Complete PR4 (per locked spec §11)

- **Duplicate:** `POST /api/v1/reports/{id}/duplicate` -> clone as private; button on report cards.
- **Sharing/visibility:** private <-> org toggle in the report header, gated by edit rights (owner always; org owner/admin for org-shared).
- **CSV export per widget:** view-mode button. **Default: client-side** (each widget already holds its rows; single-measure widgets via `data.rows`, multi-series via the `series[]` from `useSeriesQueries` — the export helper needs per-widget-type extraction). No new endpoint, no extra auth surface. Architect lock #3 applies only if a server route is later chosen.
- **Mobile:** single-column read-only stack on `<sm` (editing stays desktop-only).
- **Ownership transfer — CORRECTNESS FIX, not polish (do this first in Slice 3).** `backend/app/services/admin_users_service.py:delete_user` (~line 93-171) cleans up `invitations` + `import_batches` then `DELETE FROM users`, but **never touches `reports`**. Because `reports.owner_user_id` is `ON DELETE RESTRICT` (migration `051`), deleting any user who owns a report **already raises an FK violation today**. Fix: before deleting the user, transfer their org-shared reports to the org owner and hard-delete their private reports. Scope is **only this path** — member "removal" is just `is_active=False` (no delete, no RESTRICT hit; `admin_org_members_service.update_member:167-173`), and **org-owner transfer doesn't exist yet** as a service. Drop those two from scope; they don't apply. (See [[reference_org_delete_cascade_fk_audit]] — same FK-cascade discipline.)

## Phasing

Subagent-driven, sequential with review gates; **branch + PR per slice**. Slice 1 first (unblocks "can I make a report at all"), then Slice 2, then Slice 3. Production flag stays gated in `.do/app.yaml` until owner sign-off; `FEATURE_REPORTS_V2` default flip in `.env.example` lands with Slice 3.

## Minor / noted

- Query `meta` quirk: `truncated:true` and `query_ms:0` fire whenever `row_count == limit` even at small limits. Optional polish, not in critical path.

## Out of scope (unchanged from parent spec)

Public share links, real-time refresh, cross-org reports, drill-down, saved widget presets, non-`transactions` datasets, Sankey/treemap/gauge/scatter, materialized rollups, in-canvas text blocks, i18n, audit logging of report CRUD.

Backlog (post-save presentation): after Save/Done the report stays on the same canvas in view mode; users expect Done to feel like leaving the editor (navigate to a read-only view or back to the list). Deferred per owner 2026-06-01.
