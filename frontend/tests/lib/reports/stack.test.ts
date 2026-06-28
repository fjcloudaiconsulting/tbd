/**
 * Tests for mobile single-column stack helpers (stack.ts).
 */
import { mobileStackHeight, orderWidgetsForStack } from "@/lib/reports/stack";
import type { Widget, WidgetType } from "@/lib/reports/types";
import type { DashboardWidget, DashboardWidgetType } from "@/lib/dashboard/widget-types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeWidget(type: string, gridH = 5): Widget | DashboardWidget {
  return {
    id: "x",
    type: type as WidgetType | DashboardWidgetType,
    title: "t",
    grid: { x: 0, y: 0, w: 12, h: gridH },
    config: {},
  } as unknown as Widget | DashboardWidget;
}

// ---------------------------------------------------------------------------
// orderWidgetsForStack
// ---------------------------------------------------------------------------

describe("orderWidgetsForStack", () => {
  it("sorts by y then x", () => {
    const makeBar = (id: string, x: number, y: number): Widget =>
      ({
        id,
        type: "bar" as WidgetType,
        title: id,
        grid: { x, y, w: 4, h: 3 },
        config: {},
      }) as unknown as Widget;
    const widgets: Widget[] = [
      makeBar("c", 0, 2),
      makeBar("a", 0, 0),
      makeBar("b", 4, 0),
    ];
    const sorted = orderWidgetsForStack(widgets);
    expect(sorted.map((w) => w.id)).toEqual(["a", "b", "c"]);
  });

  it("accepts DashboardWidget without error", () => {
    const dw: DashboardWidget = {
      id: "d",
      type: "dash_on_track",
      title: "On Track",
      grid: { x: 0, y: 0, w: 12, h: 3 },
      config: {},
    };
    expect(() => orderWidgetsForStack([dw])).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// mobileStackHeight — report widgets (existing behaviour, must not regress)
// ---------------------------------------------------------------------------

describe("mobileStackHeight — report widgets", () => {
  it("returns undefined for kpi (content widget)", () => {
    expect(mobileStackHeight(makeWidget("kpi"))).toBeUndefined();
  });

  it("returns undefined for table (content widget)", () => {
    expect(mobileStackHeight(makeWidget("table"))).toBeUndefined();
  });

  it("returns a number in [220, 460] for chart widgets", () => {
    const chartTypes = ["bar", "stacked_bar", "line", "area", "pie", "sparkline", "sankey"];
    for (const type of chartTypes) {
      const h = mobileStackHeight(makeWidget(type, 5));
      expect(typeof h).toBe("number");
      expect(h).toBeGreaterThanOrEqual(220);
      expect(h).toBeLessThanOrEqual(460);
    }
  });

  it("clamps chart height to 220 minimum for very short grid rows", () => {
    expect(mobileStackHeight(makeWidget("bar", 1))).toBeGreaterThanOrEqual(220);
  });

  it("clamps chart height to 460 maximum for very tall grid rows", () => {
    expect(mobileStackHeight(makeWidget("bar", 20))).toBeLessThanOrEqual(460);
  });
});

// ---------------------------------------------------------------------------
// mobileStackHeight — dash_* widgets (new behaviour)
// ---------------------------------------------------------------------------

describe("mobileStackHeight — dash_* widgets", () => {
  const h = (type: string) =>
    mobileStackHeight(makeWidget(type, 5));

  // Content tiles (lists, summary cards, the transaction table) size to their
  // OWN content on mobile — exactly like the report `kpi`/`table` widgets.
  // A fixed clamp + the cards' `overflow-hidden` was cutting tall content
  // (e.g. a long accounts list clipped mid-row). Natural height shows it all.
  it("dash_on_track returns undefined (natural content height)", () => {
    expect(h("dash_on_track")).toBeUndefined();
  });

  it("dash_accounts returns undefined (natural content height)", () => {
    expect(h("dash_accounts")).toBeUndefined();
  });

  it("dash_account_forecast returns undefined (natural content height)", () => {
    expect(h("dash_account_forecast")).toBeUndefined();
  });

  it("dash_recent_transactions returns undefined (natural content height)", () => {
    expect(h("dash_recent_transactions")).toBeUndefined();
  });

  // Chart tiles still need a definite height for Recharts/Nivo height="100%".
  it("dash_spending (chart tile) gets >= 220", () => {
    expect(h("dash_spending")).toBeGreaterThanOrEqual(220);
  });

  it("dash_budget (chart tile) gets >= 220", () => {
    expect(h("dash_budget")).toBeGreaterThanOrEqual(220);
  });

  it("dash_forecast_category (chart tile) gets >= 220", () => {
    expect(h("dash_forecast_category")).toBeGreaterThanOrEqual(220);
  });

  it("the 3 dash_* CHART tiles return a number; the 4 CONTENT tiles return undefined", () => {
    for (const type of ["dash_spending", "dash_budget", "dash_forecast_category"]) {
      expect(typeof h(type)).toBe("number");
    }
    for (const type of [
      "dash_on_track",
      "dash_accounts",
      "dash_account_forecast",
      "dash_recent_transactions",
    ]) {
      expect(h(type)).toBeUndefined();
    }
  });
});
