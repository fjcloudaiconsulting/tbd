/**
 * The extracted mutation closures must produce the SAME onUpdate payloads
 * they did inline in ConfigRail. Renders a tiny harness that calls the
 * setters and records every payload.
 */
import { renderWithSWR, fireEvent, screen } from "../../../utils/render-with-swr";

import { useWidgetMutations } from "@/components/reports/config/useWidgetMutations";
import type { BarWidget, Widget } from "@/lib/reports/types";

function Harness({
  widget,
  onUpdate,
}: {
  widget: Widget;
  onUpdate: (next: Widget) => void;
}) {
  const m = useWidgetMutations(widget, onUpdate);
  return (
    <div>
      <button onClick={() => m.setTitle("Renamed")}>title</button>
      <button onClick={() => m.setSecondaryDimension("")}>clear-secondary</button>
      <button onClick={() => m.setSecondaryDimension("account")}>set-secondary</button>
    </div>
  );
}

function makeBar(): BarWidget {
  return {
    id: "w_bar",
    type: "bar",
    title: "Bar",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category", "account"],
    },
  };
}

describe("useWidgetMutations", () => {
  it("setTitle replaces the widget title", () => {
    const updates: Widget[] = [];
    renderWithSWR(<Harness widget={makeBar()} onUpdate={(w) => updates.push(w)} />);
    fireEvent.click(screen.getByText("title"));
    expect(updates.at(-1)?.title).toBe("Renamed");
  });

  it("setSecondaryDimension('') splices dimensions[1]", () => {
    const updates: Widget[] = [];
    renderWithSWR(<Harness widget={makeBar()} onUpdate={(w) => updates.push(w)} />);
    fireEvent.click(screen.getByText("clear-secondary"));
    expect((updates.at(-1) as BarWidget).config.dimensions).toEqual(["category"]);
  });

  it("setSecondaryDimension('account') sets dimensions[1]", () => {
    const updates: Widget[] = [];
    const bar = makeBar();
    bar.config.dimensions = ["category"];
    renderWithSWR(<Harness widget={bar} onUpdate={(w) => updates.push(w)} />);
    fireEvent.click(screen.getByText("set-secondary"));
    expect((updates.at(-1) as BarWidget).config.dimensions).toEqual([
      "category",
      "account",
    ]);
  });
});
