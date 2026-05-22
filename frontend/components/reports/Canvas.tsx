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
import { useMemo } from "react";
// react-grid-layout ships its CSS as a separate import. Loading the
// stylesheet at the canvas module keeps the dep colocated with the
// only consumer, so removing Reports doesn't leave a dangling import
// in app/layout.tsx.
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";

import { Responsive, WidthProvider, type Layout } from "react-grid-layout";

import type { LayoutJson, Widget } from "@/lib/reports/types";

const ResponsiveGridLayout = WidthProvider(Responsive);

interface Props {
  layout: LayoutJson;
  editMode: boolean;
  onLayoutChange: (next: LayoutJson) => void;
  /** Renders the body of each widget. The shell + chrome is up to the
   * caller (so the editor can wrap with WidgetShell for click-to-select).
   */
  renderWidget: (widget: Widget) => React.ReactNode;
}

const COLS = { lg: 12, md: 12, sm: 6, xs: 1, xxs: 1 } as const;
const BREAKPOINTS = { lg: 1200, md: 996, sm: 768, xs: 480, xxs: 0 } as const;

export default function Canvas({
  layout,
  editMode,
  onLayoutChange,
  renderWidget,
}: Props) {
  const items = layout.widgets ?? [];
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
    const byId = new Map(next.map((l) => [l.i, l]));
    const updated: Widget[] = items.map((w) => {
      const l = byId.get(w.id);
      if (!l) return w;
      return {
        ...w,
        grid: { x: l.x, y: l.y, w: l.w, h: l.h },
      };
    });
    onLayoutChange({ ...layout, widgets: updated });
  }

  return (
    <div data-testid="reports-canvas" className="w-full">
      <ResponsiveGridLayout
        className="layout"
        layouts={{ lg: rgLayout, md: rgLayout, sm: rgLayout, xs: rgLayout, xxs: rgLayout }}
        breakpoints={BREAKPOINTS}
        cols={COLS}
        rowHeight={60}
        isDraggable={editMode}
        isResizable={editMode}
        draggableHandle="[data-grid-drag-handle]"
        onLayoutChange={handleLayoutChange}
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
