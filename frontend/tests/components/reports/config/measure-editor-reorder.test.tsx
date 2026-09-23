/**
 * TBD-431. Reorder fences for MeasuresEditor's move-up/move-down buttons.
 *
 * ⚠ Revision 1's fences here were vacuous: they rendered a STATIC widget
 * whose onChange merely pushed to an array, so the component never
 * re-rendered with the new order and the focus effect never ran. Every test
 * below drives a CONTROLLED wrapper (`useState`) so a move genuinely
 * re-renders MeasuresEditor with the mutated array, the way DataTab does.
 */
import { useState } from "react";

import { renderWithSWR, fireEvent, screen } from "../../../utils/render-with-swr";

import MeasuresEditor from "@/components/reports/config/MeasuresEditor";
import type { LineWidget, SeriesConfig, TableWidget, Widget } from "@/lib/reports/types";

function makeLine(measures: SeriesConfig[]): LineWidget {
  return {
    id: "w_line",
    type: "line",
    title: "Line",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: { dataset: "transactions", measures, dimensions: ["month"] },
  };
}

function makeTable(measures: SeriesConfig[]): TableWidget {
  return {
    id: "w_table",
    type: "table",
    title: "Table",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: { dataset: "transactions", measures, dimensions: ["category"] },
  };
}

/** Controlled wrapper: onChange actually updates the rendered widget. */
function Controlled({
  make,
  initial,
  onChangeSpy,
}: {
  make: (m: SeriesConfig[]) => Widget & {
    config: LineWidget["config"] | TableWidget["config"];
  };
  initial: SeriesConfig[];
  onChangeSpy?: (m: SeriesConfig[]) => void;
}) {
  const [measures, setMeasures] = useState(initial);
  return (
    <MeasuresEditor
      widget={make(measures) as never}
      onChange={(next) => {
        onChangeSpy?.(next);
        setMeasures(next);
      }}
    />
  );
}

const THREE: SeriesConfig[] = [
  { measure: { agg: "sum", field: "amount" }, label: "First" },
  { measure: { agg: "avg", field: "amount" }, label: "Second" },
  { measure: { agg: "count", field: "id" }, label: "Third" },
];

function liveRegion() {
  return document.querySelector('[role="status"]') as HTMLElement;
}

