/**
 * TBD-430 — The Notice Register, rendered surface.
 *
 * Every test names the wrong implementation it kills. The decisive one
 * mounts ONE identical `{truncated: true}` meta through BarWidget and
 * through PieWidget and demands a different tone, a different glyph and
 * a different sentence: a condition-keyed severity map passes every
 * single-widget test in this file and dies only there.
 */
import {
  renderWithSWR,
  fireEvent,
  screen,
  waitFor,
} from "../../utils/render-with-swr";

import WidgetNotices from "@/components/reports/WidgetNotices";
import BarWidget from "@/components/reports/widgets/BarWidget";
import PieWidget from "@/components/reports/widgets/PieWidget";
import TableWidget from "@/components/reports/widgets/TableWidget";
import LineWidget from "@/components/reports/widgets/LineWidget";
import AreaWidget from "@/components/reports/widgets/AreaWidget";
import SparklineWidget from "@/components/reports/widgets/SparklineWidget";
import KPIWidget from "@/components/reports/widgets/KPIWidget";
import { runQuery } from "@/lib/reports/api";
import { downloadCsv } from "@/lib/reports/csv";
import type {
  AreaWidget as AreaWidgetType,
  BarWidget as BarWidgetType,
  KPIWidget as KPIWidgetType,
  LineWidget as LineWidgetType,
  PieWidget as PieWidgetType,
  QueryMeta,
  SparklineWidget as SparklineWidgetType,
  TableWidget as TableWidgetType,
} from "@/lib/reports/types";
import { mockReportSources } from "../../utils/mock-report-sources";

// The source catalog can legitimately still be in flight while /query has
// already returned — every widget deliberately holds its skeleton until the
// catalog resolves. That state is what makes the "suppressed while loading"
// fence non-vacuous, so it has to be reachable from a test.
let catalogPending = false;
vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) =>
    catalogPending
      ? new Promise(() => {})
      : mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

// Keep the real serializer so CSV fences assert the actual string.
vi.mock("@/lib/reports/csv", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/reports/csv")>();
  return { ...actual, downloadCsv: vi.fn() };
});

// Stub the code-split recharts inner so the suppression decision is
// OBSERVABLE as a prop on the real render path (next/dynamic + jsdom
// otherwise leaves the chart body unmounted, which would make a
// `queryByTestId("pie-center-total")` assertion vacuously green).
vi.mock("@/components/reports/widgets/PieWidgetChart", () => ({
  default: ({ suppressTotal }: { suppressTotal?: boolean }) => (
    <div
      data-testid="pie-chart-stub"
      data-suppress-total={suppressTotal ? "true" : "false"}
    />
  ),
}));

const TRUNCATED: QueryMeta = {
  row_count: 25,
  truncated: true,
  truncated_end: "lowest-ranked",
  query_ms: 3,
};
const CLEAN: QueryMeta = { row_count: 25, truncated: false, query_ms: 3 };
// `credit_utilization` sets this WITH zero rows when every card lacks a
// limit. Held verbatim.
const EXCLUSION_WARNING = "2 credit card(s) excluded — no credit limit set.";
const WARNED: QueryMeta = {
  row_count: 0,
  truncated: false,
  query_ms: 3,
  warning: EXCLUSION_WARNING,
};

const ROWS = [
  { category: "Food", value: 200 },
  { category: "Transport", value: 80 },
];
const SLICED_ROWS = [
  { month: "2026-01", category: "Food", value: 10 },
  { month: "2026-01", category: "Transport", value: 20 },
];
const MONTH_ROWS = [
  { month: "2026-01", value: 10 },
  { month: "2026-02", value: 20 },
];

function barWidget(
  over: Partial<BarWidgetType["config"]> = {},
): BarWidgetType {
  return {
    id: "w_bar",
    type: "bar",
    title: "Spend by category",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
      sort: { by: "value", dir: "desc" },
      limit: 10,
      ...over,
    },
  };
}

