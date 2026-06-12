/**
 * WidgetEditorPopover — the floating-ui shell with a 3-tab frame.
 * Component-level mounts here are synchronous (anchorEl passed directly),
 * so no waitFor is needed. ResizeObserver is polyfilled globally in
 * vitest.setup.ts.
 */
import { renderWithSWR, fireEvent, screen } from "../../utils/render-with-swr";

import WidgetEditorPopover from "@/components/reports/WidgetEditorPopover";
import { apiFetch } from "@/lib/api";
import type {
  AreaWidget,
  BarWidget,
  KPIWidget,
  LineWidget,
  PieWidget,
  SparklineWidget,
  StackedBarWidget,
  TableWidget,
  Widget,
} from "@/lib/reports/types";

vi.mock("@/lib/api", () => ({ apiFetch: vi.fn() }));

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(
    () => Promise.resolve([]) as Promise<unknown>,
  );
});

let anchorEl: HTMLElement;
beforeEach(() => {
  anchorEl = document.createElement("div");
  document.body.appendChild(anchorEl);
});
afterEach(() => {
  anchorEl.remove();
});

function makeBar(): BarWidget {
  return {
    id: "w_bar",
    type: "bar",
    title: "Bar",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
    },
  };
}

function makeKpi(): KPIWidget {
  return {
    id: "w_kpi",
    type: "kpi",
    title: "KPI",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: { dataset: "transactions", measure: { agg: "sum", field: "amount" } },
  };
}

function makePie(): PieWidget {
  return {
    id: "w_pie",
    type: "pie",
    title: "Pie",
    grid: { x: 0, y: 0, w: 4, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
    },
  };
}

function makeSparkline(): SparklineWidget {
  return {
    id: "w_spark",
    type: "sparkline",
    title: "Spark",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["month"],
    },
  };
}

function makeLine(): LineWidget {
  return {
    id: "w_line",
    type: "line",
    title: "Line",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
    },
  };
}

function makeArea(): AreaWidget {
  return {
    id: "w_area",
    type: "area",
    title: "Area",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
    },
  };
}

function makeTable(): TableWidget {
  return {
    id: "w_table",
    type: "table",
    title: "Table",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["category"],
    },
  };
}

function makeStacked(): StackedBarWidget {
  return {
    id: "w_stacked",
    type: "stacked_bar",
    title: "Stacked",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "sum", field: "amount" } }],
      dimensions: ["month"],
    },
  };
}

function renderPopover(widget: Widget, props: Partial<{ onUpdate: (w: Widget) => void; onClose: () => void }> = {}) {
  return renderWithSWR(
    <WidgetEditorPopover
      widget={widget}
      canvasFilters={{}}
      anchorEl={anchorEl}
      onUpdate={props.onUpdate ?? (() => {})}
      onClose={props.onClose ?? (() => {})}
    />,
  );
}

