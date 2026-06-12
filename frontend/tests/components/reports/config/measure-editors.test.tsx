/**
 * Single- and multi-series measure editors extracted from ConfigRail.
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
  it("appends a default series via measure-add", () => {
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
    fireEvent.click(screen.getByTestId("measure-add"));
    expect(calls.at(-1)).toEqual([
      { measure: { agg: "sum", field: "amount" } },
      { measure: { agg: "avg", field: "amount" } },
      { measure: { agg: "sum", field: "amount" } },
    ]);
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
