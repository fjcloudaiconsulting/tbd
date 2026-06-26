/**
 * CustomDashboard — Add-widget picker integration test.
 *
 * Verifies that in Customize mode the user can open the Add-widget menu,
 * click a "Dashboard tiles" entry (Accounts), and see the corresponding
 * tile appended to the canvas with the Save button enabled (dirty).
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

// ── mocks (must precede component imports) ────────────────────────────────────

/**
 * Stub AppShell to a plain wrapper so we don't need to mock the entire
 * sidebar/auth chrome. Same pattern used by reports-page.test.tsx.
 */
vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

/**
 * Stub DashboardPeriodNav — it performs its own SWR fetches that would
 * race with the assertions we care about.
 */
vi.mock("@/components/dashboard/DashboardPeriodNav", () => ({
  default: () => <div data-testid="period-nav-stub" />,
}));

/**
 * Stub every dashboard widget to a labelled div so the renderer doesn't
 * need the full DashboardDataProvider data shape.
 */
vi.mock("@/components/dashboard/widgets/OnTrackWidget", () => ({
  default: () => <div>OnTrack</div>,
}));
vi.mock("@/components/dashboard/widgets/AccountsWidget", () => ({
  default: () => <div>Accounts tile</div>,
}));
vi.mock("@/components/dashboard/widgets/AccountForecastWidget", () => ({
  default: () => <div>AccountForecast tile</div>,
}));
vi.mock("@/components/dashboard/widgets/SpendingDonutWidget", () => ({
  default: () => <div>Spending tile</div>,
}));
vi.mock("@/components/dashboard/widgets/BudgetBarsWidget", () => ({
  default: () => <div>Budget tile</div>,
}));
vi.mock("@/components/dashboard/widgets/ForecastBarsWidget", () => ({
  default: () => <div>ForecastBars tile</div>,
}));
vi.mock("@/components/dashboard/widgets/RecentTransactionsWidget", () => ({
  default: () => <div>RecentTransactions tile</div>,
}));

/**
 * Stub report widgets so the fall-through renderer doesn't need network calls.
 * Only bar is needed for the clone-from-report test.
 */
vi.mock("@/components/reports/widgets/BarWidget", () => ({
  default: ({ widget }: { widget: { id: string; type: string } }) => (
    <div data-testid={`report-widget-${widget.id}`}>Bar widget stub</div>
  ),
}));

/**
 * Stub listReports so the "From a report" menu can be driven in tests.
 */
vi.mock("@/lib/reports/api", () => ({
  listReports: vi.fn(),
}));

/**
 * Mock the dashboard API module so we control what getDashboard returns
 * (layout without dash_accounts) and saveDashboard resolves cleanly.
 */
vi.mock("@/lib/dashboard/api", () => ({
  getDashboard: vi.fn(),
  saveDashboard: vi.fn(),
}));

/**
 * Mock apiFetch so SWR calls inside DashboardDataProvider + useFilterChipState
 * resolve to safe empty values without hitting the network.
 */
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

/**
 * Mock fetchAll (used by DashboardDataProvider for pending transactions).
 */
vi.mock("@/lib/pagination", async () => {
  const actual = await vi.importActual<typeof import("@/lib/pagination")>("@/lib/pagination");
  return { ...actual, fetchAll: vi.fn().mockResolvedValue([]) };
});

/**
 * Mock useAuth — DashboardDataProvider calls it to seed billingCycleDay.
 */
vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return {
    ...actual,
    useAuth: vi.fn(() => ({ user: { billing_cycle_day: 1 }, loading: false })),
  };
});

/**
 * Mock usePersistedSort — avoids localStorage side-effects.
 */
vi.mock("@/lib/hooks/use-persisted-sort", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/hooks/use-persisted-sort")
  >("@/lib/hooks/use-persisted-sort");
  return {
    ...actual,
    usePersistedSort: vi.fn(() => ({
      field: "date",
      dir: "desc",
      setSort: vi.fn(),
      reset: vi.fn(),
      isDefault: true,
    })),
  };
});

/**
 * Stub useIsMobile to always return false (desktop) so Customize mode and
 * Canvas are rendered instead of the mobile read-only stack.
 */
vi.mock("@/lib/hooks/use-is-mobile", () => ({
  useIsMobile: () => false,
}));

/**
 * Canvas renders react-grid-layout which needs DOM measurement.
 * Stub it so widgets render by calling renderWidget for each item in
 * the layout — we can then assert on their test-ids.
 */
vi.mock("@/components/reports/Canvas", () => ({
  default: ({
    renderWidget,
    layout,
  }: {
    renderWidget: (w: { id: string; type: string }) => React.ReactNode;
    layout: { widgets: Array<{ id: string; type: string }> };
  }) => (
    <div data-testid="canvas-stub">
      {layout.widgets.map((w) => (
        <div key={w.id}>{renderWidget(w)}</div>
      ))}
    </div>
  ),
}));

// ── imports (after mocks) ─────────────────────────────────────────────────────