function pieWidget(): PieWidgetType {
  return {
    id: "w_pie",
    type: "pie",
    title: "Share by category",
    grid: { x: 0, y: 0, w: 4, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
      sort: { by: "value", dir: "desc" },
      limit: 50,
      top_n: 8,
    },
  };
}

function tableWidget(measureCount = 1): TableWidgetType {
  return {
    id: "w_table",
    type: "table",
    title: "Rows",
    grid: { x: 0, y: 0, w: 12, h: 6 },
    config: {
      dataset: "transactions",
      measures: [
        { measure: { agg: "sum", field: "amount" } },
        ...(measureCount > 1
          ? [{ measure: { agg: "count" as const, field: "id" as const } }]
          : []),
      ],
      dimensions: ["category"],
      sort: { by: "value", dir: "desc" },
      limit: 50,
    },
  } as TableWidgetType;
}

function seriesWidget<T>(type: "line" | "area", title: string): T {
  return {
    id: `w_${type}`,
    type,
    title,
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 100,
    },
  } as T;
}

function sparklineWidget(): SparklineWidgetType {
  return {
    id: "w_spark",
    type: "sparkline",
    title: "Trend",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 50,
    },
  };
}

function kpiWidget(): KPIWidgetType {
  return {
    id: "w_kpi",
    type: "kpi",
    title: "Net income",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      compare_prior_period: false,
    },
  };
}

const runQueryMock = vi.mocked(runQuery);
const downloadMock = vi.mocked(downloadCsv);

beforeEach(() => {
  runQueryMock.mockReset();
  downloadMock.mockReset();
  catalogPending = false;
});

describe("WidgetNotices — the decisive (condition, widget) pair fence", () => {
  it("renders ONE identical meta with a different tone, glyph and sentence", async () => {
    // Bar with ONE dimension: every mark is its own complete group -> quiet.
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    const bar = renderWithSWR(<BarWidget widget={barWidget()} />);
    const barBtn = await screen.findByTestId("widget-notices");
    const barTone = barBtn.getAttribute("data-tone");
    const barIcon = barBtn.getAttribute("data-icon");
    const barGlyph = barBtn.querySelector("svg")!.innerHTML;
    const barLabel = barBtn.getAttribute("aria-label");
    const barClass = barBtn.className;
    const barSummary = bar.container.querySelector(
      '[data-testid="widget-notices-summary"]',
    )!.textContent;
    bar.unmount();

    // Pie: the donut total and the "Other" slice are cross-row -> loud.
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    const pie = renderWithSWR(<PieWidget widget={pieWidget()} />);
    const pieBtn = await screen.findByTestId("widget-notices");
    const pieSummary = pie.container.querySelector(
      '[data-testid="widget-notices-summary"]',
    )!.textContent;

    expect(barTone).toBe("quiet");
    expect(pieBtn.getAttribute("data-tone")).toBe("loud");

    expect(barIcon).toBe("info");
    expect(pieBtn.getAttribute("data-icon")).toBe("triangle-alert");
    // Shape, not just an attribute: the two lucide glyphs differ.
    expect(pieBtn.querySelector("svg")!.innerHTML).not.toBe(barGlyph);

    expect(barSummary).not.toBe(pieSummary);
    expect(barLabel).toBe("Data note for Spend by category: 1 note");
    expect(pieBtn.getAttribute("aria-label")).toBe(
      "Data warning for Share by category: 1 note",
    );

    // KILLS: tone carried by `data-tone` while the COLOUR stays put — the
    // register's own rule forbids tone-by-attribute-only just as it forbids
    // tone-by-colour-only.
    expect(barClass).toContain("text-text-muted");
    expect(barClass).not.toContain("text-warning");
    expect(pieBtn.className).toContain("text-warning");
    expect(pieBtn.className).not.toContain("text-text-muted");
  });

  // ── B3 ────────────────────────────────────────────────────────────
  // KILLS: `derivesCrossRowAggregate={false}` hard-coded on the bar family.
  // With a SECOND dimension `astLimitForBarFamily` asks for 500 rows and a
  // row is a (primary, secondary) PAIR, so truncation makes every bar
  // height a partial sum. Same widget component, same meta, opposite tone.
  it("makes a TWO-dimension bar loud and a one-dimension bar quiet", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    const flat = renderWithSWR(<BarWidget widget={barWidget()} />);
    const flatBtn = await screen.findByTestId("widget-notices");
    expect(flatBtn.getAttribute("data-tone")).toBe("quiet");
    const flatSummary = flat.container.querySelector(
      '[data-testid="widget-notices-summary"]',
    )!.textContent;
    flat.unmount();

    runQueryMock.mockResolvedValue({
      rows: SLICED_ROWS,
      meta: {
        row_count: 500,
        truncated: true,
        truncated_end: "lowest-ranked",
        query_ms: 3,
      },
    });
    const sliced = renderWithSWR(
      <BarWidget widget={barWidget({ dimensions: ["month", "category"] })} />,
    );
    const slicedBtn = await screen.findByTestId("widget-notices");
    expect(slicedBtn.getAttribute("data-tone")).toBe("loud");
    expect(slicedBtn.getAttribute("data-icon")).toBe("triangle-alert");

    const slicedSummary = sliced.container.querySelector(
      '[data-testid="widget-notices-summary"]',
    )!.textContent;
    expect(slicedSummary).not.toBe(flatSummary);
    // KILLS: reusing the pie/table loud sentence here. A bar withholds
    // NOTHING — the partial sums are the bars — and `row_count` counts
    // pairs, not bars.
    expect(slicedSummary).toBe(
      "Only the first 500 rows of the break-down were returned. The values " +
        "drawn are sums over only what came back, so they under-report.",
    );
    expect(slicedSummary).not.toContain("The total is hidden");
  });
});

