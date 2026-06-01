"use client";

/**
 * Reports v2 — single-report editor.
 *
 * Loads the report by id, hydrates the canvas + widgets, lets the
 * user add / configure / resize / drag widgets, and saves the
 * layout JSON via PATCH on explicit "Save" click.
 *
 * Save semantics (PR2 decision): explicit save button, NOT
 * debounced auto-save. Reasoning:
 *  - explicit save keeps every PATCH a user-initiated act and
 *    keeps "dirty" state visible (Save → Saved transition).
 *  - debounced auto-save tangles with the SWR query layer per
 *    widget (rapid filter changes would churn the layout PATCH
 *    AND fire 8 query refetches per debounce slot).
 *  - explicit save fits PR2 scope; PR4 can layer auto-save on
 *    top once sharing + templates land.
 *
 * Architect ambiguity resolved: spec §1 ASCII mockup shows a "Save"
 * button in the header; the rollout-table PR2 row says "Save layout
 * (PATCH)" — both align with explicit-save.
 */
import { use, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import {
  deleteReport,
  duplicateReport,
  getReport,
  restoreVersion,
  saveLayout,
  updateReport,
} from "@/lib/reports/api";
import type {
  BarConfig,
  CanvasFilters,
  KPIConfig,
  LayoutJson,
  ReportSummary,
  ReportVersionSummary,
  Widget,
} from "@/lib/reports/types";
import ConfirmModal from "@/components/ui/ConfirmModal";
import Canvas from "@/components/reports/Canvas";
import HistoryPanel from "@/components/reports/HistoryPanel";
import CanvasFiltersBar from "@/components/reports/CanvasFiltersBar";
import ConfigRail from "@/components/reports/ConfigRail";
import WidgetPicker from "@/components/reports/WidgetPicker";
import WidgetShell from "@/components/reports/WidgetShell";
import KPIWidget from "@/components/reports/widgets/KPIWidget";
import BarWidget from "@/components/reports/widgets/BarWidget";
import LineWidget from "@/components/reports/widgets/LineWidget";
import AreaWidget from "@/components/reports/widgets/AreaWidget";
import PieWidget from "@/components/reports/widgets/PieWidget";
import SparklineWidget from "@/components/reports/widgets/SparklineWidget";
import StackedBarWidget from "@/components/reports/widgets/StackedBarWidget";
import TableWidget from "@/components/reports/widgets/TableWidget";
import type { WidgetType } from "@/lib/reports/types";

interface PageProps {
  // Next 15 makes ``params`` a promise on server-rendered pages; in
  // tests we can also pass a plain object so we don't have to wrap
  // every render in a Suspense boundary that handles ``use()``.
  params: Promise<{ id: string }> | { id: string };
}

const DEFAULT_LAYOUT: LayoutJson = { version: 1, widgets: [] };

function emptyKPI(id: string): Widget {
  const config: KPIConfig = {
    dataset: "transactions",
    measure: { agg: "sum", field: "amount" },
    format: "currency",
    compare_prior_period: false,
  };
  return {
    id,
    type: "kpi",
    title: "New KPI",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config,
  };
}

function emptyBar(id: string): Widget {
  const config: BarConfig = {
    dataset: "transactions",
    measure: { agg: "sum", field: "amount" },
    dimensions: ["category"],
    sort: { by: "value", dir: "desc" },
    limit: 10,
    format: "currency",
  };
  return {
    id,
    type: "bar",
    title: "New bar chart",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config,
  };
}

function emptyMultiSeries(
  id: string,
  type: "line" | "area" | "stacked_bar" | "table",
): Widget {
  const baseConfig = {
    dataset: "transactions" as const,
    measures: [{ measure: { agg: "sum" as const, field: "amount" as const } }],
    dimensions: [type === "table" ? ("category" as const) : ("month" as const)],
    sort: { by: "value" as const, dir: "desc" as const },
    limit: type === "table" ? 50 : 100,
    format: "currency" as const,
  };
  const baseGrid = type === "table" ? { x: 0, y: 0, w: 12, h: 6 } : { x: 0, y: 0, w: 6, h: 4 };
  return {
    id,
    type,
    title:
      type === "line"
        ? "New line chart"
        : type === "area"
          ? "New area chart"
          : type === "stacked_bar"
            ? "New stacked bar chart"
            : "New table",
    grid: baseGrid,
    config: baseConfig,
  } as Widget;
}

function emptyPie(id: string): Widget {
  return {
    id,
    type: "pie",
    title: "New pie chart",
    grid: { x: 0, y: 0, w: 4, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
      sort: { by: "value", dir: "desc" },
      limit: 50,
      format: "currency",
      top_n: 8,
    },
  };
}

function emptySparkline(id: string): Widget {
  return {
    id,
    type: "sparkline",
    title: "New sparkline",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 50,
      format: "number",
    },
  };
}