import CustomDashboard from "@/components/dashboard/CustomDashboard";
import * as dashboardApi from "@/lib/dashboard/api";
import * as reportsApi from "@/lib/reports/api";
import { apiFetch } from "@/lib/api";

// ── fixtures ──────────────────────────────────────────────────────────────────

/** A layout that intentionally omits dash_accounts so we can add it back. */
const LAYOUT_WITHOUT_ACCOUNTS = {
  version: 1,
  widgets: [
    {
      id: "w_existing",
      type: "dash_on_track",
      title: "On Track",
      grid: { x: 0, y: 0, w: 12, h: 3 },
      config: {},
    },
  ],
};

const DASHBOARD_RESPONSE = {
  id: 1,
  org_id: 1,
  layout_json: LAYOUT_WITHOUT_ACCOUNTS,
  canvas_filters_json: {},
};

// ── helpers ───────────────────────────────────────────────────────────────────

const SAFE_PERIODS = [{ id: 1, start_date: "2026-05-01", end_date: null }];

function makeApiFetchHandler() {
  return async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [];
    if (url.startsWith("/api/v1/settings/billing-periods")) return SAFE_PERIODS;
    if (url.startsWith("/api/v1/settings/billing-period")) return SAFE_PERIODS[0];
    if (url.startsWith("/api/v1/settings/billing-cycle")) return { billing_cycle_day: 1 };
    if (url.startsWith("/api/v1/forecast-plans/current")) return null;
    if (url.startsWith("/api/v1/forecast/account-balances")) return null;
    if (url.startsWith("/api/v1/forecast")) return null;
    if (url.startsWith("/api/v1/transactions")) return { items: [], total: 0 };
    if (url.startsWith("/api/v1/budgets")) return [];
    if (url.startsWith("/api/v1/categories")) return [];
    return null;
  };
}

// ── tests ─────────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.mocked(dashboardApi.getDashboard).mockResolvedValue(
    DASHBOARD_RESPONSE as never,
  );
  vi.mocked(dashboardApi.saveDashboard).mockResolvedValue(
    DASHBOARD_RESPONSE as never,
  );
  vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);
});

describe("CustomDashboard — Add-widget picker", () => {
  it("re-adds a removed dash tile from the Add-widget menu", async () => {
    render(<CustomDashboard />);

    // Wait for the initial load to complete (loading spinner disappears).
    await waitFor(() =>
      expect(
        screen.queryByTestId("custom-dashboard-loading"),
      ).not.toBeInTheDocument(),
    );

    // Enter Customize mode.
    const customizeBtn = await screen.findByRole("button", { name: /customize/i });
    act(() => { fireEvent.click(customizeBtn); });

    // Open the Add-widget picker.
    const addWidgetBtn = screen.getByRole("button", { name: /add widget/i });
    act(() => { fireEvent.click(addWidgetBtn); });

    // Click the "Accounts" tile entry in the Dashboard tiles group.
    // The button's accessible name includes the description text so we match
    // via data-testid (set on each option) rather than an exact-name regex.
    const accountsBtn = screen.getByTestId("add-widget-menu-option-dash_accounts");
    act(() => { fireEvent.click(accountsBtn); });

    // The dash_accounts widget should now appear on the canvas.
    const tileWrapper = await screen.findByTestId("widget-dash_accounts");
    expect(tileWrapper).toBeInTheDocument();

    // Regression guard: the canvas widget wrapper MUST carry `h-full`. It sits
    // between the react-grid-layout grid item (a fixed-height box) and
    // WidgetShell (which fills via `h-full`). Dropping `h-full` here breaks the
    // height chain — tiles collapse to content height inside a taller box, so
    // the resize handle floats off the card and tall tiles overflow neighbours.
    expect(tileWrapper).toHaveClass("h-full");

    // The Save button should be enabled (dirty = true).
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });

  it("does NOT show the Add-widget button when NOT in Customize mode", async () => {
    render(<CustomDashboard />);

    await waitFor(() =>
      expect(
        screen.queryByTestId("custom-dashboard-loading"),
      ).not.toBeInTheDocument(),
    );

    // Customize mode is off by default — the Add-widget button must be absent.
    expect(
      screen.queryByTestId("custom-dashboard-add-widget"),
    ).not.toBeInTheDocument();
  });

  it("closes the Add-widget menu when Escape is pressed", async () => {
    render(<CustomDashboard />);

    await waitFor(() =>
      expect(
        screen.queryByTestId("custom-dashboard-loading"),
      ).not.toBeInTheDocument(),
    );

    // Enter Customize mode and open the picker.
    const customizeBtn = await screen.findByRole("button", { name: /customize/i });
    act(() => { fireEvent.click(customizeBtn); });
    act(() => { fireEvent.click(screen.getByTestId("custom-dashboard-add-widget")); });

    // Menu is open.
    expect(screen.getByTestId("add-widget-menu")).toBeInTheDocument();

    // Press Escape — menu should close.
    act(() => { fireEvent.keyDown(document, { key: "Escape" }); });

    expect(screen.queryByTestId("add-widget-menu")).not.toBeInTheDocument();
  });

  it("closes the Add-widget menu when the backdrop is clicked", async () => {
    render(<CustomDashboard />);

    await waitFor(() =>
      expect(
        screen.queryByTestId("custom-dashboard-loading"),
      ).not.toBeInTheDocument(),
    );

    // Enter Customize mode and open the picker.
    const customizeBtn = await screen.findByRole("button", { name: /customize/i });
    act(() => { fireEvent.click(customizeBtn); });
    act(() => { fireEvent.click(screen.getByTestId("custom-dashboard-add-widget")); });

    // Menu is open.
    const menu = screen.getByTestId("add-widget-menu");
    expect(menu).toBeInTheDocument();

    // Click the backdrop (the outer overlay element itself).
    act(() => { fireEvent.click(menu); });

    expect(screen.queryByTestId("add-widget-menu")).not.toBeInTheDocument();
  });

  it("does NOT close the Add-widget menu when clicking inside the panel", async () => {
    render(<CustomDashboard />);

    await waitFor(() =>
      expect(
        screen.queryByTestId("custom-dashboard-loading"),
      ).not.toBeInTheDocument(),
    );

    // Enter Customize mode and open the picker.
    const customizeBtn = await screen.findByRole("button", { name: /customize/i });
    act(() => { fireEvent.click(customizeBtn); });
    act(() => { fireEvent.click(screen.getByTestId("custom-dashboard-add-widget")); });

    // Click a tile button inside the panel — should NOT close the menu
    // (the menu only closes after a tile is picked via onAddDashTile → setPickerOpen(false)).
    const dashGroup = screen.getByTestId("add-widget-menu-group-dashboard");
    act(() => { fireEvent.click(dashGroup); });

    // Menu still open because the inner click was stopped before reaching backdrop.
    expect(screen.getByTestId("add-widget-menu")).toBeInTheDocument();
  });
});

