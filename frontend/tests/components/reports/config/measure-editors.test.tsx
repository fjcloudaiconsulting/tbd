/**
 * Single- and multi-series measure editors extracted from the original config rail.
 * These pin the onChange payloads and the add/remove/cap behaviour that
 * downstream tabs (and the old rail) depend on.
 */
import { renderWithSWR, fireEvent, screen } from "../../../utils/render-with-swr";

import { TRANSACTIONS_ENTRY } from "../../../utils/mock-report-sources";

import SingleMeasureEditor from "@/components/reports/config/SingleMeasureEditor";
import { measureOptionsFor } from "@/components/reports/config/controlConstants";
import MeasuresEditor from "@/components/reports/config/MeasuresEditor";
import type {
  LineWidget,
  Measure,
  SeriesConfig,
  TableWidget,
} from "@/lib/reports/types";

/** The transactions source's published measures as labelled options. */
const TRANSACTIONS_OPTIONS = measureOptionsFor(TRANSACTIONS_ENTRY);

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
  // ⚠ TBD-402. There is ONE select now, over the catalog's published
  // measures, not an Aggregation select beside a Field select. The old pair
  // of tests drove each half independently and asserted the cross product —
  // which is exactly the defect: `count(amount)` is not a transactions
  // measure (it counts `id`), and `validate_against_catalog` checks the
  // FIELD only, so such a pair renders a meaningless number rather than
  // 422ing. Both halves are replaced by selecting a real catalog measure.
  it("selecting a catalog measure reports that exact published pair", () => {
    const calls: Measure[] = [];
    renderWithSWR(
      <SingleMeasureEditor
        measure={{ agg: "sum", field: "amount" }}
        onChange={(m) => calls.push(m)}
        measureOptions={TRANSACTIONS_OPTIONS}
      />,
    );
    // `count_rows` is count(id) — note the field moves WITH the agg, which
    // is the whole point of collapsing the two selects.
    fireEvent.change(screen.getByLabelText("Measure"), {
      target: { value: "count_rows" },
    });
    expect(calls.at(-1)).toEqual({ agg: "count", field: "id" });

    fireEvent.change(screen.getByLabelText("Measure"), {
      target: { value: "avg_amount" },
    });
    expect(calls.at(-1)).toEqual({ agg: "avg", field: "amount" });
  });

  it("offers ONLY the catalog's measures, so an invalid pair is unrepresentable", () => {
    renderWithSWR(
      <SingleMeasureEditor
        measure={{ agg: "sum", field: "amount" }}
        onChange={() => {}}
        measureOptions={TRANSACTIONS_OPTIONS}
      />,
    );
    const select = screen.getByLabelText("Measure") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      "sum_amount",
      "avg_amount",
      "count_rows",
    ]);
  });

  it("disables the select while the catalog is unresolved, showing the current measure", () => {
    // Mutant this kills: falling back to a static option list before the
    // catalog resolves. That fallback is precisely how a pair the source
    // does not publish got selected in the first place.
    const calls: Measure[] = [];
    renderWithSWR(
      <SingleMeasureEditor
        measure={{ agg: "sum", field: "amount" }}
        onChange={(m) => calls.push(m)}
      />,
    );
    const select = screen.getByLabelText("Measure") as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    expect(Array.from(select.options)).toHaveLength(1);
    expect(select.options[0].textContent).toBe("Sum of Amount");
    expect(calls).toEqual([]);
  });

  it("shows an unpublished persisted measure instead of silently rewriting it", () => {
    // A legacy `distinct(id)` — `distinct` is published by NO source, so this
    // is reachable from saved layouts. Rewriting it would change the number a
    // saved report renders without telling anyone.
    const calls: Measure[] = [];
    renderWithSWR(
      <SingleMeasureEditor
        measure={{ agg: "distinct", field: "id" }}
        onChange={(m) => calls.push(m)}
        measureOptions={TRANSACTIONS_OPTIONS}
      />,
    );
    const select = screen.getByLabelText("Measure") as HTMLSelectElement;
    expect(select.value).toBe("__unsupported__");
    expect(select.options[0].textContent).toContain("(unsupported)");
    expect(
      screen.getByText(/not offered by the selected data source/i),
    ).toBeInTheDocument();
    // Untouched until the user picks something.
    expect(calls).toEqual([]);
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
