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

import { useAuth } from "@/components/auth/AuthProvider";
import { getReport, saveLayout } from "@/lib/reports/api";
import type {
  BarConfig,
  CanvasFilters,
  KPIConfig,
  LayoutJson,
  ReportSummary,
  Widget,
} from "@/lib/reports/types";
import Canvas from "@/components/reports/Canvas";
import CanvasFiltersBar from "@/components/reports/CanvasFiltersBar";
import ConfigRail from "@/components/reports/ConfigRail";
import WidgetPicker from "@/components/reports/WidgetPicker";
import WidgetShell from "@/components/reports/WidgetShell";
import KPIWidget from "@/components/reports/widgets/KPIWidget";
import BarWidget from "@/components/reports/widgets/BarWidget";

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
  const [editMode, setEditMode] = useState(true);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [lastSavedAt, setLastSavedAt] = useState<Date | null>(null);

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
        setReport(r);
        const lj = (r.layout_json ?? {}) as Partial<LayoutJson>;
        setLayout(
          lj && Array.isArray(lj.widgets)
            ? { version: 1, widgets: lj.widgets as Widget[] }
            : DEFAULT_LAYOUT,
        );
        setCanvasFilters((r.canvas_filters_json as CanvasFilters) ?? {});
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

  function addWidget(type: "kpi" | "bar") {
    const id = newWidgetId();
    const widget = type === "kpi" ? emptyKPI(id) : emptyBar(id);
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

  if (authLoading || (!report && !loadError)) {
    return (
      <div className="flex h-full items-center justify-center">
        <div
          data-testid="report-editor-loading"
          className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent"
        />
      </div>
    );
  }

  if (loadError) {
    return (
      <main className="mx-auto w-full max-w-3xl px-4 py-8">
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
      </main>
    );
  }

  if (!report) return null;

  return (
    <main className="flex h-full flex-col" data-testid="report-editor">
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
          <button
            type="button"
            onClick={() => setEditMode((v) => !v)}
            className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-bg-elevated"
            data-testid="report-editor-toggle-edit"
          >
            {editMode ? "View" : "Edit"}
          </button>
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
          <button
            type="button"
            onClick={handleSave}
            disabled={saving || !dirty}
            className="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-foreground transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
            data-testid="report-editor-save"
          >
            {saving ? "Saving..." : "Save"}
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
              className="rounded-md border border-dashed border-border bg-surface px-6 py-10 text-center text-sm text-text-muted"
            >
              No widgets yet. Click &quot;Add widget&quot; to start.
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
                  {w.type === "kpi" ? (
                    <KPIWidget widget={w} canvasFilters={canvasFilters} />
                  ) : (
                    <BarWidget widget={w} canvasFilters={canvasFilters} />
                  )}
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
    </main>
  );
}
