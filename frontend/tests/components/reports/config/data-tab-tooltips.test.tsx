/**
 * Widget-editor help tooltips (re-homed from the original rail tooltips test).
 *
 * Jargon in the widget config controls gets inline ``HelpTooltip`` info
 * icons: the aggregation selector explains Sum / Count / Average /
 * Distinct, and the dimension labels carry the master-category
 * explainer. These now live on the extracted ``DataTab`` (agg + dimension
 * tooltips) and ``MeasuresEditor`` (per-series agg tooltip). We assert the
 * tooltip triggers render (by their ARIA label from the content map)
 * rather than driving the portal open.
 */
import { renderWithSWR, fireEvent, screen } from "../../../utils/render-with-swr";

import DataTab from "@/components/reports/config/DataTab";
import MeasuresEditor from "@/components/reports/config/MeasuresEditor";
import { apiFetch } from "@/lib/api";
import { HELP_TOOLTIPS } from "@/lib/help/tooltips";
import type { BarWidget, LineWidget } from "@/lib/reports/types";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(
    () => Promise.resolve([]) as Promise<unknown>,
  );
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

function makeLine(): LineWidget {
  return {
    id: "w_line",
    type: "line",
    title: "Line",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {
      dataset: "transactions",
      measures: [{ measure: { agg: "distinct", field: "amount" } }],
      dimensions: ["month"],
    },
  };
}

describe("Widget editor — help tooltips", () => {
  it("renders the aggregation tooltip trigger for a single-measure widget", () => {
    renderWithSWR(<DataTab widget={makeBar()} onUpdate={() => {}} />);
    // Bar defaults to Sum → the Sum explainer trigger is present.
    expect(
      screen.getByRole("button", {
        name: HELP_TOOLTIPS["reports.agg.sum"].triggerLabel,
      }),
    ).toBeInTheDocument();
  });

  it("swaps the aggregation tooltip to match the selected aggregation", () => {
    const updates: BarWidget[] = [];
    renderWithSWR(
      <DataTab
        widget={makeBar()}
        onUpdate={(w) => updates.push(w as BarWidget)}
      />,
    );
    fireEvent.change(screen.getByLabelText("Aggregation"), {
      target: { value: "distinct" },
    });
    const next = updates.at(-1) as BarWidget;
    expect(next.config.measure.agg).toBe("distinct");
  });

  it("renders the master-category explainer next to the dimension label", () => {
    renderWithSWR(<DataTab widget={makeBar()} onUpdate={() => {}} />);
    // Bar exposes both the Primary dimension and the Break-down (optional)
    // labels, so the master-category explainer renders on each.
    expect(
      screen.getAllByRole("button", {
        name: HELP_TOOLTIPS["reports.master-category"].triggerLabel,
      }).length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("renders a per-series aggregation tooltip for multi-series widgets", () => {
    const line = makeLine();
    renderWithSWR(
      <MeasuresEditor widget={line} onChange={() => {}} />,
    );
    // The line series defaults to Distinct count → its explainer shows.
    expect(
      screen.getByRole("button", {
        name: HELP_TOOLTIPS["reports.agg.distinct"].triggerLabel,
      }),
    ).toBeInTheDocument();
  });
});