describe("CustomDashboard — clone widget from a report", () => {
  const SOURCE_BAR_WIDGET = {
    id: "w_src_bar",
    type: "bar",
    title: "Monthly Spending",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config: {},
  };

  const REPORT_WITH_BAR = {
    id: 42,
    owner_user_id: 1,
    org_id: 1,
    visibility: "private",
    name: "My Spending Report",
    description: null,
    layout_json: { version: 1, widgets: [SOURCE_BAR_WIDGET] },
    canvas_filters_json: {},
    schema_version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };

  beforeEach(() => {
    vi.mocked(reportsApi.listReports).mockResolvedValue([REPORT_WITH_BAR] as never);
    // saveDashboard echoes the layout it receives so we can inspect what was saved.
    vi.mocked(dashboardApi.saveDashboard).mockImplementation(
      async (layout) => ({ ...DASHBOARD_RESPONSE, layout_json: layout }) as never,
    );
  });

  it("clones a bar widget from a report onto the canvas, then saves with the cloned widget", async () => {
    render(<CustomDashboard />);

    // Wait for initial load.
    await waitFor(() =>
      expect(screen.queryByTestId("custom-dashboard-loading")).not.toBeInTheDocument(),
    );

    // Enter Customize mode.
    const customizeBtn = await screen.findByRole("button", { name: /customize/i });
    act(() => { fireEvent.click(customizeBtn); });

    // Open the Add-widget picker.
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /add widget/i }));
    });

    // Navigate into "From a report".
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /from a report/i }));
    });

    // Wait for the report list and pick our report.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /my spending report/i }),
      ).toBeInTheDocument(),
    );
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: /my spending report/i }));
    });

    // Wait for the widget list and pick the bar widget.
    await waitFor(() =>
      expect(
        screen.getByTestId("add-widget-menu-report-widget-w_src_bar"),
      ).toBeInTheDocument(),
    );
    act(() => {
      fireEvent.click(screen.getByTestId("add-widget-menu-report-widget-w_src_bar"));
    });

    // The menu should close and the cloned widget should appear on the canvas.
    // The Canvas stub renders each widget via renderWidget which wraps it in
    // data-testid="widget-<type>".
    await waitFor(() =>
      expect(screen.getByTestId("widget-bar")).toBeInTheDocument(),
    );

    // Save button should be enabled (dirty).
    const saveBtn = screen.getByRole("button", { name: /^save$/i });
    expect(saveBtn).toBeEnabled();

    // Click Save and assert saveDashboard was called with the cloned widget.
    act(() => { fireEvent.click(saveBtn); });

    await waitFor(() =>
      expect(dashboardApi.saveDashboard).toHaveBeenCalledTimes(1),
    );

    const [savedLayout] = (dashboardApi.saveDashboard as ReturnType<typeof vi.fn>).mock.calls[0];
    const savedWidgets: Array<{ type: string; id: string }> = savedLayout.widgets;
    // The cloned widget must be in the saved layout.
    const clonedBar = savedWidgets.find((w) => w.type === "bar");
    expect(clonedBar).toBeDefined();
    // It must have a fresh id (not the source widget's id).
    expect(clonedBar?.id).not.toBe("w_src_bar");
  });
});
