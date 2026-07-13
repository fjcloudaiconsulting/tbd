import { describe, expect, it } from "vitest";

import { buildQueryAst } from "@/lib/reports/useReportQuery";
import {
  sourceSupportsDateFilter,
  sourceSupportsStatusFilter,
} from "@/lib/reports/resolve";
import type {
  BarWidget,
  CanvasFilters,
  SourceCatalogEntry,
} from "@/lib/reports/types";

/**
 * Task 7 (Reports v3 Phase 5): the resolver must stamp the widget's
 * ``dataset`` onto the AST and OMIT the shared canvas date filter for
 * sources that don't publish a ``date`` filter field.
 *
 * The new ``accounts`` source has no ``date`` column, so cascading the
 * canvas date range onto it is meaningless. The backend already drops a
 * stray date filter on accounts, but the frontend shouldn't send one.
 *
 * A source supports a date filter iff its catalog ``filters`` list
 * includes one with ``field === "date"`` — ``transactions`` does,
 * ``accounts`` does not.
 */

const CANVAS_DATE: CanvasFilters = {
  date_range: { start: "2026-01-01", end: "2026-01-31" },
};

const ACCOUNTS_SOURCE: SourceCatalogEntry = {
  key: "accounts",
  label: "Accounts",
  dimensions: [
    { key: "account_type", label: "Account type", kind: "category" },
  ],
  measures: [
    {
      key: "balance",
      label: "Balance",
      agg: "sum",
      field: "balance",
      format: "currency",
    },
  ],
  // No ``date`` filter — accounts has no date column.
  filters: [],
};

const TRANSACTIONS_SOURCE: SourceCatalogEntry = {
  key: "transactions",
  label: "Transactions",
  dimensions: [{ key: "category", label: "Category", kind: "category" }],
  measures: [
    {
      key: "amount_sum",
      label: "Amount",
      agg: "sum",
      field: "amount",
      format: "currency",
    },
  ],
  filters: [
    { field: "date", label: "Date", ops: ["between", "gte", "lte"], kind: "date" },
    { field: "status", label: "Status", ops: ["eq"], kind: "status" },
  ],
};

const RECURRING_SOURCE: SourceCatalogEntry = {
  key: "recurring",
  label: "Recurring",
  dimensions: [
    { key: "category", label: "Category", kind: "category" },
    { key: "frequency", label: "Frequency", kind: "category" },
  ],
  measures: [
    {
      key: "sum_amount",
      label: "Sum of amount",
      agg: "sum",
      field: "amount",
      format: "currency",
    },
  ],
  // No ``date`` filter — recurring templates have no transaction date.
  filters: [
    { field: "amount", label: "Amount", ops: ["between"], kind: "amount" },
  ],
};

const SOURCES: SourceCatalogEntry[] = [
  ACCOUNTS_SOURCE,
  TRANSACTIONS_SOURCE,
  RECURRING_SOURCE,
];

function accountsBarWidget(): BarWidget {
  return {
    id: "w-accounts",
    type: "bar",
    title: "Balance by account type",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "accounts",
      measure: { agg: "sum", field: "balance" },
      dimensions: ["account_type"],
    },
  };
}

function recurringBarWidget(): BarWidget {
  return {
    id: "w-recurring",
    type: "bar",
    title: "Recurring spend by category",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "recurring",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
    },
  };
}

function transactionsBarWidget(): BarWidget {
  return {
    id: "w-transactions",
    type: "bar",
    title: "Spend by category",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
    },
  };
}

describe("sourceSupportsDateFilter", () => {
  it("is false for a source whose filters omit a date field", () => {
    expect(sourceSupportsDateFilter(SOURCES, "accounts")).toBe(false);
  });

  it("is true for a source whose filters include a date field", () => {
    expect(sourceSupportsDateFilter(SOURCES, "transactions")).toBe(true);
  });

  it("is false for the recurring source (no date filter)", () => {
    expect(sourceSupportsDateFilter(SOURCES, "recurring")).toBe(false);
  });

  it("defaults to true when the catalog is empty (pre-load)", () => {
    expect(sourceSupportsDateFilter([], "transactions")).toBe(true);
    expect(sourceSupportsDateFilter([], "accounts")).toBe(true);
  });

  it("defaults to true when the source is not in the catalog", () => {
    expect(sourceSupportsDateFilter([TRANSACTIONS_SOURCE], "accounts")).toBe(
      true,
    );
  });
});

describe("buildQueryAst — dataset stamping + date-less source", () => {
  it("stamps dataset='accounts' and OMITS the canvas date filter", () => {
    const ast = buildQueryAst(
      accountsBarWidget(),
      CANVAS_DATE,
      sourceSupportsDateFilter(SOURCES, "accounts"),
    );
    expect(ast.dataset).toBe("accounts");
    expect(ast.filters.some((f) => f.field === "date")).toBe(false);
  });

  it("stamps dataset='recurring' and OMITS the canvas date filter", () => {
    const ast = buildQueryAst(
      recurringBarWidget(),
      CANVAS_DATE,
      sourceSupportsDateFilter(SOURCES, "recurring"),
    );
    expect(ast.dataset).toBe("recurring");
    expect(ast.filters.some((f) => f.field === "date")).toBe(false);
  });

  it("stamps dataset='transactions' and KEEPS the canvas date filter", () => {
    const ast = buildQueryAst(
      transactionsBarWidget(),
      CANVAS_DATE,
      sourceSupportsDateFilter(SOURCES, "transactions"),
    );
    expect(ast.dataset).toBe("transactions");
    expect(ast.filters).toContainEqual({
      field: "date",
      op: "between",
      value: ["2026-01-01", "2026-01-31"],
    });
  });

  it("keeps the date filter by default when the catalog flag is omitted (pre-load safety)", () => {
    const ast = buildQueryAst(transactionsBarWidget(), CANVAS_DATE);
    expect(ast.filters).toContainEqual({
      field: "date",
      op: "between",
      value: ["2026-01-01", "2026-01-31"],
    });
  });
});

describe("buildQueryAst — canvas STATUS cascade (Feature 1)", () => {
  const CANVAS_STATUS: CanvasFilters = { status: "settled" };

  it("cascades the canvas status onto a transactions widget", () => {
    const ast = buildQueryAst(
      transactionsBarWidget(),
      CANVAS_STATUS,
      sourceSupportsDateFilter(SOURCES, "transactions"),
      sourceSupportsStatusFilter(SOURCES, "transactions"),
    );
    expect(ast.filters).toContainEqual({
      field: "status",
      op: "eq",
      value: "settled",
    });
  });

  it("OMITS the canvas status on an accounts widget (source publishes no status)", () => {
    const ast = buildQueryAst(
      accountsBarWidget(),
      CANVAS_STATUS,
      sourceSupportsDateFilter(SOURCES, "accounts"),
      sourceSupportsStatusFilter(SOURCES, "accounts"),
    );
    expect(ast.filters.some((f) => f.field === "status")).toBe(false);
  });

  it("OMITS the canvas status on a recurring widget (no leak, no 422)", () => {
    const ast = buildQueryAst(
      recurringBarWidget(),
      CANVAS_STATUS,
      sourceSupportsDateFilter(SOURCES, "recurring"),
      sourceSupportsStatusFilter(SOURCES, "recurring"),
    );
    expect(ast.filters.some((f) => f.field === "status")).toBe(false);
  });
});
