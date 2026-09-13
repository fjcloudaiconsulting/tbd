/**
 * TBD-427: report chart legends, widget level.
 *
 * Our OWN chart modules are stubbed (never recharts) so the props each widget
 * hands its chart are observable on the real `next/dynamic` path, the same
 * idiom as `widget-notices.test.tsx`. `runQuery` is mocked; everything between
 * it and the DOM is real.
 *
 * What each test kills is named on the test. Two of the gate legs are
 * deliberately NOT claimed (see L9): whenever `isLoading` holds, `rows` is
 * `[]`, so dropping `!isLoading` from a legend gate cannot change output.
 */
import { useEffect, type ComponentType } from "react";
import { useSWRConfig } from "swr";

import {
  act,
  renderWithSWR,
  screen,
  within,
} from "../../../utils/render-with-swr";
import { mockReportSources } from "../../../utils/mock-report-sources";
import LineWidget from "@/components/reports/widgets/LineWidget";
import AreaWidget from "@/components/reports/widgets/AreaWidget";
import PieWidget from "@/components/reports/widgets/PieWidget";
import TableWidget from "@/components/reports/widgets/TableWidget";
import SankeyWidget from "@/components/reports/widgets/SankeyWidget";
import SparklineWidget from "@/components/reports/widgets/SparklineWidget";
import { runQuery } from "@/lib/reports/api";
import { useSankeyQuery } from "@/lib/reports/useSankeyQuery";
import type {
  AreaWidget as AreaWidgetType,
  LineWidget as LineWidgetType,
  PieWidget as PieWidgetType,
  SankeyWidget as SankeyWidgetType,
  SeriesConfig,
  SparklineWidget as SparklineWidgetType,
  TableWidget as TableWidgetType,
} from "@/lib/reports/types";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({ runQuery: vi.fn() }));

vi.mock("@/lib/reports/useSankeyQuery", () => ({ useSankeyQuery: vi.fn() }));

/** Records the props a widget hands its chart as JSON data attributes. */
function chartStub(testid: string) {
  return function ChartStub(props: {
    rows?: Array<{ label: string }>;
    labels?: string[];
    seriesColors?: string[];
    sliceColors?: string[];
  }) {
    return (
      <div
        data-testid={testid}
        data-labels={JSON.stringify(props.labels ?? null)}
        data-row-labels={JSON.stringify(props.rows?.map((r) => r.label) ?? null)}
        data-series-colors={JSON.stringify(props.seriesColors ?? null)}
        data-slice-colors={JSON.stringify(props.sliceColors ?? null)}
      />
    );
  };
}

vi.mock("@/components/reports/widgets/LineWidgetChart", () => ({
  default: chartStub("line-chart-stub"),
}));
vi.mock("@/components/reports/widgets/AreaWidgetChart", () => ({
  default: chartStub("area-chart-stub"),
}));
vi.mock("@/components/reports/widgets/PieWidgetChart", () => ({
  default: chartStub("pie-chart-stub"),
}));
vi.mock("@/components/reports/widgets/SparklineWidgetChart", () => ({
  default: chartStub("sparkline-chart-stub"),
}));
vi.mock("@/components/reports/widgets/SankeyWidgetChart", () => ({
  default: chartStub("sankey-chart-stub"),
}));

const runQueryMock = vi.mocked(runQuery);
const META = { row_count: 2, truncated: false, query_ms: 1 };
const MONTH_ROWS = {
  rows: [
    { month: "2026-01", value: 1 },
    { month: "2026-02", value: 2 },
  ],
  meta: META,
};

/**
 * Explicit labels whose alphabetical order is the REVERSE of series order.
 * Derived labels would not do: "Row count" then "Amount" become "Count of Row
 * count", "Sum of Amount", which is already alphabetical, so a sorting mutant
 * would pass.
 */
const ZETA_ALPHA: SeriesConfig[] = [
  { measure: { agg: "sum", field: "amount" }, label: "Zeta" },
  { measure: { agg: "count", field: "id" }, label: "Alpha" },
];

type SeriesType = "line" | "area";
type SeriesWidget = LineWidgetType | AreaWidgetType;

function seriesWidget(
  type: SeriesType,
  {
    id = `w_${type}`,
    title = "Net by month",
    measures = ZETA_ALPHA,
    dimensions = ["month"],
  }: {
    id?: string;
    title?: string;
    measures?: SeriesConfig[];
    dimensions?: string[];
  } = {},
): SeriesWidget {
  return {
    id,
    type,
    title,
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures,
      dimensions,
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
    },
  } as SeriesWidget;
}