// ── TBD-484 ────────────────────────────────────────────────────────
// Same shape as the decisive (condition, widget) fence, one axis over:
// ONE widget component, ONE identical meta apart from `truncated_end`,
// five values, five sentences. This single fence replaces the whole
// deleted `(dataset, dimensions)` map's coverage.
describe("WidgetNotices — the reported-end axis", () => {
  const ENDS = [
    "lowest-ranked",
    "highest-ranked",
    "oldest",
    "newest",
    null,
  ] as const;

  it("renders a distinct sentence for each end the server reports", async () => {
    const seen: string[] = [];
    for (const truncated_end of ENDS) {
      runQueryMock.mockResolvedValue({
        rows: MONTH_ROWS,
        meta: { row_count: 12, truncated: true, truncated_end, query_ms: 2 },
      });
      const r = renderWithSWR(
        <LineWidget widget={seriesWidget<LineWidgetType>("line", "Trend")} />,
      );
      await screen.findByTestId("widget-notices");
      seen.push(
        r.container.querySelector(
          '[data-testid="widget-notices-summary"]',
        )!.textContent!,
      );
      r.unmount();
    }

    expect(new Set(seen).size).toBe(5);
    expect(seen[0]).toBe("Showing the first 12 rows.");
    expect(seen[1]).toBe(
      "Showing the lowest 12 rows; higher-ranked rows are not included.",
    );
    expect(seen[2]).toBe(
      "Showing the most recent 12 periods; earlier periods in this range " +
        "are not included.",
    );
    expect(seen[3]).toBe(
      "Showing the earliest 12 periods; later periods in this range are " +
        "not included.",
    );
    // ⚠⚠ The ROUTINE case, not an edge case: a name-sorted table has no
    // reader-facing end, so the server says so and the widget must not
    // invent one. KILLS `truncated_end ?? "lowest-ranked"`.
    expect(seen[4]).toBe("Showing 12 rows; more matched than are shown.");
    expect(seen[4]).not.toBe(seen[0]);
  });

  // The seeded line/area config is `sort: {by:"dimension", dir:"asc"}`
  // over `month`, which the backend resolves to `newest` (fenced in
  // backend/tests/services/test_reports_truncated_boundary.py). The
  // widget must render that, not the ranking default it used to guess.
  it("renders the chronological wording a seeded line chart actually gets", async () => {
    runQueryMock.mockResolvedValue({
      rows: MONTH_ROWS,
      meta: { row_count: 100, truncated: true, truncated_end: "newest", query_ms: 2 },
    });
    renderWithSWR(
      <LineWidget widget={seriesWidget<LineWidgetType>("line", "Trend")} />,
    );
    await screen.findByTestId("widget-notices");
    expect(
      screen.getByTestId("widget-notices-summary").textContent,
    ).toBe(
      "Showing the earliest 100 periods; later periods in this range are " +
        "not included.",
    );
  });

  // KILLS: reading `metas[0].truncated_end` for a multi-series widget.
  // Two series that dropped opposite ends have no honest single answer.
  it("collapses disagreeing per-series ends to the unqualified sentence", async () => {
    runQueryMock
      .mockResolvedValueOnce({
        rows: ROWS,
        meta: { row_count: 40, truncated: true, truncated_end: "oldest", query_ms: 2 },
      })
      .mockResolvedValueOnce({
        rows: ROWS,
        meta: { row_count: 90, truncated: true, truncated_end: "newest", query_ms: 2 },
      });
    renderWithSWR(<TableWidget widget={tableWidget(2)} />);
    await screen.findByTestId("widget-notices");
    const summary = screen.getByTestId("widget-notices-summary").textContent!;
    expect(summary.startsWith("Showing 90 rows; more matched than are shown."))
      .toBe(true);
    expect(summary).not.toContain("most recent");
    expect(summary).not.toContain("earliest");
  });
});

