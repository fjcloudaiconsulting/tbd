import type { Layout } from "react-grid-layout";

import type { Widget } from "@/lib/reports/types";

/** The react-grid-layout item fields we read — a subset of RGL's `Layout`,
 *  reused rather than re-declared so it can't drift from the library type. */
export type RglItem = Pick<Layout, "i" | "x" | "y" | "w" | "h">;

/** Apply react-grid-layout positions back onto widgets, keyed by id.
 *  Always returns exactly `items.length` widgets (it maps over `items`);
 *  a widget with no matching rgl item is returned unchanged, and an rgl
 *  item with no matching widget is ignored. */
export function widgetsFromLayout(items: Widget[], rglItems: RglItem[]): Widget[] {
  const byId = new Map(rglItems.map((l) => [l.i, l]));
  return items.map((wgt) => {
    const l = byId.get(wgt.id);
    if (!l) return wgt;
    return { ...wgt, grid: { x: l.x, y: l.y, w: l.w, h: l.h } };
  });
}

/** True when the set of widgets (by id) changed OR any widget's grid
 *  x/y/w/h differs. Used to ignore react-grid-layout's mount-time and
 *  no-op emissions so loading a report does not spuriously mark the
 *  editor dirty. On the canvas path `next` is always
 *  `widgetsFromLayout(items, …)`, so in practice only real drag/resize
 *  deltas reach the field comparison; the length/id guards are defensive
 *  for any other caller. */
export function gridChanged(prev: Widget[], next: Widget[]): boolean {
  if (prev.length !== next.length) return true;
  const byId = new Map(prev.map((p) => [p.id, p.grid]));
  for (const n of next) {
    const g = byId.get(n.id);
    // Defensive: widgetsFromLayout preserves every id from `prev`, so a
    // miss only happens if gridChanged is called with a `next` built
    // elsewhere — treat an unknown id as a change.
    if (!g) return true;
    if (g.x !== n.grid.x || g.y !== n.grid.y || g.w !== n.grid.w || g.h !== n.grid.h) {
      return true;
    }
  }
  return false;
}
