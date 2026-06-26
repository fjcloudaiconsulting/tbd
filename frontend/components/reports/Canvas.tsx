"use client";

/**
 * Reports v2 canvas substrate.
 *
 * Wraps ``react-grid-layout``'s ``Responsive`` + ``WidthProvider``
 * combo to give a 12-column snap grid with drag + resize handles in
 * edit mode. On the smallest breakpoint the grid collapses to a
 * single column and resize handles are hidden (read-only mobile per
 * spec §1 "Mobile fallback").
 *
 * The grid library renders its own DOM tree; widget contents are
 * passed in as children keyed by widget id so layout JSON ↔ render
 * order stay coupled.
 */
import { useMemo, useRef } from "react";
// react-grid-layout ships its CSS as a separate import. Loading the
// stylesheet at the canvas module keeps the dep colocated with the
// only consumer, so removing Reports doesn't leave a dangling import
// in app/layout.tsx.
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

import { Responsive, WidthProvider, type Layout } from "react-grid-layout";

import type { LayoutJson, Widget } from "@/lib/reports/types";
import { widgetsFromLayout, gridChanged } from "@/lib/reports/layout";

const ResponsiveGridLayout = WidthProvider(Responsive);

interface Props {
  layout: LayoutJson;
  editMode: boolean;
  onLayoutChange: (next: LayoutJson) => void;
  /** Renders the body of each widget. The shell + chrome is up to the
   * caller (so the editor can wrap with WidgetShell for click-to-select).
   */
  renderWidget: (widget: Widget) => React.ReactNode;
  /**
   * Placement mode.
   *  - ``false`` (default): literal placement — widgets never auto-compact
   *    and a drag/resize into an occupied cell snaps back, honouring authored
   *    position/size verbatim (Reports v3 #442).
   *  - ``true``: vertical compaction + collision displacement — dragging a tile
   *    over another pushes it aside and tiles float up to fill gaps
   *    (phone-style rearrange). Used by the dashboard.
   */
  compact?: boolean;
}

// The editor canvas is desktop-only — true mobile widths render the
// read-only stack in the page, never this grid. So sm/xs/xxs are only
// reached in the 640–767px "narrow desktop / settings-panel-open" gap.
// Their exact column counts only affect how a narrow *view* reflows;
// editing never persists from them (handleLayoutChange gates on a 12-col
// breakpoint), so a clamped sm/xs layout can never corrupt the stored one.
const COLS = { lg: 12, md: 12, sm: 6, xs: 1, xxs: 1 } as const;
const BREAKPOINTS = { lg: 1200, md: 996, sm: 768, xs: 480, xxs: 0 } as const;

export default function Canvas({
  layout,
  editMode,
  onLayoutChange,
  renderWidget,
  compact = false,
}: Props) {
  const items = layout.widgets ?? [];
  // The stored layout is the 12-column (lg/md) layout. At narrower
  // breakpoints react-grid-layout clamps widget widths to fit fewer
  // columns (e.g. a w=12 table → w=6 at sm); those clamped layouts are
  // responsive *views*, not edits. Persisting one back would corrupt the
  // canonical layout (w=12 → w=6) and falsely mark the report dirty on
  // load whenever the canvas renders below 12 cols (e.g. the settings
  // panel narrows it on a laptop). Tracked in a ref, not state, because
  // RGL fires onBreakpointChange synchronously *before* onLayoutChange in
  // the same width-change cycle — a ref reads fresh within that tick
  // where state would still be stale.
  const colsRef = useRef<number>(COLS.lg);
  const rgLayout = useMemo<Layout[]>(
    () =>
      items.map((w) => ({
        i: w.id,
        x: w.grid.x,
        y: w.grid.y,
        w: w.grid.w,
        h: w.grid.h,
        minW: 2,
        minH: 2,
      })),
    [items],
  );

  function handleLayoutChange(next: Layout[]) {
    if (!editMode) return;
    // Only persist edits made at a full-width (12-col) breakpoint; a
    // narrower breakpoint's clamped layout is a view, not an edit.
    if (colsRef.current < COLS.lg) return;
    const updated = widgetsFromLayout(items, next);
    if (!gridChanged(items, updated)) return; // swallow mount-time / no-op emissions
    onLayoutChange({ ...layout, widgets: updated });
  }

  return (
    <div data-testid="reports-canvas" className="w-full">
      <ResponsiveGridLayout
        className="layout"
        // Pre-seed every breakpoint with the same layout so RGL never
        // falls through to findOrGenerateResponsiveLayout, which would run
        // compact() and emit a spurious onLayoutChange.
        layouts={{ lg: rgLayout, md: rgLayout, sm: rgLayout, xs: rgLayout, xxs: rgLayout }}
        breakpoints={BREAKPOINTS}
        cols={COLS}
        rowHeight={60}
        // Placement mode (see the `compact` prop):
        //  - compact: vertical compaction + collision displacement — dragging a
        //    tile over another pushes it aside and tiles float up (phone-style).
        //  - literal (default): widgets never auto-compact and a drag into an
        //    occupied cell snaps back to the last valid position, honouring the
        //    authored position/size verbatim (Reports v3 #442).
        compactType={compact ? "vertical" : null}
        preventCollision={!compact}
        isDraggable={editMode}
        isResizable={editMode}
        draggableHandle="[data-grid-drag-handle]"
        onLayoutChange={handleLayoutChange}
        onBreakpointChange={(_breakpoint, newCols) => {
          colsRef.current = newCols;
        }}
        margin={[12, 12]}
      >
        {items.map((w) => (
          <div key={w.id} data-widget-id={w.id}>
            {renderWidget(w)}
          </div>
        ))}
      </ResponsiveGridLayout>
    </div>
  );
}
