/**
 * CustomDashboard — Reset-to-default integration test.
 *
 * Verifies that in Customize mode the user can click "Reset to default",
 * confirm in the ConfirmModal, and see the 7-tile default layout replace
 * the current (customized) layout — with Save enabled (dirty) but NOT
 * yet persisted.
 */
import React from "react";
import { renderWithSWR } from "@/tests/utils/render-with-swr";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";

// ── mocks (must precede component imports) ────────────────────────────────────

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/dashboard/DashboardPeriodNav", () => ({
  default: () => <div data-testid="period-nav-stub" />,
}));

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

vi.mock("@/components/reports/widgets/BarWidget", () => ({
  default: ({ widget }: { widget: { id: string; type: string } }) => (
    <div data-testid={`report-widget-${widget.id}`}>Bar widget stub</div>
  ),
}));

vi.mock("@/lib/reports/api", () => ({
  listReports: vi.fn(),
}));

vi.mock("@/lib/dashboard/api", () => ({
  getDashboard: vi.fn(),
  saveDashboard: vi.fn(),
  getDefaultDashboard: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/lib/pagination", async () => {
  const actual = await vi.importActual<typeof import("@/lib/pagination")>("@/lib/pagination");
  return { ...actual, fetchAll: vi.fn().mockResolvedValue([]) };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return {
    ...actual,
    useAuth: vi.fn(() => ({ user: { billing_cycle_day: 1 }, loading: false })),
  };
});

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

vi.mock("@/lib/hooks/use-is-mobile", () => ({
  useIsMobile: () => false,
}));

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

/** A single-widget customized layout that differs from the 7-tile default. */
const CUSTOM_LAYOUT = {
  version: 1,
  widgets: [
    {
      id: "custom-only",
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
  layout_json: CUSTOM_LAYOUT,
  canvas_filters_json: {},
};

/**
 * The 7-tile seed that getDefaultDashboard resolves to. Grid heights match
 * the canonical backend DEFAULT_DASHBOARD_LAYOUT (routers/dashboard.py): each
 * tile is sized to fully show its default content without the card's
 * overflow-hidden clipping it.
 */
const DEFAULT_SEED = {
  layout_json: {
    version: 1,
    widgets: [
      { id: "default-on-track",           type: "dash_on_track",            title: "On Track",               grid: { x: 0, y: 0,  w: 12, h: 4 },  config: {} },
      { id: "default-accounts",           type: "dash_accounts",            title: "Accounts",               grid: { x: 0, y: 4,  w: 4,  h: 9 },  config: {} },
      { id: "default-account-forecast",   type: "dash_account_forecast",    title: "Month-End Forecast",     grid: { x: 4, y: 4,  w: 8,  h: 9 },  config: {} },
      { id: "default-spending",           type: "dash_spending",            title: "Spending by Category",   grid: { x: 0, y: 13, w: 4,  h: 6 },  config: {} },
      { id: "default-budget",             type: "dash_budget",              title: "Budget Progress",        grid: { x: 4, y: 13, w: 4,  h: 6 },  config: {} },
      { id: "default-forecast-category",  type: "dash_forecast_category",   title: "Forecast by Category",  grid: { x: 8, y: 13, w: 4,  h: 6 },  config: {} },
      { id: "default-recent-transactions", type: "dash_recent_transactions", title: "Recent Transactions",   grid: { x: 0, y: 19, w: 12, h: 11 }, config: {} },
    ],
  },
  canvas_filters_json: {},
};

// ── helpers ───────────────────────────────────────────────────────────────────

function makeApiFetchHandler() {
  return async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [];
    if (url.startsWith("/api/v1/settings/billing-periods")) return [{ id: 1, start_date: "2026-05-01", end_date: null }];
    if (url.startsWith("/api/v1/settings/billing-period")) return { id: 1, start_date: "2026-05-01", end_date: null };
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
  vi.clearAllMocks();
  vi.mocked(dashboardApi.getDashboard).mockResolvedValue(DASHBOARD_RESPONSE as never);
  vi.mocked(dashboardApi.saveDashboard).mockResolvedValue(DASHBOARD_RESPONSE as never);
  vi.mocked(dashboardApi.getDefaultDashboard).mockResolvedValue(DEFAULT_SEED as never);
  vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);
});

describe("CustomDashboard — Reset to default", () => {
  it("resets to the 7-tile seed on confirm and marks canvas dirty without saving", async () => {
    renderWithSWR(<CustomDashboard />);

    // Wait for the initial load to complete.
    await waitFor(() =>
      expect(screen.queryByTestId("custom-dashboard-loading")).not.toBeInTheDocument(),
    );

    // The canvas starts with the single customized widget.
    expect(screen.getByTestId("widget-dash_on_track")).toBeInTheDocument();

    // Enter Customize mode.
    const customizeBtn = await screen.findByRole("button", { name: /customize/i });
    act(() => { fireEvent.click(customizeBtn); });

    // The "Reset to default" button must be visible in Customize mode.
    const resetBtn = screen.getByRole("button", { name: /reset to default/i });
    expect(resetBtn).toBeInTheDocument();

    // Click Reset — the confirm modal should appear.
    act(() => { fireEvent.click(resetBtn); });

    // The confirm modal must be visible with a meaningful title.
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    // Confirm the reset — click the "Reset" button inside the dialog.
    const dialog = screen.getByRole("dialog");
    const confirmBtn = dialog.querySelector("button:last-child") as HTMLButtonElement;
    expect(confirmBtn).toBeTruthy();
    await act(async () => { fireEvent.click(confirmBtn); });

    // getDefaultDashboard must have been called exactly once.
    expect(dashboardApi.getDefaultDashboard).toHaveBeenCalledTimes(1);

    // The canvas must now render all 7 default widget types.
    await waitFor(() => {
      expect(screen.getByTestId("widget-dash_on_track")).toBeInTheDocument();
      expect(screen.getByTestId("widget-dash_accounts")).toBeInTheDocument();
      expect(screen.getByTestId("widget-dash_account_forecast")).toBeInTheDocument();
      expect(screen.getByTestId("widget-dash_spending")).toBeInTheDocument();
      expect(screen.getByTestId("widget-dash_budget")).toBeInTheDocument();
      expect(screen.getByTestId("widget-dash_forecast_category")).toBeInTheDocument();
      expect(screen.getByTestId("widget-dash_recent_transactions")).toBeInTheDocument();
    });

    // Save must be enabled (dirty) — the user hasn't saved yet.
    expect(screen.getByRole("button", { name: /^save$/i })).toBeEnabled();

    // saveDashboard must NOT have been called (Reset does not auto-save).
    expect(dashboardApi.saveDashboard).not.toHaveBeenCalled();
  });

  it("does NOT show Reset to default when NOT in Customize mode", async () => {
    renderWithSWR(<CustomDashboard />);

    await waitFor(() =>
      expect(screen.queryByTestId("custom-dashboard-loading")).not.toBeInTheDocument(),
    );

    // Customize mode is off — Reset button must be absent.
    expect(screen.queryByRole("button", { name: /reset to default/i })).not.toBeInTheDocument();
  });

  it("cancels the reset when the modal Cancel button is clicked", async () => {
    renderWithSWR(<CustomDashboard />);

    await waitFor(() =>
      expect(screen.queryByTestId("custom-dashboard-loading")).not.toBeInTheDocument(),
    );

    // Enter Customize mode and click Reset.
    act(() => { fireEvent.click(screen.getByRole("button", { name: /customize/i })); });
    act(() => { fireEvent.click(screen.getByRole("button", { name: /reset to default/i })); });

    // Confirm modal is open.
    expect(await screen.findByRole("dialog")).toBeInTheDocument();

    // Click Cancel.
    act(() => { fireEvent.click(screen.getByRole("button", { name: /cancel/i })); });

    // Modal closes and getDefaultDashboard was never called.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(dashboardApi.getDefaultDashboard).not.toHaveBeenCalled();

    // Canvas still shows the original customized widget (no change).
    expect(screen.getByTestId("widget-dash_on_track")).toBeInTheDocument();
    // Only the one widget from CUSTOM_LAYOUT — not 7.
    expect(screen.queryByTestId("widget-dash_accounts")).not.toBeInTheDocument();
  });
});
