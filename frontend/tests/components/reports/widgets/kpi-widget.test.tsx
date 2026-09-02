import { renderWithSWR, screen, waitFor } from "../../../utils/render-with-swr";

import KPIWidget from "@/components/reports/widgets/KPIWidget";
import type { KPIWidget as KPIWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";
import { mockReportSources } from "../../../utils/mock-report-sources";

vi.mock("@/lib/api", () => ({
  // TBD-381: format now derives at render from the source catalog, which
  // fetches via apiFetch. Without this the catalog is empty, format is
  // undefined, and the widget holds its loading skeleton forever.
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

// Fresh SWR provider per test prevents cache reuse from leaking
// a previous test's resolved value into the next test's mount.
function makeWidget(overrides: Partial<KPIWidgetType> = {}): KPIWidgetType {
  return {
    id: `w_kpi_${Math.random().toString(36).slice(2, 10)}`,
    type: "kpi",
    title: "Total spend",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
    },
    ...overrides,
  };
}

describe("KPIWidget", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
  });

  it("renders the value returned by the AST query", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [{ value: 1234.56 }],
      meta: { row_count: 1, truncated: false, query_ms: 12 },
    });

    renderWithSWR(<KPIWidget widget={makeWidget()} />);

    const value = await screen.findByTestId("kpi-widget-value");
    // No ``currency`` prop supplied → currency formatting degrades to a
    // bare grouped 2dp amount with no symbol. Assert the grouped digits
    // and the absence of any symbol.
    expect(value.textContent).toContain("1,234.56");
    expect(value.textContent).not.toContain("$");
    expect(value.textContent).not.toContain("€");
  });

  it("prefixes the org currency symbol when a currency code is supplied", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [{ value: 1234.56 }],
      meta: { row_count: 1, truncated: false, query_ms: 12 },
    });

    renderWithSWR(<KPIWidget widget={makeWidget()} currency="EUR" />);

    const value = await screen.findByTestId("kpi-widget-value");
    expect(value.textContent).toContain("€1,234.56");
  });

  // ⚠ TBD-383: these two used to pass a `priorValue={100}` prop. NO
  // production caller ever passed it, so they certified a feature that never
  // rendered in the app. The prop is gone; the widget computes its own
  // comparison from `config.compare_prior_period` + its resolved date window.
  // The decisive fences — through `renderReportWidget` AND
  // `widgetKit.renderWidgetByType` — live in `kpi-prior-period.test.tsx`.
  it("renders a delta from its OWN comparison query when compare_prior_period is on", async () => {
    const widget = makeWidget({
      config: {
        dataset: "transactions",
        measure: { agg: "sum", field: "amount" },
        filters: { date_range: { start: "2026-01-01", end: "2026-01-31" } },
        compare_prior_period: true,
      },
    });
    runQueryMock.mockImplementation(async (q) => {
      const date = q.filters.find((f) => f.field === "date");
      const isPrior =
        Array.isArray(date?.value) && date?.value[0] === "2025-12-01";
      return {
        rows: [{ value: isPrior ? 100 : 200 }],
        meta: { row_count: 1, truncated: false, query_ms: 1 },
      };
    });

    renderWithSWR(<KPIWidget widget={widget} />);

    const delta = await screen.findByTestId("kpi-widget-delta");
    // 100 → 200 is a +100% change.
    expect(delta.textContent).toContain("100");
    expect(delta.textContent).toContain("%");
    expect(delta.textContent).toContain("+");
  });

  it("does NOT render a delta when compare_prior_period is off", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [{ value: 200 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<KPIWidget widget={makeWidget()} />);

    await screen.findByTestId("kpi-widget-value");
    expect(screen.queryByTestId("kpi-widget-delta")).toBeNull();
  });

  it("renders a headline value of ZERO as a number, not as the empty em-dash", async () => {
    // ⚠ `readMeasureValue` returning `null` for `0` is indistinguishable from
    // "no data" everywhere else in this file, and the widget renders an
    // em-dash for null. Zero is a legitimate total.
    runQueryMock.mockResolvedValueOnce({
      rows: [{ value: 0 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<KPIWidget widget={makeWidget()} currency="EUR" />);

    const value = await screen.findByTestId("kpi-widget-value");
    expect(value.textContent).toContain("0.00");
    expect(value.textContent).not.toContain("—");
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("boom"));

    renderWithSWR(<KPIWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("kpi-widget-error")).toBeInTheDocument(),
    );
  });
});
