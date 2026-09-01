/**
 * TBD-403 — the `sharedFormatFor` WIRING, fenced at each multi-measure widget.
 *
 * `sharedFormatFor`'s own unit tests (tests/lib/reports/widget-format.test.ts)
 * prove the function returns `"number"` for a set of measures whose formats
 * disagree. They cannot prove any widget ASKS it about more than one measure.
 * The wrong implementation this file kills is:
 *
 *     useWidgetFormat(widget.config.dataset, [widget.config.measures[0].measure])
 *
 * which is `sharedFormatFor`'s stated reason to exist inverted -- "stamp
 * series[0]'s unit on a shared axis", which per its docstring "does not merely
 * under-serve series 2, it MISLABELS it". No existing widget test OBSERVES the
 * resolved format of a multi-measure widget, so that mutant is green across the
 * whole suite today. (`line-widget.test.tsx:94` does configure two measures --
 * it just never asserts the format that reaches the chart.)
 *
 * ## Why the assertion is a captured PROP and not rendered text
 *
 * jsdom gives `ResponsiveContainer` a 0x0 box, so recharts renders no ticks and
 * no axis text: an assertion on rendered output is vacuous regardless of what
 * `format` is. Mocking the chart MODULE does intercept the `next/dynamic`
 * import (`tests/components/reports/widgets/stacked-bar-widget.test.tsx` has
 * relied on exactly this since TBD-382), so the stubs below are the only place
 * the resolved format is observable. Do NOT mock `next/dynamic` itself.
 *
 * ## The discriminating fixture
 *
 * From the generated catalog (`tests/fixtures/report-sources.json`), the
 * `transactions` source publishes `sum(amount)` as `currency` and `count(id)`
 * as `number`. So for measures `[sum(amount), count(id)]`:
 *
 *   correct (all measures asked)     -> mixed  -> "number"
 *   measures[0]-only mutant          -> currency
 *
 * The order is deliberate: currency FIRST, so the mutant's answer is the wrong
 * one rather than accidentally right. Each widget is driven independently
 * because the mutant can be applied to one call site and not the others.
 *
 * ⚠ Each widget also gets a paired case whose expected answer is "currency".
 * That is the ANTI-VACUITY half, and it is load-bearing twice over: a "number"
 * assertion alone is satisfied by hardcoding "number", AND by the catalog
 * never loading at all (`derivedFormat ?? "number"` in every widget). The
 * paired case differs from its partner in nothing but the measure list, so its
 * "currency" proves the catalog resolved and the resolver ran.
 *
 * ⚠ BarWidget is fenced in the OPPOSITE direction, and that is not an
 * oversight -- see its block below.
 */
import { renderWithSWR, screen } from "../../../utils/render-with-swr";

import LineWidget from "@/components/reports/widgets/LineWidget";
import AreaWidget from "@/components/reports/widgets/AreaWidget";
import BarWidget from "@/components/reports/widgets/BarWidget";
import type {
  AreaWidget as AreaWidgetType,
  LineWidget as LineWidgetType,
  SeriesConfig,
  StackedBarWidget as StackedBarWidgetType,
} from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";
import { mockReportSources } from "../../../utils/mock-report-sources";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

/** One stub per chart module; each publishes only the prop under test. */
function formatStub(testid: string) {
  return {
    default: (props: { format: string }) => (
      <div data-testid={testid} data-format={props.format} />
    ),
  };
}

vi.mock("@/components/reports/widgets/LineWidgetChart", () =>
  formatStub("line-chart-format-stub"),
);
vi.mock("@/components/reports/widgets/AreaWidgetChart", () =>
  formatStub("area-chart-format-stub"),
);
vi.mock("@/components/reports/widgets/BarWidgetChart", () =>
  formatStub("bar-chart-format-stub"),
);

/** currency then number: the mutant's answer is "currency", the truth "number". */
const MIXED_MEASURES: SeriesConfig[] = [
  { measure: { agg: "sum", field: "amount" } },
  { measure: { agg: "count", field: "id" } },
];

/** Both currency: pins the agreeing branch, so a blanket "number" also fails. */
const AGREEING_MEASURES: SeriesConfig[] = [
  { measure: { agg: "sum", field: "amount" } },
  { measure: { agg: "avg", field: "amount" } },
];

