/**
 * ForecastPlanChart — the bar-click contract (TBD-287).
 *
 * Companion to `tests/components/budgets/budget-overview-chart.test.tsx`, which
 * carries the full chart-interaction audit.
 *
 * ## Why this file exists
 *
 * `ForecastPlanChart.tsx:64` carries
 * `onClick={(data) => onBarClick(data?.name || data?.payload?.name)}` — the
 * same expression, character for character, as the Budgets chart. It was
 * unfenced for the same reason: the component's only existing test
 * (`tests/app/forecast-plans-page.test.tsx`) stubs `BarChart` and `Bar`, so it
 * asserts against its own stubs and never invokes the real handler.
 *
 * Two identical handlers in two dynamic-imported chart components is exactly
 * the shape where fencing one and not the other ages badly: the next person
 * refactors the pair and only half the change is covered.
 *
 * ## Scope
 *
 * The `planned` Bar only. The sibling `actual` Bar has no handler, which is
 * itself worth pinning — see the last test.
 */
import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("recharts", async () => {
  const { rechartsWithFixedSize } = await import("@/tests/utils/recharts");
  return rechartsWithFixedSize();
});

import ForecastPlanChart, {
  type ForecastPlanChartDatum,
} from "@/app/forecast-plans/ForecastPlanChart";

const DATA: ForecastPlanChartDatum[] = [
  { categoryId: 1, name: "Groceries", planned: 400, actual: 380 },
  { categoryId: 2, name: "Transport", planned: 150, actual: 220 },
];

/** Rectangles of the FIRST `<Bar>` layer — the `planned` series. Scoped rather
 * than flat-indexed for the reasons given in the Budgets chart test. */
function plannedBars(container: HTMLElement) {
  const layer = container.querySelectorAll(".recharts-bar")[0];
  return layer.querySelectorAll(".recharts-bar-rectangle");
}

function renderChart(onBarClick = vi.fn()) {
  const result = render(
    <ForecastPlanChart chartData={DATA} onBarClick={onBarClick} />,
  );
  return { ...result, onBarClick };
}

describe("ForecastPlanChart bar click", () => {
  it("renders one real Recharts rectangle per row in the planned series", () => {
    // GUARD. 0 without the fixed-size stub.
    const { container } = renderChart();
    expect(plannedBars(container)).toHaveLength(DATA.length);
  });

  it("calls onBarClick with the clicked category name", () => {
    // FENCE. Wrong implementation killed: removing the `onClick` from the
    // `planned` Bar (app/forecast-plans/ForecastPlanChart.tsx:64).
    const { container, onBarClick } = renderChart();

    fireEvent.click(plannedBars(container)[0]);

    expect(onBarClick).toHaveBeenCalledTimes(1);
    expect(onBarClick).toHaveBeenCalledWith("Groceries");
  });

  it("distinguishes bars — clicking the second reports the second category", () => {
    // FENCE. Kills a handler hardcoded to index 0.
    const { container, onBarClick } = renderChart();
    const bars = plannedBars(container);
    expect(bars.length).toBeGreaterThanOrEqual(2);

    fireEvent.click(bars[1]);

    expect(onBarClick).toHaveBeenCalledTimes(1);
    expect(onBarClick).toHaveBeenCalledWith("Transport");
  });

  it("does not fire for the actual series, which has no handler", () => {
    // FENCE against the plausible wrong fix: wiring `onClick` onto every Bar
    // rather than the `planned` one. Without this, adding a handler to the
    // `actual` Bar — changing which clicks navigate — passes every other
    // assertion here.
    const { container, onBarClick } = renderChart();
    const actualLayer = container.querySelectorAll(".recharts-bar")[1];
    const actualBars = actualLayer.querySelectorAll(".recharts-bar-rectangle");
    expect(actualBars.length).toBeGreaterThan(0);

    fireEvent.click(actualBars[0]);

    expect(onBarClick).not.toHaveBeenCalled();
  });
});
