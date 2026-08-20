/**
 * TBD-382 — the palette runs out at 8 (Defect D). Rulings R4b and R5.
 *
 * Fences F16, F17, F22, F23.
 *
 * R5: the fold fires only when the number of distinct secondary values is
 * GREATER than the palette size (> 8, not >= 8). When it fires, the top 7 by
 * grand total keep their own hue and the remainder is SUMMED into a final
 * "Other" segment painted in a neutral, pinned last.
 *
 * R4b: the colour index comes from a STABLE ordering of the secondary label
 * (alphabetical), never from arrival order — the compiler defaults to
 * ORDER BY value DESC, so arrival order is a function of the values and a
 * category would change hue between two loads.
 */
import { renderWithSWR, fireEvent, screen, waitFor } from "../../../utils/render-with-swr";

import BarWidget from "@/components/reports/widgets/BarWidget";
import type { StackedBarWidget as StackedBarWidgetType } from "@/lib/reports/types";
import { runQuery } from "@/lib/reports/api";
import { mockReportSources } from "../../../utils/mock-report-sources";
import { downloadCsv } from "@/lib/reports/csv";
import { CHART_SERIES } from "@/lib/chart-colors";

vi.mock("@/lib/api", () => ({
  apiFetch: (path: string) => mockReportSources()(path),
}));

vi.mock("@/lib/reports/api", () => ({
  runQuery: vi.fn(),
}));

vi.mock("@/lib/reports/csv", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/reports/csv")>();
  return { ...actual, downloadCsv: vi.fn() };
});

vi.mock("@/components/reports/widgets/BarWidgetChart", () => ({
  default: (props: {
    rows: Array<Record<string, number | string>>;
    secondaryValues: string[];
    seriesKeys: string[];
    sliceColors: string[];
  }) => (
    <div
      data-testid="bar-chart-stub"
      data-rows={JSON.stringify(props.rows)}
      data-secondary-values={JSON.stringify(props.secondaryValues)}
      data-series-keys={JSON.stringify(props.seriesKeys)}
      data-slice-colors={JSON.stringify(props.sliceColors)}
    />
  ),
}));

const NEUTRAL = "var(--color-border-strong)";

function makeWidget(
  overrides: Partial<StackedBarWidgetType["config"]> = {},
): StackedBarWidgetType {
  return {
    id: `w_sb_${Math.random().toString(36).slice(2, 10)}`,
    type: "stacked_bar",
    title: "Category by month",
    grid: { x: 0, y: 0, w: 12, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month", "category"],
      sort: { by: "dimension", dir: "asc" },
      limit: 12,
      ...overrides,
    },
  };
}

/**
 * `n` distinct categories over two months. Category i is worth
 * ``(n - i) * 100`` in the first month and ``(n - i) * 10`` in the second, so
 * the grand-total ranking is strictly S01 > S02 > … and unambiguous. Labels
 * are zero-padded so alphabetical order and rank order coincide, which is
 * what lets F17 assert "identical to today" literally.
 */
function catalogRows(n: number) {
  const rows: Array<{ month: string; category: string; value: number }> = [];
  for (let i = 0; i < n; i += 1) {
    const label = `S${String(i + 1).padStart(2, "0")}`;
    rows.push({ month: "2026-01", category: label, value: (n - i) * 100 });
    rows.push({ month: "2026-02", category: label, value: (n - i) * 10 });
  }
  // Deliver in the compiler's default value-desc order over pairs.
  rows.sort((a, b) => b.value - a.value);
  return rows;
}

async function stub() {
  return await screen.findByTestId("bar-chart-stub");
}

function attr<T>(el: HTMLElement, name: string): T {
  return JSON.parse(el.getAttribute(name) ?? "null") as T;
}

/** label -> swatch colour, read off the rendered DOM legend. */
function legendColorMap(): Record<string, string> {
  const items = screen.getAllByTestId("stacked-bar-widget-legend-item");
  const out: Record<string, string> = {};
  for (const item of items) {
    const swatch = item.querySelector("[data-color]");
    out[item.textContent ?? ""] = swatch?.getAttribute("data-color") ?? "";
  }
  return out;
}