describe("WidgetNotices — states", () => {
  // KILLS: a placeholder / reserved slot in the ~95% no-notice case.
  it("renders NOTHING (not an empty button) when there is no notice", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: CLEAN });
    renderWithSWR(<BarWidget widget={barWidget()} />);
    // ⚠ Prove the widget actually rendered its loaded path first: without
    // this the assertion below is green for a widget that returned `null`.
    await screen.findByTestId("bar-widget-chart-region");
    expect(screen.queryByTestId("bar-widget-loading")).toBeNull();
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
  });

  // KILLS: truncation inferred from config.limit or row_count === limit.
  it("stays silent on a full-but-not-truncated page", async () => {
    runQueryMock.mockResolvedValue({
      rows: ROWS,
      meta: { row_count: 500, truncated: false, query_ms: 1 },
    });
    renderWithSWR(<BarWidget widget={barWidget({ limit: 500 })} />);
    await screen.findByTestId("bar-widget-chart-region");
    expect(screen.queryByTestId("bar-widget-loading")).toBeNull();
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
  });

  // KILLS: a notice about the shape of data that never arrived.
  //
  // ⚠ NOT vacuous: the catalog is held in flight so the widget shows its
  // skeleton while /query has ALREADY delivered a truncated meta. The
  // `data-truncated` assertion PROVES the meta reached the component, so
  // dropping the loading guard would render a glyph here.
  it("is suppressed while LOADING even with a truncated meta in hand", async () => {
    catalogPending = true;
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    renderWithSWR(<BarWidget widget={barWidget()} />);
    await screen.findByTestId("bar-widget-loading");
    await waitFor(() =>
      expect(
        screen.getByTestId("bar-widget").getAttribute("data-truncated"),
      ).toBe("true"),
    );
    expect(screen.queryByTestId("bar-widget-loading")).not.toBeNull();
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
  });

  // The error branch cannot carry a resolved meta (SWR has no data to
  // pair with a first-fetch rejection), so this asserts the BRANCH; the
  // state mechanism itself is fenced directly below.
  it("is suppressed in the ERROR branch", async () => {
    runQueryMock.mockRejectedValue(new Error("boom"));
    renderWithSWR(<BarWidget widget={barWidget()} />);
    await screen.findByTestId("bar-widget-error");
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
  });

  it("drops TRUNCATION in the EMPTY branch", async () => {
    runQueryMock.mockResolvedValue({ rows: [], meta: TRUNCATED });
    renderWithSWR(<BarWidget widget={barWidget()} />);
    await screen.findByTestId("bar-widget-empty");
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
  });

  // ── B2 ────────────────────────────────────────────────────────────
  // KILLS: ONE `suppressed` boolean covering loading/error/empty for BOTH
  // tenants. When every credit card lacks a limit, `credit_utilization`
  // returns zero rows WITH the exclusion warning — so a blanket empty
  // suppression shows "No data" and no explanation, re-silencing the very
  // disclosure whose source comment says "Silent exclusion is not
  // acceptable."
  it("KEEPS a source warning in the EMPTY branch", async () => {
    runQueryMock.mockResolvedValue({ rows: [], meta: WARNED });
    renderWithSWR(<BarWidget widget={barWidget()} />);
    await screen.findByTestId("bar-widget-empty");
    const btn = await screen.findByTestId("widget-notices");
    expect(btn.getAttribute("data-tone")).toBe("quiet");
    expect(
      screen.getByTestId("widget-notices-summary").textContent,
    ).toBe(EXCLUSION_WARNING);
  });

  it("keeps a source warning on an EMPTY pie too, and stays quiet", async () => {
    runQueryMock.mockResolvedValue({ rows: [], meta: WARNED });
    renderWithSWR(<PieWidget widget={pieWidget()} />);
    await screen.findByTestId("pie-widget-empty");
    expect(
      (await screen.findByTestId("widget-notices")).getAttribute("data-tone"),
    ).toBe("quiet");
  });

  // KILLS: the state prop accepted and ignored. All three non-ready
  // states, exercised directly, with metas that WOULD otherwise speak.
  it("honours every state directly", () => {
    const ctx = {
      metas: [{ ...TRUNCATED, warning: EXCLUSION_WARNING }],
      derivesCrossRowAggregate: true,
      withholdsCrossRowAggregate: true,
      widgetTitle: "Anything",
    };
    for (const state of ["loading", "error"] as const) {
      const r = renderWithSWR(<WidgetNotices {...ctx} state={state} />);
      expect(r.container.innerHTML).toBe("");
      r.unmount();
    }
    const empty = renderWithSWR(<WidgetNotices {...ctx} state="empty" />);
    expect(
      empty.container.querySelector('[data-testid="widget-notices"]')!
        .getAttribute("data-tone"),
    ).toBe("quiet");
    expect(
      empty.container.querySelector('[data-testid="widget-notices-summary"]')!
        .textContent,
    ).toBe(EXCLUSION_WARNING);
    empty.unmount();

    const ready = renderWithSWR(<WidgetNotices {...ctx} state="ready" />);
    expect(
      ready.container.querySelector('[data-testid="widget-notices"]')!
        .getAttribute("data-tone"),
    ).toBe("loud");
  });
});

