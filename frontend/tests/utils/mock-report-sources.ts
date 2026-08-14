/**
 * Report-source catalog for widget tests (TBD-381).
 *
 * ⚠ GENERATED, NOT HAND-WRITTEN. The entries come from
 * `tests/fixtures/report-sources.json`, produced by
 * `backend/scripts/regen_report_sources_fixture.py` and asserted against the
 * live catalog by `backend/tests/test_report_sources_frontend_contract.py`.
 *
 * The first version of this file WAS hand-written and disagreed with production
 * in sixteen places. One was a live right/wrong split: it omitted
 * `credit_utilization`'s `sum(credit_limit)` measure, so that KPI resolved to
 * `currency` in the app and `number` under test. It also omitted the entire
 * `accounts` source, which would have made any future `accounts` test
 * structurally vacuous -- an unknown dataset hits `sourceSupportsField`'s
 * deliberate "allow everything" branch, so such a test asserts every control is
 * present and passes regardless of what the code does.
 *
 * Do not edit entries here. Change a source, re-run the generator, read the diff.
 *
 * ## Why the mock is needed at all
 *
 * Format and filter visibility both derive from `GET /api/v1/reports/sources`
 * via `useReportSources`, which fetches through `apiFetch` in `@/lib/api` -- a
 * seam the widget tests do NOT mock (they mock `@/lib/reports/api`, a different
 * module). Without this the catalog is empty, format is undefined, and the
 * widget holds its skeleton while the assertion times out.
 */
import { vi } from "vitest";

import type { SourceCatalogEntry } from "@/lib/reports/types";

import catalog from "../fixtures/report-sources.json";

export const ALL_ENTRIES = catalog as unknown as SourceCatalogEntry[];

function entry(key: string): SourceCatalogEntry {
  const found = ALL_ENTRIES.find((s) => s.key === key);
  if (!found) {
    // Loud on purpose: a silent undefined flows into `sourceSupportsField`'s
    // allow-everything branch and quietly makes the calling test assert nothing.
    throw new Error(
      `report source "${key}" missing from the generated catalog fixture. ` +
        `Known: ${ALL_ENTRIES.map((s) => s.key).join(", ")}`,
    );
  }
  return found;
}

export const TRANSACTIONS_ENTRY = entry("transactions");
export const ACCOUNTS_ENTRY = entry("accounts");
export const RECURRING_ENTRY = entry("recurring");
export const NETWORTH_ENTRY = entry("networth");
export const CREDIT_UTILIZATION_ENTRY = entry("credit_utilization");

/**
 * Point `apiFetch` at the catalog for `/api/v1/reports/sources`.
 *
 * Call inside a `vi.mock("@/lib/api", ...)` factory. Returns the mock so a test
 * can narrow the catalog per case.
 */
export function mockReportSources(entries: SourceCatalogEntry[] = ALL_ENTRIES) {
  return vi.fn(async (path: string) => {
    if (path.startsWith("/api/v1/reports/sources")) return entries;
    throw new Error(`unmocked apiFetch: ${path}`);
  });
}
