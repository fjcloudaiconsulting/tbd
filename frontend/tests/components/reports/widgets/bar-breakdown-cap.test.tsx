/**
 * TBD-382 — the two-dimension limit caps PRIMARY BUCKETS, not (primary,
 * secondary) PAIRS (Defect C, ruling R3).
 *
 * Fences F6, F7, F21, F24 and guard F19.
 *
 * With two dimensions a backend row is a PAIR, so `limit: 10` returned at most
 * ten pairs in total — every affected bar under-reported its own total. R3
 * raises the AST limit to MAX_LIMIT and turns `config.limit` into a
 * client-side cap on primaries, applied AFTER the pivot and BEFORE the
 * "Other" fold.
 */
import { renderWithSWR, screen, waitFor } from "../../../utils/render-with-swr";

import BarWidget from "@/components/reports/widgets/BarWidget";
import type {
  BarWidget as BarWidgetType,
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

vi.mock("@/components/reports/widgets/BarWidgetChart", () => ({
  default: (props: {
    rows: Array<Record<string, number | string>>;
    secondaryValues: string[];
    seriesKeys: string[];
  }) => (
    <div
      data-testid="bar-chart-stub"
      data-labels={JSON.stringify(props.rows.map((r) => String(r.label)))}
      data-secondary-values={JSON.stringify(props.secondaryValues)}
      data-series-keys={JSON.stringify(props.seriesKeys)}
    />
  ),
}));

function makeStacked(
  overrides: Partial<StackedBarWidgetType["config"]> = {},
): StackedBarWidgetType {
  return {
    id: `w_sb_${Math.random().toString(36).slice(2, 10)}`,
    type: "stacked_bar",
    title: "Stacked",
    grid: { x: 0, y: 0, w: 12, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month", "category"],
      ...overrides,
    },
  };
}

function makeBar(overrides: Partial<BarWidgetType["config"]> = {}): BarWidgetType {
  return {
    id: `w_bar_${Math.random().toString(36).slice(2, 10)}`,
    type: "bar",
    title: "Bar",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category", "account"],
      ...overrides,
    },
  };
}

async function labels(): Promise<string[]> {
  const stub = await screen.findByTestId("bar-chart-stub");
  return JSON.parse(stub.getAttribute("data-labels") ?? "[]");
}