function emptyWidget(type: WidgetType, id: string): Widget {
  switch (type) {
    case "kpi":
      return emptyKPI(id);
    case "bar":
      return emptyBar(id);
    case "line":
      return emptyMultiSeries(id, "line");
    case "area":
      return emptyMultiSeries(id, "area");
    case "stacked_bar":
      return emptyMultiSeries(id, "stacked_bar");
    case "table":
      return emptyMultiSeries(id, "table");
    case "pie":
      return emptyPie(id);
    case "sparkline":
      return emptySparkline(id);
  }
}

function renderWidgetByType(w: Widget, canvasFilters: CanvasFilters) {
  switch (w.type) {
    case "kpi":
      return <KPIWidget widget={w} canvasFilters={canvasFilters} />;
    case "bar":
      return <BarWidget widget={w} canvasFilters={canvasFilters} />;
    case "line":
      return <LineWidget widget={w} canvasFilters={canvasFilters} />;
    case "area":
      return <AreaWidget widget={w} canvasFilters={canvasFilters} />;
    case "pie":
      return <PieWidget widget={w} canvasFilters={canvasFilters} />;
    case "sparkline":
      return <SparklineWidget widget={w} canvasFilters={canvasFilters} />;
    case "stacked_bar":
      return <StackedBarWidget widget={w} canvasFilters={canvasFilters} />;
    case "table":
      return <TableWidget widget={w} canvasFilters={canvasFilters} />;
  }
}

function newWidgetId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `w_${crypto.randomUUID().slice(0, 8)}`;
  }
  return `w_${Math.random().toString(36).slice(2, 10)}`;
}