function lineWidget(measures: SeriesConfig[]): LineWidgetType {
  return {
    id: `w_line_${Math.random().toString(36).slice(2, 10)}`,
    type: "line",
    title: "Line",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures,
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
    },
  };
}

function areaWidget(measures: SeriesConfig[]): AreaWidgetType {
  return {
    id: `w_area_${Math.random().toString(36).slice(2, 10)}`,
    type: "area",
    title: "Area",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures,
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
    },
  };
}

function stackedBarWidget(measures: SeriesConfig[]): StackedBarWidgetType {
  return {
    id: `w_sb_${Math.random().toString(36).slice(2, 10)}`,
    type: "stacked_bar",
    title: "Stacked bar",
    grid: { x: 0, y: 0, w: 12, h: 4 },
    config: {
      dataset: "transactions",
      measures,
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
    },
  };
}

const ROWS = {
  rows: [
    { month: "2026-01", value: 100 },
    { month: "2026-02", value: 180 },
  ],
  meta: { row_count: 2, truncated: false, query_ms: 3 },
};

async function formatOf(testid: string): Promise<string | null> {
  const stub = await screen.findByTestId(testid);
  return stub.getAttribute("data-format");
}

describe("shared-axis format wiring (TBD-403)", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
    // Line and Area fire ONE query per measure, so this must not be `Once`.
    runQueryMock.mockResolvedValue(ROWS);
  });

  // ── LineWidget ────────────────────────────────────────────────────────
  it("Line: two measures of differing formats reach the chart as number, not currency", async () => {
    renderWithSWR(<LineWidget widget={lineWidget(MIXED_MEASURES)} />);
    expect(await formatOf("line-chart-format-stub")).toBe("number");
  });

  it("Line: two measures that AGREE still reach the chart as currency", async () => {
    // Without this the previous case is satisfiable by hardcoding "number".
    renderWithSWR(<LineWidget widget={lineWidget(AGREEING_MEASURES)} />);
    expect(await formatOf("line-chart-format-stub")).toBe("currency");
  });

  // ── AreaWidget ────────────────────────────────────────────────────────
  it("Area: two measures of differing formats reach the chart as number, not currency", async () => {
    renderWithSWR(<AreaWidget widget={areaWidget(MIXED_MEASURES)} />);
    expect(await formatOf("area-chart-format-stub")).toBe("number");
  });

  it("Area: two measures that AGREE still reach the chart as currency", async () => {
    renderWithSWR(<AreaWidget widget={areaWidget(AGREEING_MEASURES)} />);
    expect(await formatOf("area-chart-format-stub")).toBe("currency");
  });

  // ── BarWidget — the INVERSE fence ─────────────────────────────────────
  //
  // ⚠ TBD-403 lists `BarWidget.tsx:173` as a third multi-measure call site
  // passing `widget.config.measures.map(m => m.measure)`. IT DOES NOT, and must
  // not. Since TBD-382, `stacked_bar` has no measure-stacking axis: it persists
  // a length-1 `measures` array only because the backend's `_MultiSeriesConfig`
  // binds `Field(min_length=1)`, it fires ONE query, and `barMeasure()` reads
  // index 0 with the documented rule that "legacy entries beyond index 0 are
  // ignored, never rewritten at render". Its stacking axis is
  // `dimensions[1]`, not `measures`.
  //
  // So the mutant named in the ticket is Bar's CORRECT behaviour, and the fence
  // has to run the other way: a legacy trailing measure must not perturb the
  // format. A "consistency" edit that hands BarWidget all of `config.measures`
  // flips a legacy [sum(amount), count(id)] widget from currency to number --
  // dropping the currency symbol off a chart that only ever plotted amounts.
  it("Bar: a legacy trailing measure does not perturb the format (stays currency)", async () => {
    renderWithSWR(<BarWidget widget={stackedBarWidget(MIXED_MEASURES)} />);
    expect(await formatOf("bar-chart-format-stub")).toBe("currency");
  });

  it("Bar: the single effective measure is measures[0], so count(id) is a number", async () => {
    // The other direction of the same read: hardcoding "currency" in BarWidget
    // (or resolving from the wrong index) passes the case above and fails here.
    renderWithSWR(
      <BarWidget
        widget={stackedBarWidget([
          { measure: { agg: "count", field: "id" } },
          { measure: { agg: "sum", field: "amount" } },
        ])}
      />,
    );
    expect(await formatOf("bar-chart-format-stub")).toBe("number");
  });
});
