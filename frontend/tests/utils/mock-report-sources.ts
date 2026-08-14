/**
 * Shared source-catalog fixture for report widget tests (TBD-381).
 *
 * ## Why this exists
 *
 * Format used to be read from `widget.config.format`, so a widget test could
 * assert "€1,234.00" with no catalog in sight. Format now derives at render
 * from `GET /api/v1/reports/sources` via `useReportSources`, which fetches
 * through `apiFetch` in `@/lib/api` — a seam the widget tests do NOT mock (they
 * mock `@/lib/reports/api`, a different module).
 *
 * Without this helper every widget test sees an empty catalog, `format`
 * resolves to `undefined`, the wrapper holds its loading skeleton, and the
 * assertions fail for a reason that has nothing to do with what they test.
 *
 * ⚠ Mirror the REAL catalog shape. These fixtures are the contract the widgets
 * format against, so a fixture that publishes formats the backend does not
 * would make the widget tests agree with themselves and disagree with
 * production — the failure mode the source catalog exists to prevent.
 */
import { vi } from "vitest";

import type { SourceCatalogEntry } from "@/lib/reports/types";

/** `transactions` — the default dataset for most widget fixtures. */
export const TRANSACTIONS_ENTRY: SourceCatalogEntry = {
  key: "transactions",
  label: "Transactions",
  dimensions: [
    { key: "month", label: "Month", kind: "time" },
    { key: "category", label: "Category", kind: "category" },
  ],
  measures: [
    { key: "amount_sum", label: "Total", agg: "sum", field: "amount", format: "currency" },
    { key: "amount_avg", label: "Average", agg: "avg", field: "amount", format: "currency" },
    { key: "txn_count", label: "Count", agg: "count", field: "id", format: "number" },
  ],
  filters: [
    { field: "date", label: "Date", ops: ["between", "gte", "lte"], kind: "time" },
    { field: "account_id", label: "Accounts", ops: ["in"], kind: "account" },
    { field: "category_id", label: "Categories", ops: ["in"], kind: "category" },
    { field: "txn_type", label: "Type", ops: ["in"], kind: "type" },
    { field: "status", label: "Status", ops: ["eq"], kind: "status" },
    { field: "amount", label: "Amount", ops: ["between", "gte", "lte"], kind: "amount" },
    { field: "tag_name", label: "Tags", ops: ["in"], kind: "tag" },
  ],
};

/** `credit_utilization` — the source that made the percent bug visible. */
export const CREDIT_UTILIZATION_ENTRY: SourceCatalogEntry = {
  key: "credit_utilization",
  label: "Credit utilization",
  dimensions: [{ key: "account", label: "Card", kind: "account" }],
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
  filters: [
    // ⚠ NO date filter: the source is point-in-time and says so in
    // credit_utilization.py. A stray shared-canvas date is dropped by the
    // SHARED_CANVAS_FILTER_FIELDS contract, not honoured.
    { field: "account_id", label: "Card", ops: ["in"], kind: "account" },
    { field: "currency", label: "Currency", ops: ["eq", "in"], kind: "currency" },
    { field: "account_active", label: "Status", ops: ["eq"], kind: "boolean" },
  ],
};

/** `networth` — publishes NO category / txn_type / tag filters. */
export const NETWORTH_ENTRY: SourceCatalogEntry = {
  key: "networth",
  label: "Net worth",
  dimensions: [{ key: "month", label: "Month", kind: "time" }],
  measures: [
    { key: "net_worth", label: "Net worth", agg: "sum", field: "net_worth", format: "currency" },
  ],
  filters: [
    { field: "date", label: "Date", ops: ["between", "gte", "lte"], kind: "time" },
    { field: "account_id", label: "Accounts", ops: ["in"], kind: "account" },
    { field: "currency", label: "Currency", ops: ["eq"], kind: "currency" },
  ],
};

/** `recurring` — publishes `amount`, which the editor used to hide. */
export const RECURRING_ENTRY: SourceCatalogEntry = {
  key: "recurring",
  label: "Recurring",
  dimensions: [{ key: "category", label: "Category", kind: "category" }],
  measures: [
    { key: "amount_sum", label: "Total", agg: "sum", field: "amount", format: "currency" },
  ],
  filters: [
    { field: "account_id", label: "Account", ops: ["in"], kind: "account" },
    { field: "category_id", label: "Category", ops: ["eq", "in"], kind: "category" },
    { field: "currency", label: "Currency", ops: ["eq", "in"], kind: "currency" },
    { field: "txn_type", label: "Type", ops: ["eq", "in"], kind: "type" },
    { field: "frequency", label: "Frequency", ops: ["eq", "in"], kind: "type" },
    { field: "recurring_active", label: "Status", ops: ["eq"], kind: "boolean" },
    // ⚠ Published, but the editor HID it (gated on dataset === "transactions").
    // Note kind is "number" here and "amount" on transactions -- the same
    // concept under two kinds, which is why kind is not a sound dispatch key.
    { field: "amount", label: "Amount", ops: ["between", "gte", "lte"], kind: "number" },
  ],
};

export const ALL_ENTRIES = [
  TRANSACTIONS_ENTRY,
  CREDIT_UTILIZATION_ENTRY,
  NETWORTH_ENTRY,
  RECURRING_ENTRY,
];

/**
 * Point `apiFetch` at the catalog for `/api/v1/reports/sources`.
 *
 * Call inside `beforeEach` AFTER `vi.mock("@/lib/api", ...)` is in place.
 * Returns the mock so a test can override per case.
 */
export function mockReportSources(entries: SourceCatalogEntry[] = ALL_ENTRIES) {
  return vi.fn(async (path: string) => {
    if (path.startsWith("/api/v1/reports/sources")) return entries;
    throw new Error(`unmocked apiFetch: ${path}`);
  });
}
