"use client";

/**
 * CustomDashboard — the Canvas-based dashboard shell, shown only when the
 * ``customDashboard`` feature flag is ON.
 *
 * Mirrors the load / Customize-mode / Save pattern from
 * ``app/reports/[id]/page.tsx`` using the same Canvas + WidgetShell kit.
 *
 * Phase 2a: wraps content in DashboardDataProvider so the 3 finance tiles
 * (dash_on_track, dash_accounts, dash_account_forecast) can read shared data.
 * Renders DashboardPeriodNav as fixed chrome above the Canvas.
 * Uses renderDashboardWidget (which handles dash_* + reports fall-through),
 * replacing the former local copy of renderWidgetByType.
 *
 * Save semantics: explicit Save button (not auto-save), identical to
 * the Reports editor. Customize mode is desktop-only; mobile renders
 * a read-only single-column stack (same mobileStackHeight pattern).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

import AppShell from "@/components/AppShell";
import Canvas from "@/components/reports/Canvas";
import WidgetShell from "@/components/reports/WidgetShell";
import AddWidgetMenu from "@/components/dashboard/AddWidgetMenu";
import { DashboardDataProvider, useDashboard } from "@/components/dashboard/DashboardDataProvider";
import DashboardPeriodNav from "@/components/dashboard/DashboardPeriodNav";
import { renderDashboardWidget } from "@/components/dashboard/renderDashboardWidget";
import ConfirmModal from "@/components/ui/ConfirmModal";
import { getDashboard, getDefaultDashboard, saveDashboard } from "@/lib/dashboard/api";
import type { CanvasFilters, LayoutJson } from "@/lib/dashboard/types";
import {
  emptyDashboardWidget,
  type DashboardWidget,
  type DashboardWidgetType,
} from "@/lib/dashboard/widget-types";
import type { Widget } from "@/lib/reports/types";
import { cloneWidgetForDashboard } from "@/lib/dashboard/clone";
import { newWidgetId } from "@/components/reports/widgetKit";
import { useIsMobile } from "@/lib/hooks/use-is-mobile";
import { card, pageTitle } from "@/lib/styles";
import { useFilterChipState } from "@/lib/reports/use-filter-chip-state";
import { reportCurrency } from "@/lib/reports/series";

// Re-use the mobile-stack helpers from the shared lib module.
import {
  mobileStackHeight,
  orderWidgetsForStack,
} from "@/lib/reports/stack";

const DEFAULT_LAYOUT: LayoutJson = { version: 1, widgets: [] };

// Inner component: reads refreshError from the DashboardDataProvider context
// and renders an inline retry affordance when a post-write refresh fails.
// Must be rendered inside <DashboardDataProvider> so useDashboard() resolves.
function RefreshErrorBanner() {
  const { refreshError, onDismissRefreshError, refresh } = useDashboard();
  if (!refreshError) return null;
  return (
    <div
      role="alert"
      data-testid="dashboard-refresh-error"
      className="mb-4 flex items-center justify-between rounded-md border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger"
    >
      <span>Refresh failed. Your data may be out of date.</span>
      <div className="ml-4 flex items-center gap-2">
        <button
          type="button"
          onClick={() => void refresh()}
          className="rounded px-2 py-0.5 text-sm font-medium underline underline-offset-2 hover:opacity-80 focus:outline-none focus-visible:ring-2 focus-visible:ring-danger/30"
        >
          Retry
        </button>
        <button
          type="button"
          aria-label="Dismiss"
          onClick={onDismissRefreshError}
          className="rounded p-0.5 hover:bg-danger/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-danger/30"
        >
          ×
        </button>
      </div>
    </div>
  );
}

export default function CustomDashboard() {
  const isSmallScreen = useIsMobile();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [resetBanner, setResetBanner] = useState(false);
  // Guard: only process ?reset=1 once per mount, even if the router/searchParams
  // references change between renders (e.g., in test environments).
  const resetProcessed = useRef(false);

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
  const [pickerOpen, setPickerOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);

  // L3.1 port: read ?reset=1 left by the org data-wipe flow, show a
  // one-time success banner, then strip the param so a refresh doesn't replay it.
  // The resetProcessed ref prevents re-triggering when router/searchParams refs
  // change between renders (guards against test environments and edge cases).
  useEffect(() => {
    if (!resetProcessed.current && searchParams.get("reset") === "1") {
      resetProcessed.current = true;
      setResetBanner(true);
      router.replace("/dashboard");
    }
  }, [searchParams, router]);

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

  function addDashTile(type: DashboardWidgetType) {
    const id = newWidgetId();
    const base = emptyDashboardWidget(type, id);
    // Compute maxY inside the functional updater so it reads the latest
    // prev.widgets — avoids a stale-closure bug if two tiles are added
    // before a re-render.
    setLayout((prev) => {
      const maxY = prev.widgets.reduce(
        (m, x) => Math.max(m, x.grid.y + x.grid.h),
        0,
      );
      const w = { ...base, grid: { ...base.grid, x: 0, y: maxY } };
      // Cast to Widget: LayoutJson.widgets is Widget[] but the canvas dispatcher
      // (renderDashboardWidget) handles dash_* types at runtime via the DashboardWidget
      // union. This cast is safe — the same pattern is used in the Canvas renderWidget call.
      return { ...prev, widgets: [...prev.widgets, w as unknown as Widget] };
    });
    setSelectedWidgetId(id);
    setDirty(true);
    setPickerOpen(false);
  }

  function addClonedWidget(source: Widget) {
    // Compute the clone's grid placement INSIDE the functional updater so it
    // reads prev.widgets — mirrors addDashTile's pattern. This removes the
    // stale-closure risk when two clones are added before a re-render.
    let cloneId: string;
    setLayout((prev) => {
      const clone = cloneWidgetForDashboard(source, prev.widgets);
      cloneId = clone.id;
      return { ...prev, widgets: [...prev.widgets, clone] };
    });
    // cloneId is always assigned synchronously by the updater before the
    // setSelectedWidgetId call executes (React batches but runs updaters first).
    setSelectedWidgetId(cloneId!);
    setDirty(true);
    setPickerOpen(false);
  }

  async function handleResetConfirm() {
    setResetOpen(false);
    const d = await getDefaultDashboard();
    setLayout(d.layout_json);
    setCanvasFilters(d.canvas_filters_json);
    setDirty(true);
  }

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
      <DashboardDataProvider>
        {/* ?reset=1 success banner — org data wipe confirmation */}
        {resetBanner && (
          <div
            role="status"
            data-testid="reset-banner"
            className="mb-4 flex items-center justify-between rounded-md border border-success/30 bg-success/10 px-4 py-3 text-sm text-success"
          >
            <span>All organisation data has been cleared.</span>
            <button
              type="button"
              aria-label="Dismiss"
              onClick={() => setResetBanner(false)}
              className="ml-4 rounded p-0.5 hover:bg-success/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-success/30"
            >
              ×
            </button>
          </div>
        )}
        {/* Post-write refresh error banner with inline retry */}
        <RefreshErrorBanner />
        <div
          data-testid="custom-dashboard"
          className="flex h-full flex-col"
        >
          {/* Header */}
          <div data-tour-id="dashboard.header" className="mb-6 flex items-center justify-between">
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
              {/* Add widget (only visible in Customize mode) */}
              {editModeActive && (
                <button
                  type="button"
                  data-testid="custom-dashboard-add-widget"
                  onClick={() => setPickerOpen(true)}
                  className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-surface-raised"
                >
                  Add widget
                </button>
              )}
              {/* Reset to default (only visible in Customize mode) */}
              {editModeActive && (
                <button
                  type="button"
                  data-testid="custom-dashboard-reset"
                  onClick={() => setResetOpen(true)}
                  className="rounded-md border border-border px-3 py-1.5 text-sm text-text-primary hover:bg-surface-raised"
                >
                  Reset to default
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

          {/* Period navigation chrome */}
          <DashboardPeriodNav />

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
                    {renderDashboardWidget(
                      w as DashboardWidget | Widget,
                      canvasFilters,
                      false,
                      currency,
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <Canvas
              layout={layout}
              editMode={editModeActive}
              compact
              onLayoutChange={updateLayout}
              renderWidget={(w) => (
                // h-full is load-bearing: react-grid-layout sizes the grid item
                // to a fixed pixel box (h*rowHeight + margins); WidgetShell uses
                // `h-full` to fill it. This wrapper sits between the two (it
                // carries the test id), so without `h-full` here the height chain
                // breaks — tiles collapse to content height inside a taller box,
                // floating the resize handle and overflowing neighbours. Reports
                // has no such wrapper (WidgetShell is the grid item's direct child).
                <div data-testid={`widget-${w.type}`} className="h-full">
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
                    {renderDashboardWidget(
                      w as DashboardWidget | Widget,
                      canvasFilters,
                      editModeActive,
                      currency,
                    )}
                  </WidgetShell>
                </div>
              )}
            />
          )}

          {/* Add-widget picker — rendered when editModeActive */}
          <AddWidgetMenu
            open={pickerOpen}
            onClose={() => setPickerOpen(false)}
            existing={layout.widgets}
            onAddDashTile={addDashTile}
            onAddCloned={addClonedWidget}
          />

          {/* Reset-to-default confirm modal */}
          <ConfirmModal
            open={resetOpen}
            title="Reset to default?"
            message="This will replace your current layout with the 7-tile default. You can review before saving."
            confirmLabel="Reset"
            cancelLabel="Cancel"
            variant="warning"
            onConfirm={handleResetConfirm}
            onCancel={() => setResetOpen(false)}
          />
        </div>
      </DashboardDataProvider>
    </AppShell>
  );
}
