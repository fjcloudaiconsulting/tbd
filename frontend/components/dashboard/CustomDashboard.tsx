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
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import Canvas from "@/components/reports/Canvas";
import WidgetShell from "@/components/reports/WidgetShell";
import AddWidgetMenu from "@/components/dashboard/AddWidgetMenu";
import { DashboardDataProvider } from "@/components/dashboard/DashboardDataProvider";
import DashboardPeriodNav from "@/components/dashboard/DashboardPeriodNav";
import { renderDashboardWidget } from "@/components/dashboard/renderDashboardWidget";
import ConfirmModal from "@/components/ui/ConfirmModal";
import TourAnchor from "@/components/tour/TourAnchor";
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
import { btnCanvas, btnCanvasActive, card, pageTitle } from "@/lib/styles";
import { useFilterChipState } from "@/lib/reports/use-filter-chip-state";
import { reportCurrency } from "@/lib/reports/series";

// Re-use the mobile-stack helpers from the shared lib module.
import {
  mobileStackHeight,
  orderWidgetsForStack,
} from "@/lib/reports/stack";

const DEFAULT_LAYOUT: LayoutJson = { version: 1, widgets: [] };

/**
 * Wrap a tile with its first-run tour anchor (Phase 2b).
 *
 * Finance tiles are user-arrangeable, so the tour anchors by tile TYPE rather
 * than position. ``as="child"`` injects ``data-tour-id`` onto the tile wrapper
 * itself (no extra span that would break the grid), and a tile the user has
 * removed simply leaves no anchor, so the tour overlay auto-skips it. The ids
 * are literal (not a lookup map) so the tour source-scan guard in
 * ``tests/lib/help-tour.test.ts`` can see each anchor. Keep these in sync with
 * ``DASHBOARD_TOUR_STEPS`` in ``lib/help/tour.ts``.
 */
function withTileTourAnchor(type: string, node: ReactElement): ReactNode {
  switch (type) {
    case "dash_on_track":
      return <TourAnchor id="dashboard.on-track-tile" as="child">{node}</TourAnchor>;
    case "dash_accounts":
      return <TourAnchor id="dashboard.accounts-tile" as="child">{node}</TourAnchor>;
    case "dash_account_forecast":
      return <TourAnchor id="dashboard.account-forecast" as="child">{node}</TourAnchor>;
    case "dash_recent_transactions":
      return <TourAnchor id="dashboard.recent-transactions" as="child">{node}</TourAnchor>;
    default:
      return node;
  }
}

