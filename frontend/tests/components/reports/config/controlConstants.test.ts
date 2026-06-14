/**
 * Locks the move of the widget-config control constants out of the
 * original config rail and into the shared module.
 */
import {
  AGG_OPTIONS,
  AGG_HELP_KEY,
  DIMENSION_OPTIONS,
  MAX_SERIES,
  MAX_TABLE_COLUMNS,
  isMultiSeries,
  isSingleAggLocked,
  measureFieldOptionsFor,
} from "@/components/reports/config/controlConstants";
import type { SourceCatalogEntry, Widget } from "@/lib/reports/types";

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

  it("measureFieldOptionsFor returns distinct fields in catalog order with labels", () => {
    const accounts: SourceCatalogEntry = {
      key: "accounts",
      label: "Accounts",
      dimensions: [],
      measures: [
        { key: "sum_balance", label: "Sum of balance", agg: "sum", field: "balance", format: "currency" },
        { key: "avg_balance", label: "Average balance", agg: "avg", field: "balance", format: "currency" },
        { key: "count_accounts", label: "Account count", agg: "count", field: "id", format: "number" },
      ],
      filters: [],
    };
    // balance appears twice in the catalog (sum + avg) → one option;
    // labels come from MEASURE_FIELD_LABELS ("Balance"/"Row count").
    expect(measureFieldOptionsFor(accounts)).toEqual([
      { value: "balance", label: "Balance" },
      { value: "id", label: "Row count" },
    ]);
  });

  it("measureFieldOptionsFor falls back to the raw field for unknown keys", () => {
    const entry: SourceCatalogEntry = {
      key: "mystery",
      label: "Mystery",
      dimensions: [],
      measures: [
        { key: "k", label: "K", agg: "sum", field: "unknown_field", format: "number" },
      ],
      filters: [],
    };
    expect(measureFieldOptionsFor(entry)).toEqual([
      { value: "unknown_field", label: "unknown_field" },
    ]);
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
