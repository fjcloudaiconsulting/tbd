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
    expect(
      await screen.findByTestId("widget-dash_accounts"),
    ).toBeInTheDocument();

    // The Save button should be enabled (dirty = true).
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();
  });
});