export default function ReportEditorPage({ params }: PageProps) {
  // Next 15 makes ``params`` a promise; ``use()`` unwraps it on the
  // client without awaiting at the top level. In test harnesses we
  // can pass a plain object and skip the promise branch entirely so
  // the editor doesn't suspend.
  const resolvedParams =
    params && typeof (params as Promise<{ id: string }>).then === "function"
      ? use(params as Promise<{ id: string }>)
      : (params as { id: string });
  const { id } = resolvedParams;
  const router = useRouter();
  const { user, loading: authLoading, featureReportsV2 } = useAuth();

  const [report, setReport] = useState<ReportSummary | null>(null);
  const [layout, setLayout] = useState<LayoutJson>(DEFAULT_LAYOUT);
  const [canvasFilters, setCanvasFilters] = useState<CanvasFilters>({});
  const [selectedWidgetId, setSelectedWidgetId] = useState<string | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [lastSavedAt, setLastSavedAt] = useState<Date | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [duplicating, setDuplicating] = useState(false);
  const [togglingVisibility, setTogglingVisibility] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [pendingRestore, setPendingRestore] =
    useState<ReportVersionSummary | null>(null);

  // Hydrate the canvas state (report snapshot + layout + filters) from
  // a server ``ReportSummary``. Reused by the initial load, Cancel
  // (restore the last-saved snapshot), and Revert (rolled-back
  // snapshot).
  function hydrateFromReport(r: ReportSummary) {
    setReport(r);
    const lj = (r.layout_json ?? {}) as Partial<LayoutJson>;
    setLayout(
      lj && Array.isArray(lj.widgets)
        ? { version: 1, widgets: lj.widgets as Widget[] }
        : DEFAULT_LAYOUT,
    );
    setCanvasFilters((r.canvas_filters_json as CanvasFilters) ?? {});
    setSelectedWidgetId(null);
    setDirty(false);
  }

  // Owner-only affordances. The backend enforces this regardless, but
  // hiding Delete/Revert for non-owners keeps the header honest when
  // the page already knows the viewer isn't the owner.
  const canEdit = !!user && !!report && report.owner_user_id === user.id;

  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!featureReportsV2) {
      router.replace("/dashboard");
      return;
    }
    let cancelled = false;
    getReport(Number(id))
      .then((r) => {
        if (cancelled) return;
        hydrateFromReport(r);
        // Default to view mode for reports that already have content; a
        // blank (0-widget) report opens in edit mode so the user has an
        // obvious starting point.
        const lj = (r.layout_json ?? {}) as Partial<LayoutJson>;
        const widgetCount = Array.isArray(lj.widgets) ? lj.widgets.length : 0;
        setEditMode(widgetCount === 0);
      })
      .catch((err: Error) => {
        if (!cancelled) setLoadError(err.message || "Couldn't load report");
      });
    return () => {
      cancelled = true;
    };
    // ``router`` is intentionally omitted — useRouter() may not
    // guarantee a referentially-stable identity across renders, and
    // refiring the load effect would clobber unsaved canvas edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, user, authLoading, featureReportsV2]);

  const selectedWidget = useMemo(
    () => layout.widgets.find((w) => w.id === selectedWidgetId) ?? null,
    [layout.widgets, selectedWidgetId],
  );

  function updateLayout(next: LayoutJson) {
    setLayout(next);
    setDirty(true);
  }

  function updateWidget(nextWidget: Widget) {
    setLayout((prev) => ({
      ...prev,
      widgets: prev.widgets.map((w) =>
        w.id === nextWidget.id ? nextWidget : w,
      ),
    }));
    setDirty(true);
  }

  function addWidget(type: WidgetType) {
    const id = newWidgetId();
    const widget = emptyWidget(type, id);
    // Drop the new widget at the bottom of the existing stack.
    const maxY = layout.widgets.reduce(
      (max, w) => Math.max(max, w.grid.y + w.grid.h),
      0,
    );
    widget.grid = { ...widget.grid, x: 0, y: maxY };
    setLayout((prev) => ({ ...prev, widgets: [...prev.widgets, widget] }));
    setSelectedWidgetId(id);
    setPickerOpen(false);
    setDirty(true);
  }

  function removeWidget(widgetId: string) {
    setLayout((prev) => ({
      ...prev,
      widgets: prev.widgets.filter((w) => w.id !== widgetId),
    }));
    if (selectedWidgetId === widgetId) setSelectedWidgetId(null);
    setDirty(true);
  }

  function updateCanvasFilters(next: CanvasFilters) {
    setCanvasFilters(next);
    setDirty(true);
  }

  async function handleSave() {
    if (!report || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await saveLayout(report.id, layout, canvasFilters);
      setReport(saved);
      setDirty(false);
      setLastSavedAt(new Date());
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't save layout");
    } finally {
      setSaving(false);
    }
  }

  // Cancel editing — discard unsaved local changes by restoring the
  // last-saved server snapshot the page already holds in ``report``,
  // then drop back to view mode. No network call: ``report`` always
  // reflects the most recent successful load/save.
  function handleCancelEdit() {
    if (report) hydrateFromReport(report);
    setEditMode(false);
    setSaveError(null);
  }

  async function handleDelete() {
    if (!report || deleting) return;
    setConfirmDelete(false);
    setDeleting(true);
    setSaveError(null);
    try {
      await deleteReport(report.id);
      router.push("/reports");
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't delete report");
      setDeleting(false);
    }
  }

  // Duplicate the report into a fresh private copy owned by the
  // viewer, then navigate to the copy's editor. Anyone who can view
  // the report (i.e. is on this page) can duplicate it.
  async function handleDuplicate() {
    if (!report || duplicating) return;
    setDuplicating(true);
    setSaveError(null);
    try {
      const copy = await duplicateReport(report.id);
      router.push(`/reports/${copy.id}`);
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't duplicate report");
      setDuplicating(false);
    }
  }

  // Flip the report between private and org-shared visibility. Gated
  // on edit rights (``canEdit``); the backend enforces this regardless.
  async function handleToggleVisibility() {
    if (!report || togglingVisibility) return;
    const next: ReportSummary["visibility"] =
      report.visibility === "org" ? "private" : "org";
    setTogglingVisibility(true);
    setSaveError(null);
    try {
      const updated = await updateReport(report.id, { visibility: next });
      setReport(updated);
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't update sharing");
    } finally {
      setTogglingVisibility(false);
    }
  }

  // Restore a chosen version into the live report. Confirmed via the
  // ConfirmModal before this fires. Re-hydrates the canvas from the
  // server's restored snapshot, closes the History panel, and drops
  // back to view mode.
  async function handleRestore() {
    if (!report || !pendingRestore || restoring) return;
    const versionId = pendingRestore.id;
    setRestoring(true);
    setSaveError(null);
    try {
      const restored = await restoreVersion(report.id, versionId);
      hydrateFromReport(restored);
      setEditMode(false);
      setHistoryOpen(false);
      setPendingRestore(null);
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't restore version");
    } finally {
      setRestoring(false);
    }
  }

  if (authLoading || (!report && !loadError)) {
    return (
      <AppShell>
        <div className="flex h-full items-center justify-center">
          <div
            data-testid="report-editor-loading"
            className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent"
          />
        </div>
      </AppShell>
    );
  }

  if (loadError) {
    return (
      <AppShell>
        <div
          role="alert"
          className="rounded-md border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger"
        >
          {loadError}
        </div>
        <Link
          href="/reports"
          className="mt-3 inline-block text-sm text-accent hover:underline"
        >
          Back to reports
        </Link>
      </AppShell>
    );
  }

  if (!report) return null;

  return (
    <AppShell>
      <div className="flex h-full flex-col" data-testid="report-editor">
      <header className="flex items-center justify-between border-b border-border bg-surface px-4 py-3">
        <div className="flex items-center gap-3">
          <Link
            href="/reports"
            className="text-sm text-text-muted hover:text-text-primary"
          >
            Reports
          </Link>
          <span className="text-text-muted">/</span>
          <span className="text-sm font-semibold text-text-primary">
            {report.name}
          </span>
          {dirty && (
            <span
              data-testid="report-editor-dirty"
              className="text-xs text-text-muted"
            >
              Unsaved changes
            </span>
          )}
          {!dirty && lastSavedAt && (
            <span className="text-xs text-text-muted">Saved</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* Edit-mode-only: Add widget + Save (+ Cancel when dirty). */}
          {editMode && (
            <button
              type="button"
              onClick={() => setPickerOpen(true)}
              className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-bg-elevated"
              data-testid="report-editor-add-widget"
            >
              Add widget
            </button>
          )}
          {editMode && (
            <button
              type="button"
              onClick={handleSave}
              disabled={saving || !dirty}
              className="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-foreground transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
              data-testid="report-editor-save"
            >
              {saving ? "Saving..." : "Save"}
            </button>
          )}
          {editMode && dirty && (
            <button
              type="button"
              onClick={handleCancelEdit}
              className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-bg-elevated"
              data-testid="report-editor-cancel"
            >
              Cancel
            </button>
          )}

          {/* Both modes: Sharing state, Duplicate, History + Delete. */}
          {/* Visibility: editors get a toggle; non-editors see it read-only. */}
          {canEdit ? (
            <button
              type="button"
              onClick={handleToggleVisibility}
              disabled={togglingVisibility}
              className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-bg-elevated disabled:cursor-not-allowed disabled:opacity-60"
              data-testid="report-editor-visibility-toggle"
              aria-label="Toggle report sharing"
            >
              <span data-testid="report-editor-visibility">
                {report.visibility === "org" ? "Shared with org" : "Private"}
              </span>
            </button>
          ) : (
            <span
              className="rounded-md border border-border px-3 py-1.5 text-sm text-text-muted"
              data-testid="report-editor-visibility"
            >
              {report.visibility === "org" ? "Shared with org" : "Private"}
            </span>
          )}

          <button
            type="button"
            onClick={handleDuplicate}
            disabled={duplicating}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-bg-elevated disabled:cursor-not-allowed disabled:opacity-60"
            data-testid="report-editor-duplicate"
          >
            {duplicating ? "Duplicating..." : "Duplicate"}
          </button>

          <button
            type="button"
            onClick={() => setHistoryOpen(true)}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-bg-elevated"
            data-testid="report-editor-history"
          >
            History
          </button>
          {canEdit && (
            <button
              type="button"
              onClick={() => setConfirmDelete(true)}
              disabled={deleting}
              className="rounded-md border border-danger px-3 py-1.5 text-sm text-danger hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-60"
              data-testid="report-editor-delete"
            >
              {deleting ? "Deleting..." : "Delete"}
            </button>
          )}

          {/* Mode toggle: View mode shows "Edit", edit mode shows "Done". */}
          <button
            type="button"
            onClick={() => setEditMode((v) => !v)}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-bg-elevated"
            data-testid="report-editor-toggle-edit"
          >
            {editMode ? "Done" : "Edit"}
          </button>
        </div>
      </header>

      {saveError && (
        <div
          role="alert"
          className="border-b border-danger/30 bg-danger/10 px-4 py-2 text-sm text-danger"
        >
          {saveError}
        </div>
      )}

      <div className="border-b border-border bg-bg px-4 py-3">
        <CanvasFiltersBar value={canvasFilters} onChange={updateCanvasFilters} />
      </div>

      <div className="flex flex-1 overflow-hidden">
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {layout.widgets.length === 0 ? (
            <div
              data-testid="report-editor-empty"
              className="rounded-md border border-dashed border-border bg-surface px-6 py-10 text-center"
            >
              <p className="text-sm font-medium text-text-primary">
                This report has no widgets yet
              </p>
              <p className="mx-auto mt-1 max-w-md text-sm text-text-muted">
                Add a widget to see your data. Canvas filters (date,
                accounts, categories) apply to every widget, so a report
                with no widgets shows nothing.
              </p>
              <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
                <button
                  type="button"
                  onClick={() => setPickerOpen(true)}
                  className="rounded-md bg-accent px-4 py-2 text-sm font-semibold text-accent-foreground transition hover:bg-accent-hover"
                  data-testid="report-editor-empty-add-widget"
                >
                  Add widget
                </button>
                <Link
                  href="/reports"
                  className="rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-bg-elevated"
                  data-testid="report-editor-empty-templates"
                >
                  Start from a template
                </Link>
              </div>
            </div>
          ) : (
            <Canvas
              layout={layout}
              editMode={editMode}
              onLayoutChange={updateLayout}
              renderWidget={(w) => (
                <WidgetShell
                  widgetId={w.id}
                  selected={selectedWidgetId === w.id}
                  editMode={editMode}
                  onSelect={() => setSelectedWidgetId(w.id)}
                  onRemove={() => removeWidget(w.id)}
                >
                  {renderWidgetByType(w, canvasFilters)}
                </WidgetShell>
              )}
            />
          )}
        </div>
        {editMode && selectedWidget && (
          <ConfigRail
            widget={selectedWidget}
            canvasFilters={canvasFilters}
            onUpdate={updateWidget}
            onClose={() => setSelectedWidgetId(null)}
          />
        )}
      </div>

        <WidgetPicker
          open={pickerOpen}
          onClose={() => setPickerOpen(false)}
          onPick={addWidget}
        />
      </div>

      <ConfirmModal
        open={confirmDelete}
        title="Delete report"
        message="Delete this report? This can't be undone."
        confirmLabel="Delete"
        variant="danger"
        onConfirm={handleDelete}
        onCancel={() => setConfirmDelete(false)}
      />
      <HistoryPanel
        open={historyOpen}
        reportId={report.id}
        onClose={() => setHistoryOpen(false)}
        onRestore={(v) => setPendingRestore(v)}
      />
      <ConfirmModal
        open={pendingRestore !== null}
        title="Restore version"
        message="Restore this version? Current unsaved changes will be lost."
        confirmLabel="Restore"
        variant="warning"
        onConfirm={handleRestore}
        onCancel={() => setPendingRestore(null)}
      />
    </AppShell>
  );
}
