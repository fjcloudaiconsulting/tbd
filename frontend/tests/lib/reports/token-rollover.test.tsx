/**
 * TBD-759. A calendar preset token (``{preset:"this_month"}``) resolves to an
 * absolute window at query time. Every widget hook must re-resolve it when the
 * day changes, or a dashboard left open across midnight on Oct 31 shows some
 * widgets in November and others still in October. ``useReportQuery`` (KPI /
 * bar) is fenced by B1u in ``kpi-prior-period.test.tsx``; these fence the
 * multi-series (line / area / table) and Sankey hooks.
 *
 * Only ``Date`` is faked, so SWR and ``waitFor`` keep their real timers.
 */
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { SWRConfig } from "swr";

import { runQuery, runSankeyQuery } from "@/lib/reports/api";
import type {
  CanvasFilters,
  Filter,
  LineWidget,
  ReportsQuery,
  SankeyWidget,
} from "@/lib/reports/types";
import { useSeriesQueries } from "@/lib/reports/useReportQuery";
import { useSankeyQuery } from "@/lib/reports/useSankeyQuery";
import { mockReportSources } from "../../utils/mock-report-sources";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
  runSankeyQuery: vi.fn(),
}));

const runQueryMock = vi.mocked(runQuery);
const runSankeyMock = vi.mocked(runSankeyQuery);

const THIS_MONTH: CanvasFilters = { date_range: { preset: "this_month" } };
const OCTOBER = ["2026-10-01", "2026-10-31"];
const NOVEMBER = ["2026-11-01", "2026-11-30"];

function swrWrapper({ children }: { children: ReactNode }) {
  return createElement(
    SWRConfig,
    { value: { provider: () => new Map(), dedupingInterval: 0 } },
    children,
  );
}

function dateValue(filters: Filter[]): unknown {
  return filters.find((f) => f.field === "date")?.value;
}

const LINE: LineWidget = {
  id: "w-line",
  type: "line",
  title: "Trend",
  grid: { x: 0, y: 0, w: 6, h: 4 },
  config: {
    dataset: "transactions",
    measures: [
      { measure: { agg: "sum", field: "amount" } },
      { measure: { agg: "count", field: "id" } },
    ],
    dimensions: ["day"],
  },
};

const SANKEY: SankeyWidget = {
  id: "w-sankey",
  type: "sankey",
  title: "Cash flow",
  grid: { x: 0, y: 0, w: 6, h: 4 },
  config: {
    dataset: "transactions",
    measure: { agg: "sum", field: "amount" },
    spending_granularity: "category",
  },
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(2026, 9, 31, 23, 50)); // Oct 31, 23:50
  runQueryMock.mockReset();
  runQueryMock.mockResolvedValue({
    rows: [],
    meta: { row_count: 0, truncated: false, query_ms: 1 },
  });
  runSankeyMock.mockReset();
  runSankeyMock.mockResolvedValue({
    links: [],
    meta: { row_count: 0, truncated: false, query_ms: 1 },
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("calendar tokens re-resolve when the day changes", () => {
  it("multi-series (line/area/table): every series moves to the new month", async () => {
    const measures = LINE.config.measures.map((s) => s.measure);
    const { result, rerender } = renderHook(
      () => useSeriesQueries(LINE, THIS_MONTH, measures),
      { wrapper: swrWrapper },
    );
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const october = runQueryMock.mock.calls.map((c) => c[0] as ReportsQuery);
    expect(october).toHaveLength(2);
    for (const q of october) expect(dateValue(q.filters)).toEqual(OCTOBER);

    vi.setSystemTime(new Date(2026, 10, 1, 0, 5)); // Nov 1, 00:05
    rerender();

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(4));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    const november = runQueryMock.mock.calls
      .slice(2)
      .map((c) => c[0] as ReportsQuery);
    for (const q of november) expect(dateValue(q.filters)).toEqual(NOVEMBER);
  });

  it("sankey: the wire body moves to the new month", async () => {
    const { result, rerender } = renderHook(
      () => useSankeyQuery(SANKEY, THIS_MONTH),
      { wrapper: swrWrapper },
    );
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(dateValue(result.current.query.filters)).toEqual(OCTOBER);

    vi.setSystemTime(new Date(2026, 10, 1, 0, 5)); // Nov 1, 00:05
    rerender();

    expect(dateValue(result.current.query.filters)).toEqual(NOVEMBER);
    await waitFor(() => expect(runSankeyMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(dateValue(runSankeyMock.mock.calls[1][0].filters)).toEqual(NOVEMBER);
  });
});