// ── G4: the state wiring, at each loud call site ─────────────────────
describe("WidgetNotices — state wiring per widget", () => {
  it("PieWidget suppresses while loading and speaks when ready", async () => {
    catalogPending = true;
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    const loading = renderWithSWR(<PieWidget widget={pieWidget()} />);
    await screen.findByTestId("pie-widget-loading");
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
    loading.unmount();

    catalogPending = false;
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    renderWithSWR(<PieWidget widget={pieWidget()} />);
    expect(await screen.findByTestId("widget-notices")).toBeInTheDocument();
  });

  // ⚠ NOT the error branch: a first-fetch rejection leaves SWR with no
  // data, so `metas` is `[undefined]` and the register would be silent
  // whatever the state said — a vacuous fence. These two states each hold
  // a REAL truncated meta while the widget is not showing its chart.
  it("TableWidget honours loading and empty while holding a truncated meta", async () => {
    catalogPending = true;
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    const loading = renderWithSWR(<TableWidget widget={tableWidget()} />);
    await screen.findByTestId("table-widget-loading");
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
    loading.unmount();

    catalogPending = false;
    runQueryMock.mockResolvedValue({ rows: [], meta: TRUNCATED });
    const empty = renderWithSWR(<TableWidget widget={tableWidget()} />);
    await screen.findByTestId("table-widget-empty");
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
    empty.unmount();

    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    renderWithSWR(<TableWidget widget={tableWidget()} />);
    expect(await screen.findByTestId("widget-notices")).toBeInTheDocument();
  });
});

