/**
 * AddWidgetMenu — "From a report" sub-flow tests.
 *
 * Verifies that when the user navigates the "From a report" path in the
 * AddWidgetMenu they can pick a saved report, see its widgets listed, and
 * clone the chosen widget via onAddCloned (with a fresh id).
 *
 * Sankey widgets must NOT be filtered out — the dashboard backend validator
 * (Task 1) now accepts "sankey".
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

// ── mock listReports BEFORE component import ──────────────────────────────────
vi.mock("@/lib/reports/api", () => ({
  listReports: vi.fn(),
}));

import AddWidgetMenu from "@/components/dashboard/AddWidgetMenu";
import * as reportsApi from "@/lib/reports/api";
import type { ReportSummary } from "@/lib/reports/types";

// ── fixtures ──────────────────────────────────────────────────────────────────

const BAR_WIDGET = {
  id: "w_src_bar",
  type: "bar" as const,
  title: "Monthly Spending",
  grid: { x: 0, y: 0, w: 6, h: 4 },
  config: {},
};

const SANKEY_WIDGET = {
  id: "w_src_sankey",
  type: "sankey" as const,
  title: "Cash Flow",
  grid: { x: 0, y: 4, w: 12, h: 6 },
  config: {},
};

const REPORTS: ReportSummary[] = [
  {
    id: 1,
    owner_user_id: 1,
    org_id: 1,
    visibility: "private",
    name: "Spending Report",
    description: null,
    layout_json: { version: 1, widgets: [BAR_WIDGET as never] },
    canvas_filters_json: {},
    schema_version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: 2,
    owner_user_id: 1,
    org_id: 1,
    visibility: "private",
    name: "Cash Flow Report",
    description: null,
    layout_json: { version: 1, widgets: [SANKEY_WIDGET as never] },
    canvas_filters_json: {},
    schema_version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

beforeEach(() => {
  vi.mocked(reportsApi.listReports).mockResolvedValue(REPORTS);
});

// ── tests ─────────────────────────────────────────────────────────────────────

describe("AddWidgetMenu — From a report", () => {
  it("lists a report's widgets and clones the chosen one (incl. sankey)", async () => {
    const onAddCloned = vi.fn();
    render(
      <AddWidgetMenu
        open
        existing={[]}
        onClose={() => {}}
        onAddDashTile={() => {}}
        onAddCloned={onAddCloned}
      />,
    );

    // Click "From a report" to navigate into the reports sub-view.
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /from a report/i }));
    });

    // The reports list should appear — pick "Cash Flow Report".
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /cash flow report/i }),
      ).toBeInTheDocument(),
    );
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /cash flow report/i }));
    });

    // The widget list for that report appears — pick the "Cash Flow" sankey widget.
    await waitFor(() =>
      expect(
        screen.getByTestId("add-widget-menu-report-widget-w_src_sankey"),
      ).toBeInTheDocument(),
    );
    act(() => {
      fireEvent.click(screen.getByTestId("add-widget-menu-report-widget-w_src_sankey"));
    });

    // onAddCloned must have been called exactly once.
    expect(onAddCloned).toHaveBeenCalledTimes(1);

    const arg = onAddCloned.mock.calls[0][0];
    expect(arg.type).toBe("sankey");
    // Clone must have a fresh id — not the source widget's id.
    expect(arg.id).not.toBe("w_src_sankey");
  });

  it("shows 'You have no saved reports yet.' when listReports returns empty", async () => {
    vi.mocked(reportsApi.listReports).mockResolvedValue([]);

    render(
      <AddWidgetMenu
        open
        existing={[]}
        onClose={() => {}}
        onAddDashTile={() => {}}
        onAddCloned={() => {}}
      />,
    );

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /from a report/i }));
    });

    await waitFor(() =>
      expect(
        screen.getByText(/you have no saved reports yet/i),
      ).toBeInTheDocument(),
    );
  });

  it("shows 'This report has no widgets.' when report layout_json is empty", async () => {
    vi.mocked(reportsApi.listReports).mockResolvedValue([
      {
        ...REPORTS[0],
        layout_json: {},
      } as ReportSummary,
    ]);

    render(
      <AddWidgetMenu
        open
        existing={[]}
        onClose={() => {}}
        onAddDashTile={() => {}}
        onAddCloned={() => {}}
      />,
    );

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /from a report/i }));
    });

    await waitFor(() =>
      expect(
        screen.getByTestId("add-widget-menu-report-spending-report"),
      ).toBeInTheDocument(),
    );
    act(() => {
      fireEvent.click(screen.getByTestId("add-widget-menu-report-spending-report"));
    });

    await waitFor(() =>
      expect(
        screen.getByText(/this report has no widgets/i),
      ).toBeInTheDocument(),
    );
  });

  it("navigates back to root from the reports sub-view", async () => {
    render(
      <AddWidgetMenu
        open
        existing={[]}
        onClose={() => {}}
        onAddDashTile={() => {}}
        onAddCloned={() => {}}
      />,
    );

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /from a report/i }));
    });

    // A "Back" button should be visible in the sub-view.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /back/i })).toBeInTheDocument(),
    );
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /back/i }));
    });

    // After going back the "Dashboard tiles" group must be visible again.
    expect(screen.getByTestId("add-widget-menu-group-dashboard")).toBeInTheDocument();
  });

  it("shows error message (not empty state) when listReports rejects", async () => {
    vi.mocked(reportsApi.listReports).mockRejectedValue(new Error("Network error"));

    render(
      <AddWidgetMenu
        open
        existing={[]}
        onClose={() => {}}
        onAddDashTile={() => {}}
        onAddCloned={() => {}}
      />,
    );

    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /from a report/i }));
    });

    // Error state must appear.
    await waitFor(() =>
      expect(
        screen.getByTestId("add-widget-menu-reports-error"),
      ).toBeInTheDocument(),
    );

    // Must show the error message.
    expect(screen.getByText(/could not load reports/i)).toBeInTheDocument();

    // Must NOT show the "no saved reports" empty state.
    expect(
      screen.queryByText(/you have no saved reports yet/i),
    ).not.toBeInTheDocument();

    // A Retry button must be present.
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