describe("'Other' fold and stable colour assignment (TBD-382 R4b/R5)", () => {
  const runQueryMock = vi.mocked(runQuery);
  const downloadMock = vi.mocked(downloadCsv);

  beforeEach(() => {
    runQueryMock.mockReset();
    downloadMock.mockReset();
  });

  // ── F16 ───────────────────────────────────────────────────────────────
  it.each([9, 11])(
    "F16: %i distinct secondaries fold to 7 + a neutral 'Other' pinned last, and the bar total stays exact",
    async (n) => {
      const rows = catalogRows(n);
      runQueryMock.mockResolvedValueOnce({
        rows,
        meta: { row_count: rows.length, truncated: false, query_ms: 4 },
      });

      renderWithSWR(<BarWidget widget={makeWidget()} />);

      const el = await stub();
      const secondaryValues = attr<string[]>(el, "data-secondary-values");
      const seriesKeys = attr<string[]>(el, "data-series-keys");
      const colors = attr<string[]>(el, "data-slice-colors");
      const chartRows = attr<Array<Record<string, number | string>>>(el, "data-rows");

      // Exactly one series per palette slot — never a wrapped `i % 8`.
      expect(secondaryValues).toHaveLength(8);
      expect(seriesKeys).toHaveLength(8);

      // The 8th is "Other", pinned LAST in stack order, in the neutral —
      // NOT CHART_SERIES[7], which is the danger hue.
      expect(secondaryValues[7]).toBe("Other");
      expect(colors[7]).toBe(NEUTRAL);
      expect(colors.slice(0, 7)).toEqual([...CHART_SERIES].slice(0, 7));
      expect(colors).not.toContain(CHART_SERIES[7]);

      // The tail is SUMMED, not dropped: each bar total still equals the
      // sum of that month's raw values.
      for (const month of ["2026-01", "2026-02"]) {
        const raw = rows
          .filter((r) => r.month === month)
          .reduce((s, r) => s + r.value, 0);
        const rendered = chartRows
          .filter((r) => r.label === month)
          .flatMap((r) => seriesKeys.map((k) => (typeof r[k] === "number" ? (r[k] as number) : 0)))
          .reduce((s, v) => s + v, 0);
        expect(rendered).toBe(raw);
      }

      // And "Other" is last in the DOM legend too.
      const items = screen.getAllByTestId("stacked-bar-widget-legend-item");
      expect(items[items.length - 1]).toHaveTextContent("Other");
    },
  );

  // ── F17 ───────────────────────────────────────────────────────────────
  it.each([7, 8])(
    "F17: %i distinct secondaries is a strict no-op — no 'Other', and the label->colour map is unchanged",
    async (n) => {
      const rows = catalogRows(n);
      runQueryMock.mockResolvedValueOnce({
        rows,
        meta: { row_count: rows.length, truncated: false, query_ms: 4 },
      });

      renderWithSWR(<BarWidget widget={makeWidget()} />);

      const el = await stub();
      const secondaryValues = attr<string[]>(el, "data-secondary-values");
      const colors = attr<string[]>(el, "data-slice-colors");

      expect(secondaryValues).toHaveLength(n);
      expect(secondaryValues).not.toContain("Other");
      expect(colors).not.toContain(NEUTRAL);

      // Per-LABEL colour, not merely the key count: a fold triggering at
      // >= 8 would still yield eight keys at n=8 while repainting the chart.
      const expected: Record<string, string> = {};
      for (let i = 0; i < n; i += 1) {
        expected[`S${String(i + 1).padStart(2, "0")}`] = CHART_SERIES[i];
      }
      expect(legendColorMap()).toEqual(expected);
    },
  );

  // ── F22 ───────────────────────────────────────────────────────────────
  it("F22: the CSV export carries the RAW, UNFOLDED columns (11 values -> 12 columns)", async () => {
    const rows = catalogRows(11);
    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: rows.length, truncated: false, query_ms: 4 },
    });

    renderWithSWR(<BarWidget widget={makeWidget()} />);

    const el = await stub();
    expect(attr<string[]>(el, "data-secondary-values")).toHaveLength(8);

    const exportBtn = await screen.findByTestId("widget-csv-export");
    await waitFor(() => expect(exportBtn).not.toBeDisabled());
    fireEvent.click(exportBtn);

    const [, csv] = downloadMock.mock.calls[0];
    const header = (csv as string).split("\r\n")[0].split(",");
    // label + 11 raw secondary columns; every original label survives, so
    // "Other" has a drill path back to its constituent rows.
    expect(header).toHaveLength(12);
    expect(header[0]).toBe("Month");
    expect(header).not.toContain("Other");
    for (let i = 1; i <= 11; i += 1) {
      expect(header).toContain(`S${String(i).padStart(2, "0")}`);
    }
  });

  // ── F23 ───────────────────────────────────────────────────────────────
  it("F23: reversing the row order leaves the label->colour mapping identical", async () => {
    // Labels chosen so first-seen order and alphabetical order DISAGREE:
    // under arrival-order colouring, Zeta is chart-1 forward and chart-3
    // reversed — a category changing hue between two loads.
    const base = [
      { month: "2026-01", category: "Zeta", value: 300 },
      { month: "2026-01", category: "Alpha", value: 200 },
      { month: "2026-01", category: "Mid", value: 100 },
      { month: "2026-02", category: "Zeta", value: 30 },
      { month: "2026-02", category: "Alpha", value: 20 },
      { month: "2026-02", category: "Mid", value: 10 },
    ];

    runQueryMock.mockResolvedValueOnce({
      rows: base,
      meta: { row_count: base.length, truncated: false, query_ms: 2 },
    });
    const first = renderWithSWR(<BarWidget widget={makeWidget()} />);
    await stub();
    const forward = legendColorMap();
    first.unmount();

    runQueryMock.mockResolvedValueOnce({
      rows: [...base].reverse(),
      meta: { row_count: base.length, truncated: false, query_ms: 2 },
    });
    renderWithSWR(<BarWidget widget={makeWidget()} />);
    await stub();
    const reversed = legendColorMap();

    expect(Object.keys(forward).sort()).toEqual(["Alpha", "Mid", "Zeta"]);
    expect(reversed).toEqual(forward);
  });
});