describe("WidgetEditorPopover", () => {
  it("renders the dialog with the right role and aria-label", () => {
    renderPopover(makeBar());
    const dialog = screen.getByTestId("widget-editor-popover");
    expect(dialog).toHaveAttribute("role", "dialog");
    expect(dialog).toHaveAttribute("aria-label", "Widget settings");
  });

  it("defaults to the Data tab", () => {
    renderPopover(makeBar());
    expect(screen.getByLabelText("Data source")).toBeInTheDocument();
    expect(screen.getByLabelText("Aggregation")).toBeInTheDocument();
    // Style/Filters panels not visible: no title input, no Filters block.
    expect(screen.queryByLabelText("Widget title")).not.toBeInTheDocument();
  });

  it("exposes three tabs with the active one selected", () => {
    renderPopover(makeBar());
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(3);
    expect(screen.getByRole("tab", { name: /data/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("clicking Style shows the title input; clicking Filters shows FilterEditor", () => {
    renderPopover(makeBar());
    fireEvent.click(screen.getByRole("tab", { name: /style/i }));
    expect(screen.getByLabelText("Widget title")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /style/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.click(screen.getByRole("tab", { name: /filters/i }));
    expect(screen.getByText("Filters (this widget)")).toBeInTheDocument();
  });

  it("kpi: no dimensions on Data, compare checkbox on Style", () => {
    renderPopover(makeKpi());
    expect(screen.queryByLabelText("Primary dimension")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /style/i }));
    expect(screen.getByLabelText("Compare to prior period")).toBeInTheDocument();
  });

  it("pie: single measure + primary, no secondary; Style top_n", () => {
    renderPopover(makePie());
    expect(screen.getByLabelText("Primary dimension")).toBeInTheDocument();
    expect(screen.queryByLabelText("Secondary dimension")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /style/i }));
    expect(screen.getByLabelText("Top N slices")).toBeInTheDocument();
  });

  it("sparkline: primary only, no secondary, no top_n", () => {
    renderPopover(makeSparkline());
    expect(screen.getByLabelText("Primary dimension")).toBeInTheDocument();
    expect(screen.queryByLabelText("Secondary dimension")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /style/i }));
    expect(screen.queryByLabelText("Top N slices")).not.toBeInTheDocument();
  });

  it("bar: primary + Break down by, no Style type-knob", () => {
    renderPopover(makeBar());
    expect(screen.getByLabelText("Break down by")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /style/i }));
    expect(screen.queryByLabelText("Top N slices")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Stack series")).not.toBeInTheDocument();
  });

  it("line: MeasuresEditor add/remove; no secondary", () => {
    renderPopover(makeLine());
    expect(screen.getByTestId("measure-add")).toBeInTheDocument();
    expect(screen.queryByLabelText("Secondary dimension")).not.toBeInTheDocument();
  });

  it("area: stacked checkbox on Style", () => {
    renderPopover(makeArea());
    fireEvent.click(screen.getByRole("tab", { name: /style/i }));
    expect(screen.getByLabelText("Stack series")).toBeInTheDocument();
  });

  it("table: secondary dimension present", () => {
    renderPopover(makeTable());
    expect(screen.getByLabelText("Secondary dimension")).toBeInTheDocument();
  });

  it("Escape closes the popover", () => {
    const onClose = vi.fn();
    renderPopover(makeBar(), { onClose });
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  it("outside pointerdown closes the popover", () => {
    const onClose = vi.fn();
    renderPopover(makeBar(), { onClose });
    fireEvent.pointerDown(document.body);
    expect(onClose).toHaveBeenCalled();
  });

  it("the Close button calls onClose", () => {
    const onClose = vi.fn();
    renderPopover(makeBar(), { onClose });
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it("renders nothing when anchorEl is null", () => {
    renderWithSWR(
      <WidgetEditorPopover
        widget={makeBar()}
        canvasFilters={{}}
        anchorEl={null}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.queryByTestId("widget-editor-popover")).not.toBeInTheDocument();
  });

  it("closes when the anchor detaches (staleness guard)", () => {
    const onClose = vi.fn();
    const detached = document.createElement("div");
    // Intentionally NOT appended to the document → not connected.
    renderWithSWR(
      <WidgetEditorPopover
        widget={makeBar()}
        canvasFilters={{}}
        anchorEl={detached}
        onUpdate={() => {}}
        onClose={onClose}
      />,
    );
    expect(onClose).toHaveBeenCalled();
  });

  it("resets to the Data tab when the selected widget changes", () => {
    const { rerender } = renderPopover(makeBar());
    fireEvent.click(screen.getByRole("tab", { name: /style/i }));
    expect(screen.getByLabelText("Widget title")).toBeInTheDocument();

    rerender(
      <WidgetEditorPopover
        widget={makeKpi()}
        canvasFilters={{}}
        anchorEl={anchorEl}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    // Back on Data: the Style-only title input is gone, Data source is shown.
    expect(screen.queryByLabelText("Widget title")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Data source")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /data/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("ArrowRight moves the tab selection (roving)", () => {
    renderPopover(makeBar());
    const dataTab = screen.getByRole("tab", { name: /data/i });
    dataTab.focus();
    fireEvent.keyDown(dataTab, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: /filters/i })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: /filters/i })).toHaveAttribute(
      "tabindex",
      "0",
    );
    expect(dataTab).toHaveAttribute("tabindex", "-1");
  });

  it("title edit on the Style tab fires onUpdate with the new title", () => {
    const onUpdate = vi.fn();
    renderPopover(makeBar(), { onUpdate });
    fireEvent.click(screen.getByRole("tab", { name: /style/i }));
    fireEvent.change(screen.getByLabelText("Widget title"), {
      target: { value: "Spending" },
    });
    expect(onUpdate).toHaveBeenCalled();
    const last = onUpdate.mock.calls.at(-1)?.[0] as Widget;
    expect(last.title).toBe("Spending");
  });

  it("changing the aggregation on the Data tab fires onUpdate", () => {
    const onUpdate = vi.fn();
    renderPopover(makeBar(), { onUpdate });
    fireEvent.change(screen.getByLabelText("Aggregation"), {
      target: { value: "count" },
    });
    expect(onUpdate).toHaveBeenCalled();
    const last = onUpdate.mock.calls.at(-1)?.[0] as BarWidget;
    expect(last.config.measure).toEqual({ agg: "count", field: "amount" });
  });

  it("stacked_bar: Style tab shows the 'Stack mode' control", () => {
    renderPopover(makeStacked());
    fireEvent.click(screen.getByRole("tab", { name: /style/i }));
    expect(screen.getByText("Stack mode")).toBeInTheDocument();
    expect(screen.getByLabelText("Stack series")).toBeInTheDocument();
  });
});
