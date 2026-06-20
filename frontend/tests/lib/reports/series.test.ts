import { describe, it, expect } from "vitest";

import {
  MEASURE_FIELD_LABELS,
  measureFieldLabel,
  seriesLabel,
} from "@/lib/reports/series";
import type { MeasureField, SeriesConfig } from "@/lib/reports/types";

describe("measureFieldLabel", () => {
  it("maps every raw measure field to a friendly label (no bare keys)", () => {
    const fields: MeasureField[] = [
      "amount",
      "id",
      "category_id",
      "account_id",
    ];
    for (const f of fields) {
      const label = measureFieldLabel(f);
      // Never surface the raw key or an *_id suffix to the user.
      expect(label).toBe(MEASURE_FIELD_LABELS[f]);
      expect(label).not.toBe(f);
      expect(label).not.toMatch(/_id$/);
    }
  });

  it("uses the expected human labels", () => {
    expect(measureFieldLabel("amount")).toBe("Amount");
    expect(measureFieldLabel("id")).toBe("Row count");
    expect(measureFieldLabel("category_id")).toBe("Category");
    expect(measureFieldLabel("account_id")).toBe("Account");
  });
});

describe("seriesLabel", () => {
  const s = (over: Partial<SeriesConfig> = {}): SeriesConfig => ({
    measure: { agg: "sum", field: "amount" },
    ...over,
  });

  it("single-series shows the humanized field, not the raw key", () => {
    expect(seriesLabel(s(), 0, 1)).toBe("Amount");
    expect(seriesLabel(s({ measure: { agg: "count", field: "id" } }), 0, 1)).toBe(
      "Row count",
    );
  });

  it("multi-series shows '<Agg> of <FriendlyField>'", () => {
    expect(seriesLabel(s(), 0, 2)).toBe("Sum of Amount");
    expect(
      seriesLabel(s({ measure: { agg: "avg", field: "account_id" } }), 1, 2),
    ).toBe("Average of Account");
  });

  it("labels distinct as 'Distinct count' to match the editor picker", () => {
    expect(
      seriesLabel(s({ measure: { agg: "distinct", field: "account_id" } }), 0, 2),
    ).toBe("Distinct count of Account");
  });

  it("an explicit label override always wins", () => {
    expect(seriesLabel(s({ label: "Total spend" }), 0, 1)).toBe("Total spend");
    expect(seriesLabel(s({ label: "  Total spend  " }), 0, 2)).toBe(
      "Total spend",
    );
  });
});