// ── G3: every wired widget, on its own path ──────────────────────────
describe("WidgetNotices — path coverage per widget", () => {
  const quietCases: Array<[string, () => React.ReactElement, string, string]> = [
    [
      "LineWidget",
      () => <LineWidget widget={seriesWidget<LineWidgetType>("line", "Trend")} />,
      "line-widget",
      "Data note for Trend: 1 note",
    ],
    [
      "AreaWidget",
      () => <AreaWidget widget={seriesWidget<AreaWidgetType>("area", "Area")} />,
      "area-widget",
      "Data note for Area: 1 note",
    ],
    [
      "SparklineWidget",
      () => <SparklineWidget widget={sparklineWidget()} />,
      "sparkline-widget",
      "Data note for Trend: 1 note",
    ],
  ];

  for (const [name, render, tid, label] of quietCases) {
    // KILLS: deleting <WidgetNotices> from this widget (green today), AND
    // KILLS flipping it to `derivesCrossRowAggregate` — an ordinary line
    // chart must never shout "The total is hidden" about a total it does
    // not draw.
    it(`${name} renders a QUIET notice on its own path`, async () => {
      runQueryMock.mockResolvedValue({
        rows: MONTH_ROWS,
        meta: {
          row_count: 40,
          truncated: true,
          truncated_end: "lowest-ranked",
          query_ms: 2,
        },
      });
      renderWithSWR(render());
      expect(screen.queryByTestId(`${tid}-empty`)).toBeNull();
      const btn = await screen.findByTestId("widget-notices");
      expect(btn.getAttribute("data-tone")).toBe("quiet");
      expect(btn.getAttribute("data-icon")).toBe("info");
      expect(btn.getAttribute("aria-label")).toBe(label);
      expect(btn.className).toContain("text-text-muted");
      expect(
        screen.getByTestId("widget-notices-summary").textContent,
      ).toBe("Showing the first 40 rows.");
    });
  }

  it("KPIWidget renders a QUIET notice on its own path", async () => {
    runQueryMock.mockResolvedValue({
      rows: [{ value: 1234 }],
      meta: {
        row_count: 1,
        truncated: true,
        truncated_end: "lowest-ranked",
        query_ms: 2,
      },
    });
    renderWithSWR(<KPIWidget widget={kpiWidget()} />);
    await screen.findByTestId("kpi-widget-value");
    const btn = await screen.findByTestId("widget-notices");
    expect(btn.getAttribute("data-tone")).toBe("quiet");
    expect(btn.getAttribute("aria-label")).toBe(
      "Data note for Net income: 1 note",
    );
    expect(
      screen.getByTestId("widget-notices-summary").textContent,
    ).toBe("Showing the first 1 row.");
  });
});