export default function CustomDashboard() {
  const isSmallScreen = useIsMobile();
  // Auth-readiness gate. CustomDashboard renders <AppShell> as a CHILD, so its
  // mount effects run ABOVE AppShell's `loading || !user` guard — i.e. before
  // AuthProvider has restored the in-memory token on a hard refresh. Gating
  // the layout fetch (and the loaded-branch render that mounts the data
  // provider + filter-chip SWR) on `user` ensures every dashboard request
  // carries a bearer. AuthProvider sets the token before it exposes `user`,
  // so `user` truthy ⇒ token present. Gate on a primitive boolean (not the
  // `user` object) so the effect dep is reference-stable.
  const { user } = useAuth();
  const authReady = !!user;

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
  // Confirm shown when leaving Customize mode (Done) with unsaved changes.
  const [discardOpen, setDiscardOpen] = useState(false);
  // Last persisted layout + filters, so "Done → Discard" can revert without a
  // refetch. Seeded on load and updated on every successful save.
  const savedRef = useRef<{ layout: LayoutJson; filters: CanvasFilters }>({
    layout: DEFAULT_LAYOUT,
    filters: {},
  });

  // Accounts SWR (shared cache with the reports surface) — used to derive
  // the org display currency so money widgets format correctly.
  const { accounts, categories } = useFilterChipState(setSelectedWidgetId);
  const currency = reportCurrency(accounts);

  // Editing is desktop-only (mirrors reports editor).
  const editModeActive = editMode && !isSmallScreen;

  // Load dashboard once auth is ready (token restored). Gating on `user`
  // keeps this fetch from racing AuthProvider's hydration — see the note on
  // the `user` declaration above. Re-runs if `user` transitions to present.
  useEffect(() => {
    if (!authReady) return;
    let cancelled = false;
    setLoading(true);
    getDashboard()
      .then((data) => {
        if (cancelled) return;
        const lj = (data.layout_json ?? {}) as Partial<LayoutJson>;
        const loaded =
          lj && Array.isArray(lj.widgets)
            ? { version: 1 as const, widgets: lj.widgets as Widget[] }
            : DEFAULT_LAYOUT;
        const loadedFilters = (data.canvas_filters_json as CanvasFilters) ?? {};
        setLayout(loaded);
        setCanvasFilters(loadedFilters);
        savedRef.current = { layout: loaded, filters: loadedFilters };
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
  }, [authReady]);

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

  // Remove a tile in Customize mode (mirrors the reports editor). Clears the
  // selection if the removed tile was selected and marks the canvas dirty so
  // the change is saved on Save.
  function removeWidget(widgetId: string) {
    setLayout((prev) => ({
      ...prev,
      widgets: prev.widgets.filter((w) => w.id !== widgetId),
    }));
    if (selectedWidgetId === widgetId) setSelectedWidgetId(null);
    setDirty(true);
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
      const persisted =
        lj && Array.isArray(lj.widgets)
          ? { version: 1 as const, widgets: lj.widgets as Widget[] }
          : DEFAULT_LAYOUT;
      const persistedFilters =
        (saved.canvas_filters_json as CanvasFilters) ?? {};
      setLayout(persisted);
      setCanvasFilters(persistedFilters);
      savedRef.current = { layout: persisted, filters: persistedFilters };
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

  // Leave Customize mode. With unsaved changes, confirm first — otherwise Done
  // silently drops the edits (they vanish on the next reload, since the
  // dashboard re-fetches the last saved layout).
  function leaveCustomize() {
    if (dirty) {
      setDiscardOpen(true);
      return;
    }
    setEditMode(false);
    setSelectedWidgetId(null);
  }

  // Discard unsaved edits: revert to the last persisted layout/filters, exit.
  function handleDiscardConfirm() {
    setDiscardOpen(false);
    setLayout(savedRef.current.layout);
    setCanvasFilters(savedRef.current.filters);
    setDirty(false);
    setSelectedWidgetId(null);
    setEditMode(false);
  }

  // Memoize the widget list for the mobile stack.
  const orderedWidgets = useMemo(
    () => orderWidgetsForStack(layout.widgets),
    [layout.widgets],
  );

  // ── Loading state ──────────────────────────────────────────────
  // `!user` holds the loader until auth resolves, so the loaded branch (which
  // mounts DashboardDataProvider + the Canvas filter-chip SWR) never renders
  // token-less. The data-provider/SWR fetches thus always carry a bearer.
  if (loading || !authReady) {
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
        <div
          data-testid="custom-dashboard"
          className="flex h-full flex-col"
        >
          {/* Header */}
          <div className="mb-6 flex items-center justify-between">
            <TourAnchor id="dashboard.header" as="child">
              <h1 className={`${pageTitle} mb-0`}>Dashboard</h1>
            </TourAnchor>
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
                  className={btnCanvas}
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
                  className={btnCanvas}
                >
                  Reset to default
                </button>
              )}
              {/* Customize / Done toggle — desktop-only */}
              {!isSmallScreen && (
                <TourAnchor id="dashboard.customize" as="child">
                  <button
                    type="button"
                    data-testid="custom-dashboard-customize"
                    onClick={() => (editMode ? leaveCustomize() : setEditMode(true))}
                    className={editMode ? btnCanvasActive : btnCanvas}
                  >
                    {editMode ? "Done" : "Customize"}
                  </button>
                </TourAnchor>
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
                // Apply the tour anchor to the existing height-constrained
                // wrapper via as="child" (cloneElement preserves the key), so
                // no extra element enters the fixed-height stack.
                return withTileTourAnchor(
                  w.type,
                  <div key={w.id} style={h ? { height: h } : undefined}>
                    {renderDashboardWidget(
                      w as DashboardWidget | Widget,
                      canvasFilters,
                      false,
                      currency,
                    )}
                  </div>,
                );
              })}
            </div>
          ) : (
            <Canvas
              layout={layout}
              editMode={editModeActive}
              compact
              onLayoutChange={updateLayout}
              renderWidget={(w) =>
                // h-full is load-bearing: react-grid-layout sizes the grid item
                // to a fixed pixel box (h*rowHeight + margins); WidgetShell uses
                // `h-full` to fill it. This wrapper sits between the two (it
                // carries the test id), so without `h-full` here the height chain
                // breaks — tiles collapse to content height inside a taller box,
                // floating the resize handle and overflowing neighbours. Reports
                // has no such wrapper (WidgetShell is the grid item's direct child).
                // withTileTourAnchor injects the finance-tile tour anchor's
                // data-tour-id onto this wrapper (no extra element) for the
                // first-run tour; non-tour tiles pass through unchanged.
                withTileTourAnchor(
                  w.type,
                  <div data-testid={`widget-${w.type}`} className="h-full">
                    <WidgetShell
                      widgetId={w.id}
                      selected={selectedWidgetId === w.id}
                      editMode={editModeActive}
                      onSelect={() => setSelectedWidgetId(w.id)}
                      widget={w}
                      canvasFilters={canvasFilters}
                      accounts={accounts}
                      categories={categories}
                      onRemove={() => removeWidget(w.id)}
                      onSelectFilters={() => {}}
                    >
                      {renderDashboardWidget(
                        w as DashboardWidget | Widget,
                        canvasFilters,
                        editModeActive,
                        currency,
                      )}
                    </WidgetShell>
                  </div>,
                )
              }
            />
          )}

          {/* Add-widget picker — rendered when editModeActive */}
          <AddWidgetMenu
            open={pickerOpen}
            onClose={() => setPickerOpen(false)}
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

          {/* Unsaved-changes guard when leaving Customize via Done */}
          <ConfirmModal
            open={discardOpen}
            title="Discard unsaved changes?"
            message="You have unsaved changes to your dashboard layout. Discard them and leave Customize mode, or keep editing to Save."
            confirmLabel="Discard changes"
            cancelLabel="Keep editing"
            variant="warning"
            onConfirm={handleDiscardConfirm}
            onCancel={() => setDiscardOpen(false)}
          />
        </div>
      </DashboardDataProvider>
    </AppShell>
  );
}
