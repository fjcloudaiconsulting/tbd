"use client";

/**
 * CustomDashboard — the Canvas-based dashboard shell, shown only when the
 * ``customDashboard`` feature flag is ON.
 *
 * Mirrors the load / Customize-mode / Save pattern from
 * ``app/reports/[id]/page.tsx`` using the same Canvas + WidgetShell kit.
 * Phase 1's layout is whatever the server returns from
 * ``getDashboard()`` (a minimal KPI widget by default); real finance
 * tiles arrive in Phase 2.
 *
 * Save semantics: explicit Save button (not auto-save), identical to
 * the Reports editor. Customize mode is desktop-only; mobile renders
 * a read-only single-column stack (same mobileStackHeight pattern).
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import AppShell from "@/components/AppShell";
import Canvas from "@/components/reports/Canvas";
import WidgetShell from "@/components/reports/WidgetShell";
import { getDashboard, saveDashboard } from "@/lib/dashboard/api";
import type { CanvasFilters, LayoutJson } from "@/lib/dashboard/types";
import type { Widget } from "@/lib/reports/types";
import { useIsMobile } from "@/lib/hooks/use-is-mobile";
import { card, pageTitle } from "@/lib/styles";
import { useFilterChipState } from "@/lib/reports/use-filter-chip-state";
import { reportCurrency } from "@/lib/reports/series";

// Re-use the mobile-stack helpers from the shared lib module.
import {
  mobileStackHeight,
  orderWidgetsForStack,
} from "@/lib/reports/stack";

// --- widget renderer ---
// Phase 1: the server default layout contains only a KPI widget.
// Phase 2 will expand this with finance-specific widgets.
// We re-use the same renderWidgetByType from the reports surface by
// importing the widget components directly so CustomDashboard never
// diverges from the reports widget kit.
import KPIWidget from "@/components/reports/widgets/KPIWidget";
import BarWidget from "@/components/reports/widgets/BarWidget";
import LineWidget from "@/components/reports/widgets/LineWidget";
import AreaWidget from "@/components/reports/widgets/AreaWidget";
import PieWidget from "@/components/reports/widgets/PieWidget";
import SparklineWidget from "@/components/reports/widgets/SparklineWidget";
import StackedBarWidget from "@/components/reports/widgets/StackedBarWidget";
import TableWidget from "@/components/reports/widgets/TableWidget";
import SankeyWidget from "@/components/reports/widgets/SankeyWidget";

const DEFAULT_LAYOUT: LayoutJson = { version: 1, widgets: [] };

function renderWidgetByType(
  w: Widget,
  canvasFilters: CanvasFilters,
  editMode: boolean,
  currency?: string,
) {
  switch (w.type) {
    case "kpi":
      return <KPIWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} currency={currency} />;
    case "bar":
      return <BarWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} currency={currency} />;
    case "line":
      return <LineWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} currency={currency} />;
    case "area":
      return <AreaWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} currency={currency} />;
    case "pie":
      return <PieWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} currency={currency} />;
    case "sparkline":
      return <SparklineWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} currency={currency} />;
    case "stacked_bar":
      return <StackedBarWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} currency={currency} />;
    case "table":
      return <TableWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} currency={currency} />;
    case "sankey":
      return <SankeyWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} currency={currency} />;
  }
}

export default function CustomDashboard() {
  const isSmallScreen = useIsMobile();

  const [layout, setLayout] = useState<LayoutJson>(DEFAULT_LAYOUT);
  const [canvasFilters, setCanvasFilters] = useState<CanvasFilters>({});
  const [editMode, setEditMode] = useState(false);
  const [selectedWidgetId, setSelectedWidgetId] = useState<string | null>(null);

  const [loadError, setLoadError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [savedAt, setSavedAt] = useState<Date | null>(null);

  // Accounts SWR (shared cache with the reports surface) — used to derive
  // the org display currency so money widgets format correctly.
  const { accounts } = useFilterChipState(setSelectedWidgetId);
  const currency = reportCurrency(accounts);

  // Editing is desktop-only (mirrors reports editor).
  const editModeActive = editMode && !isSmallScreen;

  // Load dashboard on mount.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getDashboard()
      .then((data) => {
        if (cancelled) return;
        const lj = (data.layout_json ?? {}) as Partial<LayoutJson>;
        setLayout(
          lj && Array.isArray(lj.widgets)
            ? { version: 1, widgets: lj.widgets as Widget[] }
            : DEFAULT_LAYOUT,
        );
        setCanvasFilters((data.canvas_filters_json as CanvasFilters) ?? {});
        setLoading(false);
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setLoadError(err.message || "Couldn't load dashboard");
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Stable identity so Canvas's onLayoutChange dep doesn't thrash.
  // setState setters are stable, so [] is an honest dependency list.
  const updateLayout = useCallback((next: LayoutJson) => {
    setLayout(next);
    setDirty(true);
  }, []);

  const handleSave = useCallback(async () => {
    if (saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await saveDashboard(layout, canvasFilters);
      const lj = (saved.layout_json ?? {}) as Partial<LayoutJson>;
      setLayout(
        lj && Array.isArray(lj.widgets)
          ? { version: 1, widgets: lj.widgets as Widget[] }
          : DEFAULT_LAYOUT,
      );
      setCanvasFilters((saved.canvas_filters_json as CanvasFilters) ?? {});
      setDirty(false);
      setSavedAt(new Date());
      setEditMode(false);
      setSelectedWidgetId(null);
    } catch (err) {
      const e = err as Error;
      setSaveError(e.message || "Couldn't save dashboard");
    } finally {
      setSaving(false);
    }
  }, [saving, layout, canvasFilters]);

  // Memoize the widget list for the mobile stack.
  const orderedWidgets = useMemo(
    () => orderWidgetsForStack(layout.widgets),
    [layout.widgets],
  );

  // ── Loading state ──────────────────────────────────────────────
  if (loading) {
    return (
      <AppShell>
        <div
          data-testid="custom-dashboard-loading"
          className="flex h-full items-center justify-center"
        >
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
        </div>
      </AppShell>
    );
  }

  // ── Error state ────────────────────────────────────────────────
  if (loadError) {
    return (
      <AppShell>
        <div
          role="alert"
          data-testid="custom-dashboard-error"
          className="rounded-md border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger"
        >
          {loadError}
        </div>
      </AppShell>
    );
  }

  // ── Render ─────────────────────────────────────────────────────
  return (
    <AppShell>
      <div
        data-testid="custom-dashboard"
        className="flex h-full flex-col"
      >
        {/* Header */}
        <div className="mb-6 flex items-center justify-between">
          <h1 className={`${pageTitle} mb-0`}>Dashboard</h1>
          <div className="flex items-center gap-2">
            {/* Save (only visible in Customize mode) */}
            {editModeActive && (
              <button
                type="button"
                data-testid="custom-dashboard-save"
                onClick={handleSave}
                disabled={saving || !dirty}
                className="rounded-md bg-accent px-3 py-1.5 text-sm font-semibold text-accent-text transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
              >
                {saving ? "Saving…" : "Save"}
              </button>
            )}
            {/* Customize / Done toggle — desktop-only */}
            {!isSmallScreen && (
              <button
                type="button"
                data-testid="custom-dashboard-customize"
                onClick={() => setEditMode((v) => !v)}
                className={`rounded-md border px-3 py-1.5 text-sm ${
                  editMode
                    ? "border-accent text-accent hover:bg-accent/10"
                    : "border-border text-text-primary hover:bg-surface-raised"
                }`}
              >
                {editMode ? "Done" : "Customize"}
              </button>
            )}
          </div>
        </div>

        {/* Inline save/load error */}
        {saveError && (
          <div
            role="alert"
            className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger"
          >
            {saveError}
          </div>
        )}

        {/* "Saved" confirmation */}
        {!dirty && savedAt && !editMode && (
          <p className="mb-2 text-xs text-text-muted">Saved</p>
        )}

        {/* Canvas area */}
        {layout.widgets.length === 0 ? (
          <div
            data-testid="custom-dashboard-empty"
            className={`${card} px-6 py-10 text-center`}
          >
            <p className="text-sm font-medium text-text-primary">
              Your dashboard is empty
            </p>
            <p className="mx-auto mt-1 max-w-md text-sm text-text-muted">
              Customize your dashboard to add widgets.
            </p>
          </div>
        ) : isSmallScreen ? (
          // Mobile: read-only single-column stack, no drag/resize.
          <div
            data-testid="custom-dashboard-stack"
            className="flex flex-col gap-3"
          >
            {orderedWidgets.map((w) => {
              const h = mobileStackHeight(w);
              return (
                <div key={w.id} style={h ? { height: h } : undefined}>
                  {renderWidgetByType(w, canvasFilters, false, currency)}
                </div>
              );
            })}
          </div>
        ) : (
          <Canvas
            layout={layout}
            editMode={editModeActive}
            onLayoutChange={updateLayout}
            renderWidget={(w) => (
              <WidgetShell
                widgetId={w.id}
                selected={selectedWidgetId === w.id}
                editMode={editModeActive}
                onSelect={() => setSelectedWidgetId(w.id)}
                widget={w}
                canvasFilters={canvasFilters}
                accounts={[]}
                categories={[]}
                onSelectFilters={() => {}}
              >
                {renderWidgetByType(w, canvasFilters, editModeActive, currency)}
              </WidgetShell>
            )}
          />
        )}
      </div>
    </AppShell>
  );
}
