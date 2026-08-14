/**
 * TBD-381 — render-time format derivation.
 *
 * The kernel these fences pin: format is a pure function of (measure, catalog),
 * never persisted state. Each test names the wrong implementation it kills;
 * where a case exists specifically because the OLD mutation-time resolver got
 * it wrong, that is stated, because those are the ones that would otherwise
 * pass against unmodified `main`.
 */
import { describe, expect, it } from "vitest";

import type { SourceCatalogEntry } from "@/lib/reports/types";
import {
  entryFor,
  formatForMeasure,
  sharedFormatFor,
} from "@/lib/reports/widget-format";

/** Mirrors the real catalog shape: a measure row is (key, label, agg, field, format). */
const TRANSACTIONS: SourceCatalogEntry = {
  key: "transactions",
  label: "Transactions",
  dimensions: [],
  filters: [],
  measures: [
    { key: "amount_sum", label: "Total", agg: "sum", field: "amount", format: "currency" },
    { key: "amount_avg", label: "Average", agg: "avg", field: "amount", format: "currency" },
    { key: "txn_count", label: "Count", agg: "count", field: "id", format: "number" },
  ],
};

/** The source that made this bug visible: the first `percent` measure. */
const CREDIT_UTILIZATION: SourceCatalogEntry = {
  key: "credit_utilization",
  label: "Credit utilization",
  dimensions: [],
  filters: [],
  measures: [
    {
      key: "utilization_avg",
      label: "Utilization",
      agg: "avg",
      field: "utilization_pct",
      format: "percent",
    },
    {
      key: "outstanding_sum",
      label: "Outstanding",
      agg: "sum",
      field: "outstanding",
      format: "currency",
    },
  ],
};

const CATALOG = [TRANSACTIONS, CREDIT_UTILIZATION];

describe("formatForMeasure", () => {
  it("resolves the exact (agg, field) pair", () => {
    expect(
      formatForMeasure(CREDIT_UTILIZATION, { agg: "avg", field: "utilization_pct" }),
    ).toBe("percent");
    expect(
      formatForMeasure(CREDIT_UTILIZATION, { agg: "sum", field: "outstanding" }),
    ).toBe("currency");
  });

  it("is the whole bug: a percent measure must not render as currency", () => {
    // KILLS: reading a persisted `config.format`. Every saved widget carries a
    // hardcoded "currency" (widgetKit, draft.ts and 14 templates all seed it),
    // so a credit_utilization widget renders 45% as "€45.00" under any design
    // that treats stored format as an input.
    expect(
      formatForMeasure(CREDIT_UTILIZATION, { agg: "avg", field: "utilization_pct" }),
    ).not.toBe("currency");
  });

  it("treats a cardinality as a number even when the field is a currency field", () => {
    // KILLS: the FIELD-ONLY lookup the old mutation-time resolver used, which
    // `useWidgetMutations.ts:41` documented as deliberate. `count(amount)`
    // matches the `amount` row and inherits "currency" under field-only
    // matching -- a count of transactions rendered as "€1,234.00".
    expect(formatForMeasure(TRANSACTIONS, { agg: "count", field: "amount" })).toBe(
      "number",
    );
    expect(
      formatForMeasure(TRANSACTIONS, { agg: "distinct", field: "amount" }),
    ).toBe("number");
  });

  it("falls back to the field when the exact pair is unpublished", () => {
    // `sum(utilization_pct)` is not published (only avg is), but the field's
    // format is unambiguous, so the backstop is safe rather than a guess.
    expect(
      formatForMeasure(CREDIT_UTILIZATION, { agg: "sum", field: "utilization_pct" }),
    ).toBe("percent");
  });

  it("returns undefined without a catalog, so callers can hold their skeleton", () => {
    // KILLS: defaulting to "number" while the catalog loads, which would flash
    // an unformatted value and then flip once /sources lands.
    expect(formatForMeasure(undefined, { agg: "sum", field: "amount" })).toBeUndefined();
  });

  it("falls to number for a measure the source does not publish at all", () => {
    expect(
      formatForMeasure(CREDIT_UTILIZATION, { agg: "sum", field: "amount" }),
    ).toBe("number");
  });
});

describe("sharedFormatFor", () => {
  it("uses the common format when every series agrees", () => {
    expect(
      sharedFormatFor(TRANSACTIONS, [
        { agg: "sum", field: "amount" },
        { agg: "avg", field: "amount" },
      ]),
    ).toBe("currency");
  });

  it("falls to number when series disagree, rather than mislabelling the axis", () => {
    // KILLS: taking measures[0]'s format for a shared Y axis. These charts pass
    // ONE format into ONE Recharts tickFormatter, so stamping "€" on the ticks
    // asserts a unit the percent series does not have -- it does not merely
    // under-serve series 2, it mislabels it.
    expect(
      sharedFormatFor(CREDIT_UTILIZATION, [
        { agg: "sum", field: "outstanding" },
        { agg: "avg", field: "utilization_pct" },
      ]),
    ).toBe("number");
  });

  it("is undefined with no catalog", () => {
    expect(sharedFormatFor(undefined, [{ agg: "sum", field: "amount" }])).toBeUndefined();
  });
});

describe("entryFor", () => {
  it("finds a dataset and misses cleanly", () => {
    expect(entryFor(CATALOG, "credit_utilization")?.key).toBe("credit_utilization");
    expect(entryFor(CATALOG, "nope")).toBeUndefined();
    expect(entryFor(CATALOG, undefined)).toBeUndefined();
  });
});
