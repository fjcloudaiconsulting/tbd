/**
 * Locks the move of the widget-config control constants out of ConfigRail.
 */
import {
  AGG_OPTIONS,
  AGG_HELP_KEY,
  DIMENSION_OPTIONS,
  MAX_SERIES,
  MAX_TABLE_COLUMNS,
  isMultiSeries,
  isSingleAggLocked,
} from "@/components/reports/config/controlConstants";
import type { Widget } from "@/lib/reports/types";

describe("widget-config control constants", () => {
  it("exposes the four aggregation values sum/count/avg/distinct", () => {
    expect(AGG_OPTIONS.map((o) => o.value)).toEqual([
      "sum",
      "count",
      "avg",
      "distinct",
    ]);
  });

  it("maps every aggregation value to a help-tooltip key", () => {
    for (const { value } of AGG_OPTIONS) {
      expect(AGG_HELP_KEY[value]).toBe(`reports.agg.${value}`);
    }
  });

  it("exposes the nine expected dimension keys in order", () => {
    expect(DIMENSION_OPTIONS.map((o) => o.value)).toEqual([
      "category",
      "category_master",
      "account",
      "tag",
      "txn_type",
      "status",
      "month",
      "week",
      "day",
    ]);
  });

  it("caps series and table columns at five", () => {
    expect(MAX_SERIES).toBe(5);
    expect(MAX_TABLE_COLUMNS).toBe(5);
  });

  it("isMultiSeries is true only for line/area/stacked_bar/table", () => {
    const base = { id: "w", title: "", grid: { x: 0, y: 0, w: 1, h: 1 } };
    const multi = ["line", "area", "stacked_bar", "table"];
    const single = ["bar", "kpi", "pie", "sparkline"];
    for (const type of multi) {
      expect(isMultiSeries({ ...base, type } as unknown as Widget)).toBe(true);
    }
    for (const type of single) {
      expect(isMultiSeries({ ...base, type } as unknown as Widget)).toBe(false);
    }
  });

  it("isSingleAggLocked is true only for pie/sparkline", () => {
    const base = { id: "w", title: "", grid: { x: 0, y: 0, w: 1, h: 1 } };
    expect(
      isSingleAggLocked({ ...base, type: "pie" } as unknown as Widget),
    ).toBe(true);
    expect(
      isSingleAggLocked({ ...base, type: "sparkline" } as unknown as Widget),
    ).toBe(true);
    expect(
      isSingleAggLocked({ ...base, type: "bar" } as unknown as Widget),
    ).toBe(false);
  });
});