describe("WidgetNotices — accessibility and interaction", () => {
  // KILLS: a generic `More info` / `getByLabelText(/more info/i)` name.
  it("names the button with tone, widget title and notice count", async () => {
    runQueryMock.mockResolvedValue({
      rows: ROWS,
      meta: { ...TRUNCATED, warning: "Source says hello." },
    });
    renderWithSWR(<PieWidget widget={pieWidget()} />);
    expect(
      await screen.findByRole("button", {
        name: "Data warning for Share by category: 2 notes",
      }),
    ).toBeInTheDocument();
  });

  // KILLS: relying on Tooltip's `aria-describedby`, wired only while open.
  it("always renders an sr-only copy of the composed summary", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    const { container } = renderWithSWR(<BarWidget widget={barWidget()} />);
    await screen.findByTestId("widget-notices");
    const sr = container.querySelector(
      '[data-testid="widget-notices-summary"]',
    )!;
    expect(sr.className).toContain("sr-only");
    expect(sr.textContent).toBe("Showing the first 25 rows.");
  });

  // KILLS: copying `WidgetCsvButton`'s `editMode` opt-out. The notice is
  // the ONLY thing that explains a total the editor can see is missing.
  it("stays visible in edit mode, where the CSV button hides", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    renderWithSWR(<BarWidget widget={barWidget()} editMode />);
    expect(await screen.findByTestId("widget-notices")).toBeInTheDocument();
    expect(screen.queryByTestId("widget-csv-export")).toBeNull();
  });

  // ── B4 ────────────────────────────────────────────────────────────
  // KILLS: leaving the glyph flush right in edit mode. `WidgetShell`'s
  // overlay is absolute at `right-1 top-1` and its REMOVE control owns
  // x ∈ [W−26, W−4]; with the CSV button gone the glyph lands at
  // x ∈ [W−42, W−16] and a click on its top-right corner DELETES the
  // widget with no confirmation. The header must reserve that lane.
  //
  // ⚠ jsdom computes no layout, so this fences the MECHANISM (the
  // reservation is present in edit mode and absent otherwise), not the
  // pixels. The measurement itself lives in `WidgetNotices.tsx`.
  it("reserves the shell overlay's lane in edit mode only", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    const view = renderWithSWR(<BarWidget widget={barWidget()} />);
    await screen.findByTestId("widget-notices");
    expect(screen.getByTestId("widget-header").className).not.toContain("pr-12");
    view.unmount();

    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    renderWithSWR(<BarWidget widget={barWidget()} editMode />);
    await screen.findByTestId("widget-notices");
    expect(screen.getByTestId("widget-header").className).toContain("pr-12");
  });

  it("opens the tooltip with the composed summary", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    renderWithSWR(<BarWidget widget={barWidget()} />);
    fireEvent.click(await screen.findByTestId("widget-notices"));
    const bubble = await screen.findByTestId("tooltip-bubble");
    expect(bubble).toHaveTextContent("Showing the first 25 rows.");
  });

  // KILLS: omitting stopPropagation — WidgetShell wraps every widget in
  // an onClick={onSelect}, so the notice would also open the config rail.
  it("does not bubble its click to the shell's select handler", async () => {
    const onSelect = vi.fn();
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    renderWithSWR(
      <div onClick={onSelect}>
        <BarWidget widget={barWidget()} />
      </div>,
    );
    fireEvent.click(await screen.findByTestId("widget-notices"));
    expect(onSelect).not.toHaveBeenCalled();
  });
});

describe("Suppressing the fabricated total", () => {
  // The chart's own suppression behaviour is fenced against the REAL
  // component in `pie-widget-chart.test.tsx` (this file stubs it, so a
  // direct assertion here would test the stub). What is fenced here is
  // the PATH: that PieWidget actually decides and passes it down.
  it("PieWidget passes truncation down to the chart", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    const t = renderWithSWR(<PieWidget widget={pieWidget()} />);
    await waitFor(() =>
      expect(
        screen.getByTestId("pie-chart-stub").getAttribute("data-suppress-total"),
      ).toBe("true"),
    );
    t.unmount();

    runQueryMock.mockResolvedValue({ rows: ROWS, meta: CLEAN });
    renderWithSWR(<PieWidget widget={pieWidget()} />);
    await waitFor(() =>
      expect(
        screen.getByTestId("pie-chart-stub").getAttribute("data-suppress-total"),
      ).toBe("false"),
    );
  });

  it("TableWidget drops the totals row under truncation and keeps it otherwise", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: CLEAN });
    const kept = renderWithSWR(<TableWidget widget={tableWidget()} />);
    expect(await screen.findByTestId("table-widget-total-row")).toBeInTheDocument();
    kept.unmount();

    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    renderWithSWR(<TableWidget widget={tableWidget()} />);
    await screen.findByTestId("widget-notices");
    expect(screen.queryByTestId("table-widget-total-row")).toBeNull();
  });

  // ── G1 ────────────────────────────────────────────────────────────
  // KILLS: withholding the RENDERED total and still writing a fabricated
  // one into the download. A wrong `Total` row in a spreadsheet has no
  // glyph beside it and outlives the session.
  it("TableWidget drops the totals row from the CSV too", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: CLEAN });
    const kept = renderWithSWR(<TableWidget widget={tableWidget()} />);
    fireEvent.click(await screen.findByTestId("widget-csv-export"));
    expect(downloadMock.mock.calls[0][1]).toBe(
      "Category,Amount\r\nFood,200\r\nTransport,80\r\nTotal,280",
    );
    kept.unmount();
    downloadMock.mockReset();

    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    renderWithSWR(<TableWidget widget={tableWidget()} />);
    await screen.findByTestId("widget-notices");
    fireEvent.click(screen.getByTestId("widget-csv-export"));
    const csv = downloadMock.mock.calls[0][1] as string;
    expect(csv).toBe("Category,Amount\r\nFood,200\r\nTransport,80");
    expect(csv).not.toContain("Total");
  });
});

