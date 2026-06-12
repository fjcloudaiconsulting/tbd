import { renderWithSWR, screen, waitFor } from "../../../utils/render-with-swr";

import KPIWidget from "@/components/reports/widgets/KPIWidget";
import type { KPIWidget as KPIWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";

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
      format: "currency",
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
    // Grouped, 2dp, NO currency symbol (symbols deferred to the future
    // multi-currency work). Assert the grouped digits and the absence
    // of any "$"/"USD".
    expect(value.textContent).toContain("1,234.56");
    expect(value.textContent).not.toContain("$");
    expect(value.textContent).not.toContain("USD");
  });

  it("renders a delta vs the supplied prior-period value when compare_prior_period is on", async () => {
    const widget = makeWidget({
      config: {
        dataset: "transactions",
        measure: { agg: "sum", field: "amount" },
        format: "currency",
        compare_prior_period: true,
      },
    });
    runQueryMock.mockResolvedValueOnce({
      rows: [{ value: 200 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderWithSWR(<KPIWidget widget={widget} priorValue={100} />);

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

    renderWithSWR(<KPIWidget widget={makeWidget()} priorValue={100} />);

    await screen.findByTestId("kpi-widget-value");
    expect(screen.queryByTestId("kpi-widget-delta")).toBeNull();
  });

  it("renders an inline error when the query fails", async () => {
    runQueryMock.mockRejectedValueOnce(new Error("boom"));

    renderWithSWR(<KPIWidget widget={makeWidget()} />);

    await waitFor(() =>
      expect(screen.getByTestId("kpi-widget-error")).toBeInTheDocument(),
    );
  });
});
