import type { Widget } from "@/lib/reports/types";

/** Minimal shape of a react-grid-layout item (the fields we read). */
export interface RglItem {
  i: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Apply react-grid-layout positions back onto widgets, keyed by id.
 *  Widgets with no matching rgl item are returned unchanged. */
export function widgetsFromLayout(items: Widget[], rglItems: RglItem[]): Widget[] {
  const byId = new Map(rglItems.map((l) => [l.i, l]));
  return items.map((wgt) => {
    const l = byId.get(wgt.id);
    if (!l) return wgt;
    return { ...wgt, grid: { x: l.x, y: l.y, w: l.w, h: l.h } };
  });
}

/** True only when at least one widget's grid x/y/w/h actually differs.
 *  Used to ignore react-grid-layout's mount-time and no-op emissions so
 *  loading a report does not spuriously mark the editor dirty. */
export function gridChanged(prev: Widget[], next: Widget[]): boolean {
  if (prev.length !== next.length) return true;
  const byId = new Map(prev.map((p) => [p.id, p.grid]));
  for (const n of next) {
    const g = byId.get(n.id);
    if (!g) return true;
    if (g.x !== n.grid.x || g.y !== n.grid.y || g.w !== n.grid.w || g.h !== n.grid.h) {
      return true;
    }
  }
  return false;
}