describe("useSeriesQueries exposes per-series meta", () => {
  // KILLS: `useSeriesQueries` discarding `meta` (it did), and KILLS a
  // fix that only reads series[0].
  it("notices when only the SECOND measure's query is truncated", async () => {
    runQueryMock
      .mockResolvedValueOnce({ rows: ROWS, meta: CLEAN })
      .mockResolvedValueOnce({
        rows: ROWS,
        meta: {
          row_count: 100,
          truncated: true,
          truncated_end: "lowest-ranked",
          query_ms: 2,
        },
      });
    renderWithSWR(<TableWidget widget={tableWidget(2)} />);
    const btn = await screen.findByTestId("widget-notices");
    expect(btn.getAttribute("data-tone")).toBe("loud");
    expect(
      screen.getByTestId("widget-notices-summary").textContent,
    ).toContain("Showing the first 100 rows.");
  });

  // ── G2 ────────────────────────────────────────────────────────────
  // KILLS: `const truncated = !!metas[0]?.truncated` for the SUPPRESSION
  // while the NOTICE reads every series. That combination renders the loud
  // "the total is hidden" sentence directly above a visible, wrong total —
  // the self-contradiction the register's own rule forbids.
  it("suppresses the totals row AND the CSV total when only the second series truncates", async () => {
    runQueryMock
      .mockResolvedValueOnce({ rows: ROWS, meta: CLEAN })
      .mockResolvedValueOnce({
        rows: ROWS,
        meta: {
          row_count: 100,
          truncated: true,
          truncated_end: "lowest-ranked",
          query_ms: 2,
        },
      });
    renderWithSWR(<TableWidget widget={tableWidget(2)} />);
    await screen.findByTestId("widget-notices");
    expect(screen.queryByTestId("table-widget-total-row")).toBeNull();
    fireEvent.click(screen.getByTestId("widget-csv-export"));
    expect(downloadMock.mock.calls[0][1]).not.toContain("Total");
  });
});

describe("BarWidget break-down legend", () => {
  // KILLS: a bare flex-wrap <ul> with no cap — it bleeds out of the card,
  // and a scrollable region that is not keyboard-scrollable fails SC 2.1.1.
  it("caps its height, scrolls, and is keyboard focusable", async () => {
    runQueryMock.mockResolvedValue({ rows: SLICED_ROWS, meta: CLEAN });
    renderWithSWR(
      <BarWidget widget={barWidget({ dimensions: ["month", "category"] })} />,
    );
    const legend = await screen.findByTestId("bar-widget-legend");
    expect(legend.getAttribute("tabindex")).toBe("0");
    expect(legend.className).toContain("overflow-y-auto");
    expect(legend.className).toMatch(/max-h-/);
  });
});