function pieWidget(top_n = 8): PieWidgetType {
  return {
    id: "w_pie",
    type: "pie",
    title: "Spend share",
    grid: { x: 0, y: 0, w: 4, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
      sort: { by: "value", dir: "desc" },
      limit: 50,
      top_n,
    },
  };
}

const PIE_FOLD_ROWS = {
  rows: [
    { category: "Rent", value: 5 },
    { category: "Food", value: 3 },
    { category: "Misc", value: 1 },
    { category: "Fun", value: 1 },
  ],
  meta: META,
};

const itemTexts = (list: HTMLElement) =>
  within(list)
    .getAllByRole("listitem")
    .map((li) => li.textContent);
const swatchColors = (list: HTMLElement) =>
  [...list.querySelectorAll("[data-testid$='-legend-swatch']")].map((s) =>
    s.getAttribute("data-color"),
  );
const attr = (el: HTMLElement, name: string): unknown =>
  JSON.parse(el.getAttribute(name) ?? "null");

beforeEach(() => runQueryMock.mockReset());

/**
 * Revalidates every key in the test's own SWR cache. Rendered beside a widget
 * so an error can land on data that has ALREADY resolved (stale rows): SWR
 * keeps `data` and sets `error`, which is the only state where `!error` is
 * the sole thing keeping a legend off the card.
 */
const swrHandle: { revalidateAll?: () => Promise<unknown> } = {};
function SWRHandle() {
  const { mutate } = useSWRConfig();
  useEffect(() => {
    swrHandle.revalidateAll = async () => mutate(() => true);
  }, [mutate]);
  return null;
}

async function failNextRevalidation() {
  runQueryMock.mockRejectedValue(new Error("boom"));
  const revalidateAll = swrHandle.revalidateAll;
  if (!revalidateAll) throw new Error("render <SWRHandle /> beside the widget");
  await act(async () => {
    // The widget's hook records the rejection as `error`, which is what the
    // test asserts; this caller has no use for it.
    await revalidateAll().catch(() => undefined);
  });
}

const SERIES: Array<[SeriesType, ComponentType<{ widget: SeriesWidget }>]> = [
  ["line", LineWidget as ComponentType<{ widget: SeriesWidget }>],
  ["area", AreaWidget as ComponentType<{ widget: SeriesWidget }>],
];

describe.each(SERIES)("%s legend", (type, Widget) => {
  const stubId = `${type}-chart-stub`;

  // L3 + L4 + L6. KILLS: a legend with no accessible name; any sort of the
  // items; legend labels or colours built apart from what the chart receives.
  it("is a named list in series order whose labels and colours are the chart's", async () => {
    runQueryMock.mockResolvedValue(MONTH_ROWS);
    renderWithSWR(<Widget widget={seriesWidget(type)} />);
    const stub = await screen.findByTestId(stubId);
    const list = screen.getByRole("list", { name: "Series in Net by month" });

    expect(itemTexts(list)).toEqual(["Zeta", "Alpha"]);
    expect(itemTexts(list)).toEqual(attr(stub, "data-labels"));
    expect(swatchColors(list)).toEqual(attr(stub, "data-series-colors"));
    expect(swatchColors(list)).toHaveLength(2);
    expect(within(screen.getByTestId(`${type}-widget`)).getByTestId(`${type}-widget-legend`)).toBe(list);
  });

  // L8. KILLS: dropping `!twoDimensional` from the gate. TWO measures, because
  // with one the `seriesKeys.length > 1` gate hides the legend regardless.
  // The one-dimension sentinel card is the settle signal: its legend has
  // rendered, so the refused card's rows have arrived too.
  it("renders no legend beside the two-dimension refusal", async () => {
    runQueryMock.mockResolvedValue({
      rows: [
        { month: "2026-01", category: "Rent", value: 9 },
        { month: "2026-02", category: "Food", value: 4 },
      ],
      meta: META,
    });
    renderWithSWR(
      <>
        <Widget
          widget={seriesWidget(type, { dimensions: ["month", "category"] })}
        />
        <Widget widget={seriesWidget(type, { id: "sentinel", title: "Sentinel" })} />
      </>,
    );
    await screen.findByRole("list", { name: "Series in Sentinel" });
    await screen.findByTestId(`${type}-widget-unsupported`);
    expect(screen.getAllByRole("list")).toHaveLength(1);
  });

  // L9 guard: no legend on empty, or on a single series.
  it("renders no legend when empty or single-series", async () => {
    runQueryMock.mockResolvedValue({ rows: [], meta: META });
    const empty = renderWithSWR(<Widget widget={seriesWidget(type)} />);
    await screen.findByTestId(`${type}-widget-empty`);
    expect(screen.queryByRole("list")).toBeNull();
    empty.unmount();

    runQueryMock.mockResolvedValue(MONTH_ROWS);
    renderWithSWR(
      <Widget widget={seriesWidget(type, { measures: [ZETA_ALPHA[0]] })} />,
    );
    await screen.findByTestId(stubId);
    expect(screen.queryByRole("list")).toBeNull();
  });

  // L9 fence (error leg). KILLS: dropping `!error` from the gate. The rows
  // are STALE: data resolved, then a revalidation rejects, so the card shows
  // its error with `rows.length > 0`.
  it("renders no legend beside an error, even with stale rows", async () => {
    runQueryMock.mockResolvedValue(MONTH_ROWS);
    renderWithSWR(
      <>
        <SWRHandle />
        <Widget widget={seriesWidget(type)} />
      </>,
    );
    await screen.findByRole("list", { name: "Series in Net by month" });

    await failNextRevalidation();
    await screen.findByTestId(`${type}-widget-error`);
    expect(screen.queryByRole("list")).toBeNull();
    // A further `runQuery` call follows the error (observed while writing
    // this); left rejecting, vitest reports it unhandled against this test.
    runQueryMock.mockResolvedValue(MONTH_ROWS);
  });
});

