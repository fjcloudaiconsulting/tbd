/**
 * Single- and multi-series measure editors extracted from the original config rail.
 * These pin the onChange payloads and the add/remove/cap behaviour that
 * downstream tabs (and the old rail) depend on.
 */
import { renderWithSWR, fireEvent, screen } from "../../../utils/render-with-swr";

import SingleMeasureEditor from "@/components/reports/config/SingleMeasureEditor";
import MeasuresEditor from "@/components/reports/config/MeasuresEditor";
import type {
  LineWidget,
  Measure,
  SeriesConfig,
  TableWidget,
} from "@/lib/reports/types";

/** The transactions source's published (agg, field) pairs, in catalog order. */
const TRANSACTIONS_PAIRS: Measure[] = [
  { agg: "sum", field: "amount" },
  { agg: "avg", field: "amount" },
  { agg: "count", field: "id" },
];

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

describe("SingleMeasureEditor", () => {
  it("changing Aggregation reports the new agg, keeping the field", () => {
    const calls: Measure[] = [];
    renderWithSWR(
      <SingleMeasureEditor
        measure={{ agg: "sum", field: "amount" }}
        onChange={(m) => calls.push(m)}
      />,
    );
    fireEvent.change(screen.getByLabelText("Aggregation"), {
      target: { value: "count" },
    });
    expect(calls.at(-1)).toEqual({ agg: "count", field: "amount" });
  });

  it("changing Field reports the new field, keeping the agg", () => {
    const calls: Measure[] = [];
    renderWithSWR(
      <SingleMeasureEditor
        measure={{ agg: "sum", field: "amount" }}
        onChange={(m) => calls.push(m)}
      />,
    );
    fireEvent.change(screen.getByLabelText("Field"), {
      target: { value: "id" },
    });
    expect(calls.at(-1)).toEqual({ agg: "sum", field: "id" });
  });
});

describe("MeasuresEditor", () => {
  // ⚠ TBD-382 R7: this used to assert the DEFECT — the add button seeded
  // `{agg:"sum", field: fields[0]}`, i.e. a duplicate of series 1, which drew
  // pixel-identical on top of it. It now seeds the next unused CATALOG PAIR.
  it("appends the next unused catalog pair via measure-add", () => {
    const calls: SeriesConfig[][] = [];
    renderWithSWR(
      <MeasuresEditor
        widget={makeLine([
          { measure: { agg: "sum", field: "amount" } },
          { measure: { agg: "avg", field: "amount" } },
        ])}
        onChange={(m) => calls.push(m)}
        measurePairs={TRANSACTIONS_PAIRS}
      />,
    );
    fireEvent.click(screen.getByTestId("measure-add"));
    expect(calls.at(-1)).toEqual([
      { measure: { agg: "sum", field: "amount" } },
      { measure: { agg: "avg", field: "amount" } },
      { measure: { agg: "count", field: "id" } },
    ]);
  });

  it("refuses (and explains) once every catalog pair is already a series", () => {
    const calls: SeriesConfig[][] = [];
    renderWithSWR(
      <MeasuresEditor
        widget={makeLine(TRANSACTIONS_PAIRS.map((measure) => ({ measure })))}
        onChange={(m) => calls.push(m)}
        measurePairs={TRANSACTIONS_PAIRS}
      />,
    );
    const btn = screen.getByTestId("measure-add");
    expect(btn).toBeDisabled();
    expect(
      screen.getByTestId("measure-add-exhausted-help"),
    ).toBeInTheDocument();
    fireEvent.click(btn);
    expect(calls).toHaveLength(0);
  });

  it("is inert, with no explanation, while the catalog is unresolved", () => {
    renderWithSWR(
      <MeasuresEditor
        widget={makeLine([{ measure: { agg: "sum", field: "amount" } }])}
        onChange={() => {}}
      />,
    );
    expect(screen.getByTestId("measure-add")).toBeDisabled();
    expect(screen.queryByTestId("measure-add-exhausted-help")).toBeNull();
  });

  it("removes a series by index via measure-remove-1", () => {
    const calls: SeriesConfig[][] = [];
    renderWithSWR(
      <MeasuresEditor
        widget={makeLine([
          { measure: { agg: "sum", field: "amount" } },
          { measure: { agg: "avg", field: "amount" } },
        ])}
        onChange={(m) => calls.push(m)}
      />,
    );
    fireEvent.click(screen.getByTestId("measure-remove-1"));
    expect(calls.at(-1)).toEqual([{ measure: { agg: "sum", field: "amount" } }]);
  });

  it("hides remove when only one series remains", () => {
    renderWithSWR(
      <MeasuresEditor
        widget={makeLine([{ measure: { agg: "sum", field: "amount" } }])}
        onChange={() => {}}
      />,
    );
    expect(screen.queryByTestId("measure-remove-0")).not.toBeInTheDocument();
  });

  it("hides add once the series cap is reached", () => {
    const five: SeriesConfig[] = Array.from({ length: 5 }, () => ({
      measure: { agg: "sum", field: "amount" },
    }));
    renderWithSWR(
      <MeasuresEditor widget={makeLine(five)} onChange={() => {}} />,
    );
    expect(screen.queryByTestId("measure-add")).not.toBeInTheDocument();
  });

  it("labels table rows as Column N and caps at five columns", () => {
    const five: SeriesConfig[] = Array.from({ length: 5 }, () => ({
      measure: { agg: "sum", field: "amount" },
    }));
    renderWithSWR(
      <MeasuresEditor widget={makeTable(five)} onChange={() => {}} />,
    );
    expect(screen.getByText("Column 1")).toBeInTheDocument();
    expect(screen.queryByTestId("measure-add")).not.toBeInTheDocument();
  });
});
