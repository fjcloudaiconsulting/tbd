/**
 * DataTab — Sankey-specific knobs.
 *
 * Covers:
 *   (a) spending_granularity radio → onUpdate called with the new value
 *   (b) valid top_n (e.g. 5) → config.top_n === 5
 *   (c) negative / < 2 top_n (e.g. "-3") → config.top_n === undefined (clamped)
 *   (d) empty top_n input → config.top_n === undefined
 *   (e) top_n above 50 → clamped to 50
 *
 * Mocking conventions match the sibling data-tab-secondary-dimension test:
 *   - vi.mock("@/lib/api") so useReportSources (SWR) resolves to []
 *   - renderWithSWR wrapper for SWR cache isolation
 *   - fireEvent.change / fireEvent.click for interactions
 */
import { renderWithSWR, fireEvent, screen } from "../../../utils/render-with-swr";

import DataTab from "@/components/reports/config/DataTab";
import { apiFetch } from "@/lib/api";
import type { SankeyWidget, Widget } from "@/lib/reports/types";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  // useReportSources calls apiFetch("/sources") — return empty list so the
  // catalog never interferes with our assertions.
  vi.mocked(apiFetch).mockImplementation(() =>
    Promise.resolve([]) as Promise<unknown>,
  );
});

function makeSankey(overrides: Partial<SankeyWidget["config"]> = {}): SankeyWidget {
  return {
    id: "w_sankey",
    type: "sankey",
    title: "Cash flow",
    grid: { x: 0, y: 0, w: 8, h: 5 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      spending_granularity: "category",
      ...overrides,
    },
  };
}

describe("DataTab — Sankey knobs", () => {
  it("(a) changing spending_granularity to 'category_master' calls onUpdate with that value", () => {
    const updates: Widget[] = [];
    renderWithSWR(
      <DataTab widget={makeSankey()} onUpdate={(w) => updates.push(w)} />,
    );

    fireEvent.click(screen.getByLabelText("Master category"));

    expect(updates).toHaveLength(1);
    const next = updates[0] as SankeyWidget;
    expect(next.config.spending_granularity).toBe("category_master");
  });

  it("(a) changing spending_granularity back to 'category' calls onUpdate with 'category'", () => {
    const updates: Widget[] = [];
    renderWithSWR(
      <DataTab
        widget={makeSankey({ spending_granularity: "category_master" })}
        onUpdate={(w) => updates.push(w)}
      />,
    );

    fireEvent.click(screen.getByLabelText("Category"));

    expect(updates).toHaveLength(1);
    const next = updates[0] as SankeyWidget;
    expect(next.config.spending_granularity).toBe("category");
  });

  it("(b) entering a valid top_n value (5) writes config.top_n === 5", () => {
    const updates: Widget[] = [];
    renderWithSWR(
      <DataTab widget={makeSankey()} onUpdate={(w) => updates.push(w)} />,
    );

    fireEvent.change(screen.getByLabelText("Top N categories"), {
      target: { value: "5" },
    });

    expect(updates).toHaveLength(1);
    const next = updates[0] as SankeyWidget;
    expect(next.config.top_n).toBe(5);
  });

  it("(c) entering '-3' (< 2) clamps to undefined", () => {
    const updates: Widget[] = [];
    renderWithSWR(
      <DataTab widget={makeSankey()} onUpdate={(w) => updates.push(w)} />,
    );

    fireEvent.change(screen.getByLabelText("Top N categories"), {
      target: { value: "-3" },
    });

    expect(updates).toHaveLength(1);
    const next = updates[0] as SankeyWidget;
    expect(next.config.top_n).toBeUndefined();
  });

  it("(c) entering '1' (< 2) clamps to undefined", () => {
    const updates: Widget[] = [];
    renderWithSWR(
      <DataTab widget={makeSankey()} onUpdate={(w) => updates.push(w)} />,
    );

    fireEvent.change(screen.getByLabelText("Top N categories"), {
      target: { value: "1" },
    });

    expect(updates).toHaveLength(1);
    const next = updates[0] as SankeyWidget;
    expect(next.config.top_n).toBeUndefined();
  });

  it("(d) clearing the input (empty string) writes config.top_n === undefined", () => {
    const updates: Widget[] = [];
    renderWithSWR(
      <DataTab widget={makeSankey({ top_n: 10 })} onUpdate={(w) => updates.push(w)} />,
    );

    fireEvent.change(screen.getByLabelText("Top N categories"), {
      target: { value: "" },
    });

    expect(updates).toHaveLength(1);
    const next = updates[0] as SankeyWidget;
    expect(next.config.top_n).toBeUndefined();
  });

  it("(e) entering a value above 50 clamps to 50", () => {
    const updates: Widget[] = [];
    renderWithSWR(
      <DataTab widget={makeSankey()} onUpdate={(w) => updates.push(w)} />,
    );

    fireEvent.change(screen.getByLabelText("Top N categories"), {
      target: { value: "99" },
    });

    expect(updates).toHaveLength(1);
    const next = updates[0] as SankeyWidget;
    expect(next.config.top_n).toBe(50);
  });

  it("hides the data-source picker for sankey (fixed to transactions)", () => {
    renderWithSWR(<DataTab widget={makeSankey()} onUpdate={() => {}} />);
    expect(screen.queryByLabelText("Data source")).not.toBeInTheDocument();
  });

  it("hides the primary dimension picker for sankey (fixed schema)", () => {
    renderWithSWR(<DataTab widget={makeSankey()} onUpdate={() => {}} />);
    expect(screen.queryByLabelText("Primary dimension")).not.toBeInTheDocument();
  });
});
