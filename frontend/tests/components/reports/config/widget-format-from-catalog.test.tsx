/**
 * F-8 — a widget's number format must come from the source catalog.
 *
 * Originally TBD-170, as a MUTATION-time fence: `resolveFormat` wrote
 * `config.format` at each write site and this file asserted the write.
 * TBD-381 moved derivation to RENDER and deleted `config.format` entirely, so
 * this file moved with it — it now asserts what the user SEES.
 *
 * The dormant bug it exists for is unchanged: `SourceMeasure.format` is
 * published by the backend and typed by the frontend, and nothing read it.
 * Format was hardcoded `"currency"` at creation and never re-derived. It never
 * fired because every measure shipped to date was currency-shaped —
 * `utilization_pct` is the first `percent` measure, so without this a 45%
 * utilization renders "€45.00".
 *
 * ⚠ Why the render-time version is STRONGER than the mutation-time one it
 * replaces. That one could only observe widgets that went through a mutation.
 * It was structurally blind to the 14 `format` writes in
 * `backend/app/reports/templates.py`, the 5 in `widgetKit.tsx` and the 5 in the
 * duplicated factory set in `app/reports/[id]/page.tsx` — none of which pass
 * through a mutation. Two were already wrong in shipped code (a `sum(amount)`
 * sparkline seeded `"number"`; the `cdd-pie-share` template omitted format
 * entirely). Asserting rendered output covers all of them at once.
 *
 * ⚠ The measure shapes below are ones the REAL UI produces. A test that
 * hand-builds a measure the shipped editor cannot emit is a green fence over a
 * live bug — that already happened once on TBD-170.
 */
import { describe, expect, it, vi } from "vitest";

import { renderWithSWR, screen } from "../../../utils/render-with-swr";
import {
  CREDIT_UTILIZATION_ENTRY,
  TRANSACTIONS_ENTRY,
  mockReportSources,
} from "../../../utils/mock-report-sources";

import KPIWidget from "@/components/reports/widgets/KPIWidget";
import { runQuery } from "@/lib/reports/api";
import type { KPIWidget as KPIWidgetType } from "@/lib/reports/types";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) =>
    mockReportSources([TRANSACTIONS_ENTRY, CREDIT_UTILIZATION_ENTRY])(path),
}));

vi.mock("@/lib/reports/api", () => ({ runQuery: vi.fn() }));

function kpi(
  dataset: string,
  measure: { agg: string; field: string },
  extra: Record<string, unknown> = {},
): KPIWidgetType {
  return {
    id: "w_fmt",
    type: "kpi",
    title: "Metric",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    // `extra` exists to plant a STALE `format` key, proving it is inert.
    config: { dataset, measure, ...extra },
  } as unknown as KPIWidgetType;
}

describe("format derives from the source catalog at render", () => {
  const runQueryMock = vi.mocked(runQuery);

  function resolveTo(value: number) {
    runQueryMock.mockResolvedValue({
      rows: [{ value }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });
  }

  it("renders a percent measure as a percentage, not currency", async () => {
    // The headline bug. KILLS any design that reads a stored format, because
    // every write site seeds "currency".
    resolveTo(45);
    renderWithSWR(
      <KPIWidget
        widget={kpi("credit_utilization", { agg: "avg", field: "utilization_pct" })}
        currency="EUR"
      />,
    );
    const el = await screen.findByTestId("kpi-widget-value");
    // ⚠ EXACT. `toContain("45")` + no "€" is also satisfied by a `"number"`
    // collapse -- so the earlier version of this test proved only the second
    // half of its own title. This kills:
    //   if (exact) return exact.format === "currency" ? "currency" : "number";
    expect(el.textContent).toBe("45.0%");
  });

  it("IGNORES a stale persisted format on the same widget", async () => {
    // KILLS keeping `config.format` as a "pre-catalog fallback". Every saved
    // widget carries a hardcoded "currency", so a fallback renders this as
    // "€45.00" — the reported bug, shipped as a first paint.
    resolveTo(45);
    renderWithSWR(
      <KPIWidget
        widget={kpi(
          "credit_utilization",
          { agg: "avg", field: "utilization_pct" },
          { format: "currency" },
        )}
        currency="EUR"
      />,
    );
    const el = await screen.findByTestId("kpi-widget-value");
    expect(el.textContent).toBe("45.0%");
  });

  it("still renders a currency measure as currency", async () => {
    // The control. Without it, "strip all formatting" passes the two above.
    resolveTo(1234.5);
    renderWithSWR(
      <KPIWidget
        widget={kpi("credit_utilization", { agg: "sum", field: "outstanding" })}
        currency="EUR"
      />,
    );
    const el = await screen.findByTestId("kpi-widget-value");
    expect(el.textContent).toContain("€");
  });

  it("renders a count as a plain number even on a currency field", async () => {
    // KILLS the FIELD-ONLY lookup the mutation-time resolver used deliberately
    // (`useWidgetMutations.ts:41` documented it as load-bearing). `count(amount)`
    // matches the `amount` row under field-only matching and inherits
    // "currency" — a transaction count rendered as "€10.00".
    resolveTo(10);
    renderWithSWR(
      <KPIWidget
        widget={kpi("transactions", { agg: "count", field: "amount" })}
        currency="EUR"
      />,
    );
    const el = await screen.findByTestId("kpi-widget-value");
    expect(el.textContent).not.toContain("€");
  });
});
