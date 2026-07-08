/**
 * WidgetEditorPopover — the ``requestedTab`` deep-link (consume-and-clear
 * handshake). A filter chip sets ``requestedTab="filters"`` on the page;
 * the popover's honor-request effect selects that tab and calls
 * ``onTabConsumed`` so the page clears the request back to null. Two
 * separate effects keep this race-free:
 *  - reset effect keyed on [widget.id] ONLY → resets to Data on a new
 *    widget identity, never clobbers when requestedTab clears.
 *  - honor-request effect keyed on [requestedTab] ONLY → honors a chip
 *    click, including a SECOND click on the already-selected widget.
 */
import { fireEvent, render, screen } from "@testing-library/react";

import WidgetEditorPopover from "@/components/reports/WidgetEditorPopover";
import type { TabKey } from "@/components/reports/WidgetEditorPopover";
import { apiFetch } from "@/lib/api";
import type { BarWidget, KPIWidget } from "@/lib/reports/types";

vi.mock("@/lib/api", () => ({ apiFetch: vi.fn() }));
vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: () => ({ user: { id: 1 }, loading: false }),
}));

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

function bar(id = "w_bar"): BarWidget {
  return {
    id,
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

function kpi(id = "w_kpi"): KPIWidget {
  return {
    id,
    type: "kpi",
    title: "KPI",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: { dataset: "transactions", measure: { agg: "sum", field: "amount" } },
  };
}

function selected(tab: string): boolean {
  return (
    screen.getByRole("tab", { name: tab }).getAttribute("aria-selected") ===
    "true"
  );
}

describe("WidgetEditorPopover — requestedTab deep-link", () => {
  it("opens on the Filters tab when requestedTab='filters'", () => {
    render(
      <WidgetEditorPopover
        widget={bar()}
        canvasFilters={{}}
        anchorEl={anchorEl}
        requestedTab="filters"
        onTabConsumed={() => {}}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    expect(selected("Filters")).toBe(true);
  });

  it("defaults to Data when no requestedTab is passed", () => {
    render(
      <WidgetEditorPopover
        widget={bar()}
        canvasFilters={{}}
        anchorEl={anchorEl}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    expect(selected("Data")).toBe(true);
  });

  it("lands on Filters when a fresh widget id is passed with requestedTab", () => {
    const { rerender } = render(
      <WidgetEditorPopover
        widget={bar("w_a")}
        canvasFilters={{}}
        anchorEl={anchorEl}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    expect(selected("Data")).toBe(true);
    rerender(
      <WidgetEditorPopover
        widget={bar("w_b")}
        canvasFilters={{}}
        anchorEl={anchorEl}
        requestedTab="filters"
        onTabConsumed={() => {}}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    expect(selected("Filters")).toBe(true);
  });

  it("re-opens Filters on a SECOND chip click on the same widget", () => {
    // Model the page's consume-and-clear: onTabConsumed clears the prop.
    let requestedTab: TabKey | null = "filters";
    const onTabConsumed = vi.fn(() => {
      requestedTab = null;
    });
    const renderPopover = () => (
      <WidgetEditorPopover
        widget={bar("w_same")}
        canvasFilters={{}}
        anchorEl={anchorEl}
        requestedTab={requestedTab ?? undefined}
        onTabConsumed={onTabConsumed}
        onUpdate={() => {}}
        onClose={() => {}}
      />
    );
    const { rerender } = render(renderPopover());
    // First chip click consumed → Filters, then prop cleared to null.
    expect(selected("Filters")).toBe(true);
    rerender(renderPopover());
    expect(onTabConsumed).toHaveBeenCalled();

    // User manually goes to Data.
    fireEvent.click(screen.getByRole("tab", { name: "Data" }));
    expect(selected("Data")).toBe(true);

    // Second chip click on the SAME widget: page sets requestedTab again.
    requestedTab = "filters";
    rerender(renderPopover());
    expect(selected("Filters")).toBe(true);
  });

  it("does NOT clobber the tab back to Data when requestedTab clears (filters → null)", () => {
    const { rerender } = render(
      <WidgetEditorPopover
        widget={bar("w_stable")}
        canvasFilters={{}}
        anchorEl={anchorEl}
        requestedTab="filters"
        onTabConsumed={() => {}}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    expect(selected("Filters")).toBe(true);
    // The consume: requestedTab goes filters → null with a STABLE widget id.
    rerender(
      <WidgetEditorPopover
        widget={bar("w_stable")}
        canvasFilters={{}}
        anchorEl={anchorEl}
        requestedTab={undefined}
        onTabConsumed={() => {}}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    // Must STILL be on Filters — the reset effect is keyed on widget.id
    // only, so a requestedTab clear never re-fires it.
    expect(selected("Filters")).toBe(true);
  });

  it("resets a fresh widget to Data even after a prior Filters request", () => {
    const { rerender } = render(
      <WidgetEditorPopover
        widget={bar("w_one")}
        canvasFilters={{}}
        anchorEl={anchorEl}
        requestedTab="filters"
        onTabConsumed={() => {}}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    expect(selected("Filters")).toBe(true);
    // Plain widget switch (no requestedTab) → Data default preserved.
    rerender(
      <WidgetEditorPopover
        widget={kpi("w_two")}
        canvasFilters={{}}
        anchorEl={anchorEl}
        onUpdate={() => {}}
        onClose={() => {}}
      />,
    );
    expect(selected("Data")).toBe(true);
  });
});
