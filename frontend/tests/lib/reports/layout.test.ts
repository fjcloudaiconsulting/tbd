import { describe, it, expect } from "vitest";
import { widgetsFromLayout, gridChanged } from "@/lib/reports/layout";
import type { Widget } from "@/lib/reports/types";

const w = (id: string, x: number, y: number, ww = 4, h = 4): Widget =>
  ({ id, type: "kpi", title: id, grid: { x, y, w: ww, h }, config: {} } as unknown as Widget);

describe("widgetsFromLayout", () => {
  it("applies new x/y/w/h from the rgl items, keyed by id", () => {
    const items = [w("a", 0, 0), w("b", 0, 4)];
    const next = [
      { i: "a", x: 2, y: 0, w: 4, h: 4 },
      { i: "b", x: 0, y: 4, w: 6, h: 5 },
    ];
    const out = widgetsFromLayout(items, next);
    expect(out[0].grid).toEqual({ x: 2, y: 0, w: 4, h: 4 });
    expect(out[1].grid).toEqual({ x: 0, y: 4, w: 6, h: 5 });
  });

  it("leaves a widget untouched when no rgl item matches its id", () => {
    const items = [w("a", 0, 0)];
    const out = widgetsFromLayout(items, [{ i: "ghost", x: 9, y: 9, w: 1, h: 1 }]);
    expect(out[0].grid).toEqual({ x: 0, y: 0, w: 4, h: 4 });
  });
});

describe("gridChanged", () => {
  it("is false when every widget's grid is identical (mount / no-op emission)", () => {
    const items = [w("a", 0, 0), w("b", 0, 4)];
    expect(gridChanged(items, widgetsFromLayout(items, [
      { i: "a", x: 0, y: 0, w: 4, h: 4 },
      { i: "b", x: 0, y: 4, w: 4, h: 4 },
    ]))).toBe(false);
  });

  it("is true when any x/y/w/h moved (real drag/resize)", () => {
    const items = [w("a", 0, 0)];
    expect(gridChanged(items, [{ ...items[0], grid: { x: 1, y: 0, w: 4, h: 4 } } as Widget])).toBe(true);
  });
});