describe("pie legend", () => {
  // L3 + L4 + L5 + L6. KILLS: no accessible name; any sort; "Other" not last
  // or painted `--color-border`; legend colours built apart from the chart's.
  it("is a named list in slice order, Other last in border-strong, colours equal to the chart's", async () => {
    runQueryMock.mockResolvedValue(PIE_FOLD_ROWS);
    renderWithSWR(<PieWidget widget={pieWidget(2)} />);
    const stub = await screen.findByTestId("pie-chart-stub");
    const list = screen.getByRole("list", {
      name: "Category slices in Spend share",
    });

    expect(itemTexts(list)).toEqual(["Rent", "Food", "Other"]);
    expect(itemTexts(list)).toEqual(attr(stub, "data-row-labels"));
    expect(swatchColors(list)).toEqual(attr(stub, "data-slice-colors"));
    expect(swatchColors(list)[2]).toBe("var(--color-border-strong)");
    expect(list.getAttribute("data-testid")).toBe("pie-widget-legend");
  });

  // L13. KILLS: label-keyed list items. A REAL "Other" category ranks inside
  // the top N and `topNWithOther` appends a second literal "Other".
  it("keys items so a real 'Other' beside the folded 'Other' is not a duplicate key", async () => {
    const errors = vi.spyOn(console, "error");
    runQueryMock.mockResolvedValue({
      rows: [
        { category: "Other", value: 10 },
        { category: "Rent", value: 5 },
        { category: "Food", value: 3 },
        { category: "Misc", value: 1 },
      ],
      meta: META,
    });
    renderWithSWR(<PieWidget widget={pieWidget(2)} />);
    await screen.findByTestId("pie-chart-stub");
    const list = screen.getByRole("list", {
      name: "Category slices in Spend share",
    });

    expect(itemTexts(list)).toEqual(["Other", "Rent", "Other"]);
    const duplicateKey = errors.mock.calls.filter((call) =>
      call.some((arg) => String(arg).includes("same key")),
    );
    expect(duplicateKey).toEqual([]);
    errors.mockRestore();
  });

  // L9 guard: a single slice still gets its key; nothing when empty.
  it("renders a one-item legend for one slice and none when empty", async () => {
    runQueryMock.mockResolvedValue({
      rows: [{ category: "Rent", value: 5 }],
      meta: META,
    });
    const one = renderWithSWR(<PieWidget widget={pieWidget()} />);
    await screen.findByTestId("pie-chart-stub");
    expect(
      itemTexts(screen.getByRole("list", { name: "Category slices in Spend share" })),
    ).toEqual(["Rent"]);
    one.unmount();

    runQueryMock.mockResolvedValue({ rows: [], meta: META });
    renderWithSWR(<PieWidget widget={pieWidget()} />);
    await screen.findByTestId("pie-widget-empty");
    expect(screen.queryByRole("list")).toBeNull();
  });

  // L9 fence (error leg), stale rows as above.
  it("renders no legend beside an error, even with stale rows", async () => {
    runQueryMock.mockResolvedValue(PIE_FOLD_ROWS);
    renderWithSWR(
      <>
        <SWRHandle />
        <PieWidget widget={pieWidget()} />
      </>,
    );
    await screen.findByRole("list", { name: "Category slices in Spend share" });

    await failNextRevalidation();
    await screen.findByTestId("pie-widget-error");
    expect(screen.queryByRole("list")).toBeNull();
    // A further `runQuery` call follows the error (observed while writing
    // this); left rejecting, vitest reports it unhandled against this test.
    runQueryMock.mockResolvedValue(PIE_FOLD_ROWS);
  });
});

