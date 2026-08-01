/**
 * BudgetOverviewChart — the bar-click contract (TBD-287).
 *
 * ## Why this file exists
 *
 * `BudgetOverviewChart` had **zero** test files before this. Its `spent` bar
 * carries an `onClick` that navigates to the filtered transactions list, and
 * that handler was unreachable from a test: `ResponsiveContainer` measures 0x0
 * under jsdom, so no `.recharts-bar-rectangle` is rendered and there is nothing
 * to click. Deleting the handler would not have failed anything.
 *
 * With `tests/utils/recharts.tsx` these are the repo's second set of assertions
 * to click a real Recharts element (after `dashboard-transfer-collapse`).
 *
 * ## The audit TBD-287 asks for — chart-element handlers, measured 2026-08-01
 *
 * **Eight** handlers exist, not the five this ticket's description implies.
 * Status of each, stated precisely, because "unfenced" and "fenced" are both
 * too coarse here:
 *
 *   1. `app/budgets/BudgetOverviewChart.tsx:60` — FENCED HERE, via a real click.
 *   2. `app/forecast-plans/ForecastPlanChart.tsx:64` — FENCED, in
 *      `tests/app/forecast-plan-chart-click.test.tsx` (added by TBD-287).
 *      Its handler is character-for-character identical to item 1.
 *   3. `app/dashboard/page.tsx` Budget Progress bar — FENCED by a real click in
 *      `tests/app/dashboard-transfer-collapse.test.tsx` (TBD-268 fold).
 *   4. `components/dashboard/widgets/BudgetBarsWidget.tsx:75` — handler body
 *      FENCED, wiring NOT.
 *   5. `components/dashboard/widgets/ForecastBarsWidget.tsx:65` — same as 4.
 *   6. `components/dashboard/widgets/SpendingDonutWidget.tsx:46` — UNFENCED.
 *   7. `app/dashboard/page.tsx:901` (spending donut `Pie`) — UNFENCED.
 *   8. `app/dashboard/page.tsx:1143` (forecast `planned` Bar) — UNFENCED.
 *      `dashboard-transfer-collapse` clicks `bars[0]` only, which is item 3.
 *
 * ⚠ On 4 and 5, precisely: `tests/components/dashboard/chart-widgets.test.tsx`
 * stubs `Bar` as a **pass-through** (`<button onClick={() => onClick?.({}, 0)}>`),
 * so deleting either handler DOES turn that suite red — verified by injection.
 * What is not fenced there is the *wiring*: the stub synthesizes `({}, 0)`
 * rather than letting Recharts supply the arguments, and it hardcodes index 0,
 * so an index-mapping bug survives. That is exactly the failure class the third
 * test below exists to catch, which is why the distinction is worth stating
 * rather than filing them as "untested".
 *
 * ⚠ Items 6-8 need this helper *plus* removal of local stubs in their existing
 * suites, which is larger than TBD-287's stated scope. Recorded, not silently
 * left.
 *
 * ## What this file does NOT cover
 *
 * The component's prop contract only. `app/budgets/page.tsx` turns the name
 * into `router.push("/transactions?category=...")`; renaming that query param,
 * dropping `encodeURIComponent` (a category like `Food & Drink` would break the
 * URL), or deleting the push leaves every assertion here green.
 */
import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("recharts", async () => {
  const { rechartsWithFixedSize } = await import("@/tests/utils/recharts");
  return rechartsWithFixedSize();
});

import BudgetOverviewChart, {
  type BudgetOverviewDatum,
} from "@/app/budgets/BudgetOverviewChart";

const DATA: BudgetOverviewDatum[] = [
  { name: "Groceries", spent: 120, remaining: 80, over: 0 },
  { name: "Transport", spent: 300, remaining: 0, over: 50 },
];

const CELL_META = [
  { category_id: 1, percent_used: 60 },
  { category_id: 2, percent_used: 117 },
];

/**
 * Returns the rectangles of the FIRST `<Bar>` layer — the `spent` series.
 *
 * Scoped through `.recharts-bar` rather than indexing a flat
 * `.recharts-bar-rectangle` list on purpose. A flat list mixes all three
 * stacked series, and its ordering depends on child order and on Recharts
 * dropping zero-width rectangles. Adding `zIndex` to the `spent` Bar (which
 * portals its layer elsewhere in the SVG) or reordering the three `<Bar>`
 * children would silently repoint a flat index onto a bar with no handler, and
 * the failure would surface as "spy not called" — a message naming nothing
 * about ordering.
 */
function spentBars(container: HTMLElement) {
  const spentLayer = container.querySelectorAll(".recharts-bar")[0];
  return spentLayer.querySelectorAll(".recharts-bar-rectangle");
}

function renderChart(onBarClick = vi.fn()) {
  const result = render(
    <BudgetOverviewChart
      budgetChartData={DATA}
      cellMeta={CELL_META}
      onBarClick={onBarClick}
    />,
  );
  return { ...result, onBarClick };
}

describe("BudgetOverviewChart bar click", () => {
  it("renders one real Recharts rectangle per row in the spent series", () => {
    // GUARD for the helper, and the precondition every assertion below rests
    // on. Without the fixed-size stub this is 0 — which is exactly how the
    // interaction stayed untested: an assertion on the handler would pass
    // vacuously because the click target never existed.
    //
    // `DATA.length` rather than a hardcoded total: the `spent` Bar carries a
    // custom `shape`, which exempts it from Recharts' zero-width filtering, so
    // one rectangle per row is invariant here. Counting the whole chart would
    // pin that filtering behaviour instead, and go red against correct code if
    // a fixture row changed or Recharts stopped filtering.
    const { container } = renderChart();
    expect(spentBars(container)).toHaveLength(DATA.length);
  });

  it("calls onBarClick with the clicked category name", () => {
    // FENCE. Wrong implementation killed: removing the `onClick` from the
    // `spent` Bar (app/budgets/BudgetOverviewChart.tsx:60), moving it to the
    // `remaining`/`over` Bar, or reading a field the datum does not carry.
    //
    // ⚠ Knowingly NOT killed: dropping the `|| data?.payload?.name` fallback.
    // Recharts builds its click payload as `{...entry, payload: entry}`, so
    // both disjuncts are the same string and the second arm is masked by the
    // first. It is dead either way; this test cannot tell.
    const { container, onBarClick } = renderChart();

    fireEvent.click(spentBars(container)[0]);

    expect(onBarClick).toHaveBeenCalledTimes(1);
    // Asserts the ARGUMENT, not merely that something fired. The name drives a
    // navigation, so a handler wired to the wrong datum field would send the
    // user to the wrong category while still "being called".
    expect(onBarClick).toHaveBeenCalledWith("Groceries");
  });

  it("distinguishes bars — clicking the second reports the second category", () => {
    // FENCE. Kills a handler hardcoded to index 0, or one reading `cellMeta[0]`
    // / a captured first datum. A single-row fixture cannot see either: with
    // one bar, "the right answer" and "always the first" agree.
    const { container, onBarClick } = renderChart();
    const bars = spentBars(container);
    expect(bars.length).toBeGreaterThanOrEqual(2);

    fireEvent.click(bars[1]);

    expect(onBarClick).toHaveBeenCalledTimes(1);
    expect(onBarClick).toHaveBeenCalledWith("Transport");
  });
});