describe("two-dimension limit caps primary buckets (TBD-382 R3)", () => {
  const runQueryMock = vi.mocked(runQuery);

  beforeEach(() => {
    runQueryMock.mockReset();
  });

  // ── F6 ────────────────────────────────────────────────────────────────
  it("F6: 12 primaries with limit 10 keeps the ten with the largest SUMMED total", async () => {
    // Rows arrive in the compiler's default value-desc order over PAIRS.
    // C11 is deliberately shaped so its LARGEST pair is small (100) while
    // its TOTAL (300) beats C10's (252) and C12's (292): "keep backend
    // order and take the first ten" would keep C12 and drop C11, which is
    // the wrong answer.
    const rows: Array<{ category: string; account: string; value: number }> = [];
    const push = (category: string, x: number, y: number, z: number) => {
      rows.push({ category, account: "X", value: x });
      rows.push({ category, account: "Y", value: y });
      rows.push({ category, account: "Z", value: z });
    };
    for (let i = 1; i <= 9; i += 1) push(`C${String(i).padStart(2, "0")}`, 300, 1, 1);
    push("C10", 250, 1, 1); // total 252 — the smallest, must drop
    push("C11", 100, 100, 100); // total 300 — must SURVIVE on total
    push("C12", 290, 1, 1); // total 292 — must drop
    rows.sort((a, b) => b.value - a.value);

    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: rows.length, truncated: false, query_ms: 5 },
    });

    renderWithSWR(
      <BarWidget
        widget={makeBar({ sort: { by: "value", dir: "desc" }, limit: 10 })}
      />,
    );

    const shown = await labels();
    expect(shown).toHaveLength(10);
    expect(shown).toContain("C11");
    expect(shown).not.toContain("C10");
    expect(shown).not.toContain("C12");
  });

  // ── F7 ────────────────────────────────────────────────────────────────
  it("F7: a time primary keeps the MOST RECENT N months, ascending, with no gaps", async () => {
    // 14 months, limit 12, `sort` ABSENT — so the compiler applied its
    // default ORDER BY value DESC over pairs and the oldest months arrive
    // FIRST (they carry the largest values). "Keep backend order, take the
    // first twelve" therefore keeps the twelve OLDEST months.
    const months: string[] = [];
    for (let i = 0; i < 14; i += 1) {
      const year = 2025 + Math.floor(i / 12);
      const month = (i % 12) + 1;
      months.push(`${year}-${String(month).padStart(2, "0")}`);
    }
    const rows: Array<{ month: string; category: string; value: number }> = [];
    months.forEach((m, i) => {
      rows.push({ month: m, category: "Rent", value: (14 - i) * 100 });
      rows.push({ month: m, category: "Food", value: 10 });
    });
    rows.sort((a, b) => b.value - a.value);

    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: rows.length, truncated: false, query_ms: 7 },
    });

    renderWithSWR(
      <BarWidget widget={makeStacked({ sort: undefined, limit: 12 })} />,
    );

    const shown = await labels();
    // The twelve MOST RECENT, in ascending chronological order, no gaps.
    expect(shown).toEqual(months.slice(2));
    expect(shown).not.toContain(months[0]);
    expect(shown).not.toContain(months[1]);
  });

  // ── F21 ───────────────────────────────────────────────────────────────
  it("F21: primaries are capped BEFORE the 'Other' fold ranks secondaries", async () => {
    // Ten primaries carry secondaries A..H. An eleventh primary carries a
    // single "Ghost" secondary worth 600 — smaller than any surviving
    // primary's total (690), so the cap drops it. Fold-FIRST would rank
    // Ghost (grand total 600) above the real, visible G (500) and H (400),
    // awarding Ghost a legend entry and a palette slot while it contributes
    // ZERO to every rendered bar, and burying G and H in "Other".
    const rows: Array<{ category: string; account: string; value: number }> = [];
    const common: Array<[string, number]> = [
      ["A", 100],
      ["B", 100],
      ["C", 100],
      ["D", 100],
      ["E", 100],
      ["F", 100],
      ["G", 50],
      ["H", 40],
    ];
    for (let i = 1; i <= 10; i += 1) {
      for (const [acct, v] of common) {
        rows.push({ category: `P${String(i).padStart(2, "0")}`, account: acct, value: v });
      }
    }
    rows.push({ category: "P11", account: "Ghost", value: 600 });
    rows.sort((a, b) => b.value - a.value);

    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: rows.length, truncated: false, query_ms: 8 },
    });

    renderWithSWR(
      <BarWidget
        widget={makeBar({ sort: { by: "value", dir: "desc" }, limit: 10 })}
      />,
    );

    const legend = await screen.findByTestId("bar-widget-legend");
    const items = screen
      .getAllByTestId("bar-widget-legend-item")
      .map((el) => el.textContent);

    // Ghost never reached a rendered bar, so it never reaches the legend.
    expect(items).not.toContain("Ghost");
    // Eight surviving secondaries is NOT more than the palette, so nothing
    // folds: G and H keep their own entries.
    expect(legend).not.toHaveTextContent("Other");
    expect(items).toContain("G");
    expect(items).toContain("H");
    expect(items).toHaveLength(8);
  });

  // ── F24 ───────────────────────────────────────────────────────────────
  it("F24: with no config.limit, a 2-dimension widget renders all 12 primaries", async () => {
    const rows: Array<{ category: string; account: string; value: number }> = [];
    for (let i = 1; i <= 12; i += 1) {
      rows.push({ category: `C${String(i).padStart(2, "0")}`, account: "X", value: 100 + i });
    }
    rows.sort((a, b) => b.value - a.value);

    runQueryMock.mockResolvedValueOnce({
      rows,
      meta: { row_count: rows.length, truncated: false, query_ms: 3 },
    });

    renderWithSWR(<BarWidget widget={makeBar({ limit: undefined })} />);

    const shown = await labels();
    expect(shown).toHaveLength(12);
    // …and the AST asked for the whole pair space, not ten pairs.
    expect(runQueryMock.mock.calls[0][0].limit).toBe(500);
  });

  // ── F19 (guard) ───────────────────────────────────────────────────────
  it("F19 guard: a 1-dimension stacked_bar with no limit does not silently inherit bar's 10", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [{ month: "2026-01", value: 5 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderWithSWR(
      <BarWidget
        widget={makeStacked({ dimensions: ["month"], limit: undefined })}
      />,
    );

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(1));
    expect(runQueryMock.mock.calls[0][0].limit).toBe(100);
  });

  it("F19 guard: a 1-dimension bar with no limit keeps its own 10", async () => {
    runQueryMock.mockResolvedValueOnce({
      rows: [{ category: "Food", value: 5 }],
      meta: { row_count: 1, truncated: false, query_ms: 1 },
    });

    renderWithSWR(
      <BarWidget widget={makeBar({ dimensions: ["category"], limit: undefined })} />,
    );

    await waitFor(() => expect(runQueryMock).toHaveBeenCalledTimes(1));
    expect(runQueryMock.mock.calls[0][0].limit).toBe(10);
  });
});
