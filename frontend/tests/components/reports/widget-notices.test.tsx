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
import { runQuery } from "@/lib/reports/api";
import type {
  BarWidget as BarWidgetType,
  PieWidget as PieWidgetType,
  QueryMeta,
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

const TRUNCATED: QueryMeta = { row_count: 25, truncated: true, query_ms: 3 };
const CLEAN: QueryMeta = { row_count: 25, truncated: false, query_ms: 3 };

const ROWS = [
  { category: "Food", value: 200 },
  { category: "Transport", value: 80 },
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

const runQueryMock = vi.mocked(runQuery);

beforeEach(() => {
  runQueryMock.mockReset();
  catalogPending = false;
});

describe("WidgetNotices — the decisive (condition, widget) pair fence", () => {
  it("renders ONE identical meta with a different tone, glyph and sentence", async () => {
    // Bar: every mark is its own complete group -> quiet.
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    const bar = renderWithSWR(<BarWidget widget={barWidget()} />);
    const barBtn = await screen.findByTestId("widget-notices");
    const barTone = barBtn.getAttribute("data-tone");
    const barIcon = barBtn.getAttribute("data-icon");
    const barGlyph = barBtn.querySelector("svg")!.innerHTML;
    const barLabel = barBtn.getAttribute("aria-label");
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
  });
});

describe("WidgetNotices — states", () => {
  // KILLS: a placeholder / reserved slot in the ~95% no-notice case.
  it("renders NOTHING (not an empty button) when there is no notice", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: CLEAN });
    renderWithSWR(<BarWidget widget={barWidget()} />);
    await waitFor(() =>
      expect(screen.queryByTestId("bar-widget-loading")).toBeNull(),
    );
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
  });

  // KILLS: truncation inferred from config.limit or row_count === limit.
  it("stays silent on a full-but-not-truncated page", async () => {
    runQueryMock.mockResolvedValue({
      rows: ROWS,
      meta: { row_count: 500, truncated: false, query_ms: 1 },
    });
    renderWithSWR(<BarWidget widget={barWidget({ limit: 500 })} />);
    await waitFor(() =>
      expect(screen.queryByTestId("bar-widget-loading")).toBeNull(),
    );
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
  });

  // KILLS: a notice about the shape of data that never arrived.
  //
  // ⚠ NOT vacuous: the catalog is held in flight so the widget shows its
  // skeleton while /query has ALREADY delivered a truncated meta. The
  // `data-truncated` assertion PROVES the meta reached the component, so
  // dropping the `suppressed` guard would render a glyph here.
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
  // `suppressed` mechanism itself is fenced directly below.
  it("is suppressed in the ERROR branch", async () => {
    runQueryMock.mockRejectedValue(new Error("boom"));
    renderWithSWR(<BarWidget widget={barWidget()} />);
    await screen.findByTestId("bar-widget-error");
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
  });

  // KILLS: `suppressed` accepted as a prop and then ignored. Metas that
  // WOULD produce a loud notice, suppressed, must render nothing at all.
  it("renders nothing when suppressed, and the glyph when not", () => {
    const shown = renderWithSWR(
      <WidgetNotices
        metas={[TRUNCATED]}
        derivesCrossRowAggregate
        widgetTitle="Anything"
        suppressed={false}
      />,
    );
    expect(shown.container.querySelectorAll("[data-testid='widget-notices']"))
      .toHaveLength(1);
    shown.unmount();

    const hidden = renderWithSWR(
      <WidgetNotices
        metas={[TRUNCATED]}
        derivesCrossRowAggregate
        widgetTitle="Anything"
        suppressed
      />,
    );
    expect(hidden.container.innerHTML).toBe("");
  });

  it("is suppressed in the EMPTY branch", async () => {
    runQueryMock.mockResolvedValue({ rows: [], meta: TRUNCATED });
    renderWithSWR(<BarWidget widget={barWidget()} />);
    await screen.findByTestId("bar-widget-empty");
    expect(screen.queryAllByTestId("widget-notices")).toHaveLength(0);
  });
});

describe("WidgetNotices — accessibility and interaction", () => {
  // KILLS: a generic `More info` / `getByLabelText(/more info/i)` name.
  // Twelve identically-named buttons in a screen-reader list is worse
  // than silence, so the exact composed name is the fence.
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

  // KILLS: relying on Tooltip's `aria-describedby`, which is wired only
  // while the bubble is OPEN.
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
  // the ONLY thing that explains a total the editor can see is missing, so
  // it must survive edit mode — which is also why it sits beside the title
  // rather than under `WidgetShell`'s absolute `right-1 top-1` overlay.
  it("stays visible in edit mode, where the CSV button hides", async () => {
    runQueryMock.mockResolvedValue({ rows: ROWS, meta: TRUNCATED });
    renderWithSWR(<BarWidget widget={barWidget()} editMode />);
    expect(await screen.findByTestId("widget-notices")).toBeInTheDocument();
    expect(screen.queryByTestId("widget-csv-export")).toBeNull();
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
      // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events
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

  // KILLS: computing suppression correctly but never wiring it.
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
});

describe("useSeriesQueries exposes per-series meta", () => {
  // KILLS: `useSeriesQueries` discarding `meta` (it did), and KILLS a
  // fix that only reads series[0].
  it("notices when only the SECOND measure's query is truncated", async () => {
    runQueryMock
      .mockResolvedValueOnce({ rows: ROWS, meta: CLEAN })
      .mockResolvedValueOnce({
        rows: ROWS,
        meta: { row_count: 100, truncated: true, query_ms: 2 },
      });
    renderWithSWR(<TableWidget widget={tableWidget(2)} />);
    const btn = await screen.findByTestId("widget-notices");
    expect(btn.getAttribute("data-tone")).toBe("loud");
    expect(
      screen.getByTestId("widget-notices-summary").textContent,
    ).toContain("Showing the first 100 rows.");
  });
});

describe("BarWidget break-down legend", () => {
  // KILLS: a bare flex-wrap <ul> with no cap — it bleeds out of the card,
  // and a scrollable region that is not keyboard-scrollable fails SC 2.1.1.
  it("caps its height, scrolls, and is keyboard focusable", async () => {
    runQueryMock.mockResolvedValue({
      rows: [
        { month: "2026-01", category: "Food", value: 10 },
        { month: "2026-01", category: "Transport", value: 20 },
      ],
      meta: CLEAN,
    });
    renderWithSWR(
      <BarWidget widget={barWidget({ dimensions: ["month", "category"] })} />,
    );
    const legend = await screen.findByTestId("bar-widget-legend");
    expect(legend.getAttribute("tabindex")).toBe("0");
    expect(legend.className).toContain("overflow-y-auto");
    expect(legend.className).toMatch(/max-h-/);
  });
});