/**
 * L10. ARIA prohibits naming the generic role, so an `aria-label` on a
 * role-less `<div>`/`<span>` is ignored by assistive tech. Sweeps the whole
 * population of report widgets that carried one, on the rendered DOM.
 */
const NAMEABLE_TAGS = new Set([
  "A", "BUTTON", "INPUT", "SELECT", "TEXTAREA", "UL", "OL", "TABLE",
  "SECTION", "NAV", "FORM", "IMG", "SVG",
]);
function roleLessNamed(root: HTMLElement): string[] {
  return [root, ...root.querySelectorAll<HTMLElement>("[aria-label]")]
    .filter(
      (el) =>
        el.hasAttribute("aria-label") &&
        !el.hasAttribute("role") &&
        !NAMEABLE_TAGS.has(el.tagName.toUpperCase()),
    )
    .map((el) => `<${el.tagName.toLowerCase()} aria-label="${el.getAttribute("aria-label")}">`);
}

describe("L10: no role-less aria-label in any report widget", () => {
  it.each(SERIES)("%s", async (type, Widget) => {
    runQueryMock.mockResolvedValue(MONTH_ROWS);
    renderWithSWR(<Widget widget={seriesWidget(type)} />);
    await screen.findByTestId(`${type}-chart-stub`);
    expect(roleLessNamed(screen.getByTestId(`${type}-widget`))).toEqual([]);
  });

  it("pie", async () => {
    runQueryMock.mockResolvedValue(PIE_FOLD_ROWS);
    renderWithSWR(<PieWidget widget={pieWidget()} />);
    await screen.findByTestId("pie-chart-stub");
    expect(roleLessNamed(screen.getByTestId("pie-widget"))).toEqual([]);
  });

  it("table", async () => {
    runQueryMock.mockResolvedValue({
      rows: [{ category: "Rent", value: 5 }],
      meta: META,
    });
    const widget: TableWidgetType = {
      id: "w_table",
      type: "table",
      title: "Rows",
      grid: { x: 0, y: 0, w: 12, h: 6 },
      config: {
        dataset: "transactions",
        measures: [{ measure: { agg: "sum", field: "amount" } }],
        dimensions: ["category"],
        sort: { by: "value", dir: "desc" },
        limit: 50,
      },
    };
    renderWithSWR(<TableWidget widget={widget} />);
    await screen.findByText("Rent");
    expect(roleLessNamed(screen.getByTestId("table-widget"))).toEqual([]);
  });

  it("sparkline", async () => {
    runQueryMock.mockResolvedValue(MONTH_ROWS);
    const widget: SparklineWidgetType = {
      id: "w_spark",
      type: "sparkline",
      title: "Trend",
      grid: { x: 0, y: 0, w: 3, h: 2 },
      config: {
        dataset: "transactions",
        measure: { agg: "sum", field: "amount" },
        dimensions: ["month"],
        sort: { by: "dimension", dir: "asc" },
        limit: 12,
      },
    };
    renderWithSWR(<SparklineWidget widget={widget} />);
    await screen.findByTestId("sparkline-widget-value");
    expect(roleLessNamed(screen.getByTestId("sparkline-widget"))).toEqual([]);
  });

  it("sankey", async () => {
    vi.mocked(useSankeyQuery).mockReturnValue({
      data: {
        links: [{ source: "__hub_income__", target: "Food", value: 200 }],
        meta: META,
      },
      error: undefined,
      isLoading: false,
      query: { filters: [], spending_granularity: "category" },
    });
    const widget: SankeyWidgetType = {
      id: "w_sankey",
      type: "sankey",
      title: "Cash flow",
      grid: { x: 0, y: 0, w: 8, h: 5 },
      config: {
        dataset: "transactions",
        measure: { agg: "sum", field: "amount" },
        spending_granularity: "category",
      },
    };
    renderWithSWR(<SankeyWidget widget={widget} />);
    await screen.findByTestId("sankey-chart-stub");
    expect(roleLessNamed(screen.getByTestId("sankey-widget"))).toEqual([]);
  });
});
