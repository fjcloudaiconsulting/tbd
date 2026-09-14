/**
 * TBD-427: the shared DOM legend (L7, L12).
 *
 * L7 kills label text coloured with the series hue (recharts'
 * `DefaultLegendContent` does exactly that, and five of eight light-theme hues
 * fall below 4.5:1 as text) and an off-scale `text-[11px]`. L12 pins TBD-430's
 * keyboard-reachable scroll cap as the markup moves into this component.
 */
import { render, screen } from "@testing-library/react";

import WidgetLegend from "@/components/reports/widgets/WidgetLegend";

const ITEMS = [
  { label: "Alpha", color: "var(--color-chart-2)" },
  { label: "Other", color: "var(--color-border-strong)" },
];

it("L7: only the swatch carries the hue; the list is text-secondary at text-xs", () => {
  render(<WidgetLegend testidPrefix="x" label="Series in T" items={ITEMS} />);
  const list = screen.getByTestId("x-legend");
  expect(list.tagName).toBe("UL");

  const offenders = [...list.querySelectorAll<HTMLElement>("*")]
    .filter((el) => el.getAttribute("data-testid") !== "x-legend-swatch")
    .filter((el) => {
      const style = el.getAttribute("style") ?? "";
      return el.style.color !== "" || /var\(--color-/.test(style);
    })
    .map((el) => el.outerHTML);
  expect(offenders).toEqual([]);
  expect(list.getAttribute("style")).toBeNull();
  expect(list.classList.contains("text-text-secondary")).toBe(true);
  expect(list.classList.contains("text-xs")).toBe(true);

  // Non-vacuity: the colour did reach the DOM, on the swatch.
  expect(
    screen.getAllByTestId("x-legend-swatch").map((s) => s.style.backgroundColor),
  ).toEqual(ITEMS.map((i) => i.color));
  expect(
    screen.getAllByTestId("x-legend-item").map((li) => li.textContent),
  ).toEqual(["Alpha", "Other"]);
});

it("L12: the list stays a keyboard-reachable, height-capped scroll region", () => {
  render(<WidgetLegend testidPrefix="x" label="Series in T" items={ITEMS} />);
  const list = screen.getByRole("list", { name: "Series in T" });
  expect(list.getAttribute("tabindex")).toBe("0");
  expect(list.classList.contains("max-h-16")).toBe(true);
  expect(list.classList.contains("overflow-y-auto")).toBe(true);
});