describe("MeasuresEditor reorder (TBD-431)", () => {
  it("fence reorder-copies-array: never mutates in place", () => {
    const original: SeriesConfig[] = THREE.map((s) => ({ ...s }));
    const originalClone = JSON.parse(JSON.stringify(original));
    const calls: SeriesConfig[][] = [];
    renderWithSWR(
      <Controlled make={makeLine} initial={original} onChangeSpy={(m) => calls.push(m)} />,
    );
    fireEvent.click(screen.getByTestId("measure-move-down-0"));
    expect(calls).toHaveLength(1);
    // The received array is a new reference...
    expect(calls[0]).not.toBe(original);
    // ...and the array captured BEFORE the click was never mutated through a
    // stray alias (`copy = measures; copy[i] = …; onChange([...copy])`).
    expect(original).toEqual(originalClone);
  });

  it("fence reorder-order: moving idx 1 up yields [m1, m0, m2]", () => {
    const calls: SeriesConfig[][] = [];
    renderWithSWR(
      <Controlled make={makeLine} initial={THREE} onChangeSpy={(m) => calls.push(m)} />,
    );
    fireEvent.click(screen.getByTestId("measure-move-up-1"));
    expect(calls.at(-1)).toEqual([THREE[1], THREE[0], THREE[2]]);
  });

  it("fence reorder-carries-label: the label travels with the measure", () => {
    const calls: SeriesConfig[][] = [];
    renderWithSWR(
      <Controlled make={makeLine} initial={THREE} onChangeSpy={(m) => calls.push(m)} />,
    );
    fireEvent.click(screen.getByTestId("measure-move-up-1"));
    const moved = calls.at(-1)!;
    expect(moved[0].label).toBe("Second");
    expect(moved[0].measure).toEqual(THREE[1].measure);
  });

  it("fence reorder-boundary-no-op: idx 0 up is a no-op that stays focusable", () => {
    const calls: SeriesConfig[][] = [];
    renderWithSWR(
      <Controlled make={makeLine} initial={THREE} onChangeSpy={(m) => calls.push(m)} />,
    );
    const btn = screen.getByTestId("measure-move-up-0");
    expect(btn).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(btn);
    expect(calls).toHaveLength(0);
    expect(liveRegion().textContent).toBe("Series 1 is already first");
    btn.focus();
    expect(document.activeElement).toBe(btn);
    expect(btn).toBeEnabled();
  });

  it("fence reorder-boundary-no-op: last idx down is a no-op that stays focusable", () => {
    const calls: SeriesConfig[][] = [];
    renderWithSWR(
      <Controlled make={makeLine} initial={THREE} onChangeSpy={(m) => calls.push(m)} />,
    );
    const btn = screen.getByTestId("measure-move-down-2");
    expect(btn).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(btn);
    expect(calls).toHaveLength(0);
    expect(liveRegion().textContent).toBe("Series 3 is already last");
    btn.focus();
    expect(document.activeElement).toBe(btn);
    expect(btn).toBeEnabled();
  });

  it("fence reorder-focus-follows-moved-item: DOWN keeps focus on the down button", () => {
    // ⚠ The up-direction twin below cannot see a hardcoded `dir: "up"`. Spec
    // §5 names this exact failure: press Down on a series, press Enter again,
    // and if focus landed on the UP button the second press undoes the first,
    // making a two-step move impossible -- while an up-only fence stays green.
    renderWithSWR(<Controlled make={makeLine} initial={THREE} />);
    fireEvent.click(screen.getByTestId("measure-move-down-0"));
    expect(document.activeElement).toBe(
      screen.getByTestId("measure-move-down-1"),
    );
    // Slot 1 holds the series that moved DOWN, not slot 1's old occupant.
    expect(screen.getByLabelText("Series 2 label")).toHaveValue("First");
  });

  it("fence reorder-focus-follows-moved-item: move idx 2 -> 1 of 3", () => {
    renderWithSWR(<Controlled make={makeLine} initial={THREE} />);
    fireEvent.click(screen.getByTestId("measure-move-up-2"));
    const target = screen.getByTestId("measure-move-up-1");
    expect(document.activeElement).toBe(target);
    // The row now at index 1 carries the MOVED series' label, not slot 1's
    // old occupant — kills a slot-tautological "focus the same testid" fix.
    expect(
      screen.getByLabelText("Series 2 label"),
    ).toHaveValue("Third");
  });

  it("fence reorder-focus-not-stolen-by-typing: a label keystroke keeps focus in the input", () => {
    renderWithSWR(<Controlled make={makeLine} initial={THREE} />);
    fireEvent.click(screen.getByTestId("measure-move-down-0"));
    const input = screen.getByLabelText("Series 1 label") as HTMLInputElement;
    input.focus();
    fireEvent.change(input, { target: { value: "R" } });
    expect(document.activeElement).toBe(input);
  });

  it("fence reorder-announced: live region is empty until the first move, then names it", () => {
    renderWithSWR(<Controlled make={makeLine} initial={THREE} />);
    expect(liveRegion()).toBeInTheDocument();
    expect(liveRegion().textContent).toBe("");
    fireEvent.click(screen.getByTestId("measure-move-up-1"));
    expect(liveRegion().textContent).toBe("Series 2 moved to position 1 of 3");
  });

  it("fence reorder-announces-column-for-table: table widgets say column", () => {
    renderWithSWR(<Controlled make={makeTable} initial={THREE} />);
    fireEvent.click(screen.getByTestId("measure-move-up-1"));
    expect(liveRegion().textContent).toBe("Column 2 moved to position 1 of 3");
  });

  it("fence reorder-button-accessible-names: branch on widget type", () => {
    const { unmount } = renderWithSWR(<Controlled make={makeLine} initial={THREE} />);
    expect(screen.getByTestId("measure-move-up-1")).toHaveAccessibleName(
      "Move series 2 up",
    );
    expect(screen.getByTestId("measure-move-down-1")).toHaveAccessibleName(
      "Move series 2 down",
    );
    unmount();
    renderWithSWR(<Controlled make={makeTable} initial={THREE} />);
    expect(screen.getByTestId("measure-move-up-1")).toHaveAccessibleName(
      "Move column 2 up",
    );
    expect(screen.getByTestId("measure-move-down-1")).toHaveAccessibleName(
      "Move column 2 down",
    );
  });

  it("guard reorder-hidden-when-single: one series has no move buttons", () => {
    renderWithSWR(
      <Controlled make={makeLine} initial={[THREE[0]]} />,
    );
    expect(screen.queryByTestId("measure-move-up-0")).not.toBeInTheDocument();
    expect(screen.queryByTestId("measure-move-down-0")).not.toBeInTheDocument();
  });
});
