/**
 * Locks the move of the widget-config control constants out of ConfigRail.
 */
import {
  AGG_OPTIONS,
  DIMENSION_OPTIONS,
  MAX_SERIES,
  MAX_TABLE_COLUMNS,
} from "@/components/reports/config/controlConstants";

describe("widget-config control constants", () => {
  it("exposes nine dimension options", () => {
    expect(DIMENSION_OPTIONS.length).toBe(9);
  });

  it("exposes four aggregation options", () => {
    expect(AGG_OPTIONS.length).toBe(4);
  });

  it("caps series and table columns at five", () => {
    expect(MAX_SERIES).toBe(5);
    expect(MAX_TABLE_COLUMNS).toBe(5);
  });
});
