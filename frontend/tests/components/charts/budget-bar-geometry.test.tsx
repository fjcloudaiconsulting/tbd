import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import BudgetOverviewChart from "@/app/budgets/BudgetOverviewChart";
import BudgetBarsWidget from "@/components/dashboard/widgets/BudgetBarsWidget";
import CreditUtilizationBar from "@/components/dashboard/widgets/CreditUtilizationBar";
import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import { barPathWidth } from "@/tests/utils/recharts";

/**
 * TBD-505: every chart drawn through `BudgetSpentBarShape` paints bars with
 * real width, both once the enter animation has run and under reduced motion.
 *
 * ## What the ticket saw, and why it is not a code defect
 *
 * Reported on recharts 3.8.1: /budgets showed the Y-axis names and the DOM
 * legend but no bar of any series. Measured in headless Chrome against a
 * seeded stack, on both 3.8.1 and 3.10.1, the chart draws every bar. It goes
 * blank, exactly as reported, only when `requestAnimationFrame` never fires
 * (a background or agent-driven tab): recharts' enter animation then stays at
 * t=0, where every segment has width 0, `BudgetSpentBarShape` returns null and
 * the stock `Rectangle` draws nothing. None of the ticket's three suspects
 * holds: the shape draws, `cellMeta` only colours the cells, and the domain
 * gives each row its share of the track.
 *
 * So this file fences the geometry the user sees, for the three components
 * that use the shape (the legacy dashboard page's copy is fenced in
 * `tests/app/dashboard-transfer-collapse.test.tsx`):
 *   - reduced motion: final geometry on first paint;
 *   - default motion: the animation, driven by a fake clock, ends at the same
 *     geometry, and its first frame is the blank state the ticket saw (the
 *     positive control: the helper can see "no bars").
 *
 * ⚠ Do not mock recharts' marks or `@/lib/chart-shapes` here. Only
 * `ResponsiveContainer` is swapped for a fixed size.
 */

vi.mock("recharts", async () => {
  const { rechartsWithFixedSize } = await import("@/tests/utils/recharts");
  return rechartsWithFixedSize(); // 400 x 300
});

vi.mock("@/components/dashboard/DashboardDataProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/dashboard/DashboardDataProvider")>(
    "@/components/dashboard/DashboardDataProvider",
  );
  return { ...actual, useDashboard: vi.fn() };
});

function mockMatchMedia(reduce: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: reduce && query.includes("prefers-reduced-motion"),
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** Drawn bar widths per `<Bar>` series, in child order. */
function seriesWidths(container: HTMLElement): number[][] {
  return Array.from(container.querySelectorAll(".recharts-bar")).map((layer) =>
    Array.from(layer.querySelectorAll(".recharts-bar-rectangle path")).map(barPathWidth),
  );
}

const OVERVIEW = {
  budgetChartData: [
    { categoryId: 1, name: "Groceries", spent: 120, remaining: 80, over: 0 },
    { categoryId: 2, name: "Transport", spent: 300, remaining: 0, over: 50 },
  ],
  cellMeta: [
    { category_id: 1, percent_used: 60, over_budget: false },
    { category_id: 2, percent_used: 117, over_budget: true },
  ],
};

function mockDashboard() {
  const budgets = [
    { category_id: 1, category_name: "Groceries", amount: "200", spent: "120", percent_used: 60, over_budget: false },
    { category_id: 2, category_name: "Rent", amount: "1000", spent: "1000", percent_used: 100, over_budget: false },
  ];
  vi.mocked(useDashboard).mockReturnValue({
    budgets,
    dashBudgets: budgets,
    budgetChartData: [
      { name: "Groceries", spent: 120, remaining: 80 },
      { name: "Rent", spent: 1000, remaining: 0 },
    ],
    chartFilter: null,
    setChartFilter: vi.fn(),
    isPastSelectedPeriod: false,
    isFutureSelectedPeriod: false,
  } as unknown as ReturnType<typeof useDashboard>);
}

/** Each case: the element, and what its final geometry must satisfy. */
const CASES: Array<[string, () => React.ReactElement, (s: number[][]) => void]> = [
  [
    "BudgetOverviewChart",
    () => <BudgetOverviewChart {...OVERVIEW} onBarClick={() => {}} />,
    ([spent, remaining, over]) => {
      expect(spent, "one spent bar per row").toHaveLength(2);
      for (const w of spent) expect(w).toBeGreaterThan(0);
      // Width follows the data: 300 spent draws wider than 120.
      expect(spent[1]).toBeGreaterThan(spent[0]);
      expect(remaining.filter((w) => w > 0), "Groceries' remaining").toHaveLength(1);
      expect(over.filter((w) => w > 0), "Transport's overspend").toHaveLength(1);
    },
  ],
  [
    "BudgetBarsWidget",
    () => {
      mockDashboard();
      return <BudgetBarsWidget />;
    },
    ([spent, remaining]) => {
      expect(spent, "one spent bar per row").toHaveLength(2);
      for (const w of spent) expect(w).toBeGreaterThan(0);
      expect(spent[1]).toBeGreaterThan(spent[0]);
      expect(remaining.filter((w) => w > 0), "Groceries' remaining").toHaveLength(1);
    },
  ],
  [
    "CreditUtilizationBar",
    () => <CreditUtilizationBar name="Visa" balance={-500} creditLimit={2000} currency="EUR" />,
    ([[spent], [remaining]]) => {
      // 25% used on a 0..100 domain across the 400px plot: 100 + 300.
      expect(spent).toBeCloseTo(100, 0);
      expect(remaining).toBeCloseTo(300, 0);
    },
  ],
];

describe("TBD-505: BudgetSpentBarShape charts draw bars with real width", () => {
  it.each(CASES)("%s: reduced motion paints the final geometry on first paint", (_, el, check) => {
    mockMatchMedia(true);
    const { container } = render(el());
    check(seriesWidths(container));
  });

  it.each(CASES)("%s: the enter animation starts blank and ends at full geometry", (_, el, check) => {
    mockMatchMedia(false);
    // One fake clock for rAF and performance.now(): recharts times each frame
    // from both, so faking only one never advances the animation.
    vi.useFakeTimers({
      toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance", "setTimeout", "clearTimeout"],
    });
    const { container } = render(el());
    // Positive control: frame 0 is the ticket's symptom, so the helper can
    // tell "no bars" from "bars".
    expect(seriesWidths(container).flat().filter((w) => w > 0)).toEqual([]);

    act(() => {
      vi.advanceTimersByTime(2000); // > every duration here (220ms, recharts' 400ms default)
    });
    check(seriesWidths(container));
  });
});
