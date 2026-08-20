/**
 * TBD-432 — the report value axes size themselves to their content.
 *
 * The three reports charts hard-coded `width={92}` on their value axis, sized
 * for the widest formatted currency tick. That over-reserved on every narrower
 * one: 5% of a `w:12` widget, 27% at `w:4`, and 66% at the grid minimum, where
 * the plot area was a third of the card. It could also CLIP a value wider than
 * 92px, which auto cannot.
 *
 * `width="auto"` (recharts >= 3 types the prop as `number | 'auto'`) hands the
 * measurement to `getCalculatedYAxisWidth`.
 *
 * ## What this fence can and cannot prove
 *
 * ⚠ jsdom has no text metrics, so recharts measures every tick as 0 wide and
 * `auto` collapses the axis to ~8px REGARDLESS of content. So this fence can
 * only prove the axis is no longer reserving a fixed 92px -- which is exactly
 * enough to kill a revert to `width={92}` -- and CANNOT prove it is correctly
 * sized. Claiming otherwise would be a fence asserting something it never
 * evaluates.
 *
 * The sizing itself was verified in a real browser on the seeded
 * `category_deep_dive` template: widest label `€2,600.00` measured 54.3px, the
 * axis line moved from x=92 to x=62, every tick label rendered in full, and no
 * label overlapped the plot area. Recorded in the TBD-432 PR body.
 */
import { render, waitFor } from "@testing-library/react";

import BarWidgetChart from "@/components/reports/widgets/BarWidgetChart";

vi.mock("recharts", async () => {
  const { rechartsWithFixedSize } = await import("@/tests/utils/recharts");
  return rechartsWithFixedSize();
});

/** x offset where the plot area begins, read off the first bar's path. */
function plotStartX(container: HTMLElement): number {
  const path = container.querySelector(".recharts-bar-rectangle path");
  expect(path).not.toBeNull();
  const d = path!.getAttribute("d") ?? "";
  const m = /^M([\d.]+)/.exec(d);
  expect(m, `could not read a start x from path d=${d}`).not.toBeNull();
  return Number(m![1]);
}

function chart() {
  return (
    <BarWidgetChart
      rows={[
        { label: "A", value: 5 },
        { label: "B", value: 9 },
      ]}
      sliced={false}
      stacked={false}
      secondaryValues={[]}
      seriesKeys={[]}
      sliceColors={[]}
      valueName="Amount"
      format="number"
    />
  );
}

it("does not reserve a fixed 92px for the value axis", async () => {
  const { container } = render(chart());
  await waitFor(() =>
    expect(container.querySelector(".recharts-bar-rectangle")).not.toBeNull(),
  );

  // With `width={92}` the plot cannot begin before x=92. Under `auto` it
  // begins far left of that. The threshold is deliberately loose: this asserts
  // "not the hardcoded reservation", not a specific measured width, because
  // jsdom cannot measure text.
  expect(plotStartX(container)).toBeLessThan(60);
});

it("still renders a value axis with its ticks", async () => {
  const { container } = render(chart());
  await waitFor(() =>
    expect(container.querySelector(".recharts-yAxis")).not.toBeNull(),
  );
  const ticks = container.querySelectorAll(
    ".recharts-yAxis .recharts-cartesian-axis-tick",
  );
  expect(ticks.length).toBeGreaterThan(0);
});
