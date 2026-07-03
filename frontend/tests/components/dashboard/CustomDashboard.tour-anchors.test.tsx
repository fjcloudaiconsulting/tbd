/**
 * CustomDashboard — first-run tour anchors (Phase 2b).
 *
 * The customizable dashboard's finance tiles are user-arrangeable, so the
 * tour can't rely on fixed positions. Instead each default finance tile
 * carries a stable ``data-tour-id`` injected via ``<TourAnchor as="child">``
 * on the tile wrapper itself (no extra span). This test renders the 7-tile
 * default layout and asserts every DASHBOARD_TOUR_STEPS anchor is present on
 * a real element, and that the Customize button carries its anchor too.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";

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
import { DASHBOARD_TOUR_STEPS } from "@/lib/help/tour";

// ── fixtures ──────────────────────────────────────────────────────────────────

/** The 7-tile default seed a first-run user starts with. */
const DEFAULT_LAYOUT = {
  version: 1,
  widgets: [
    { id: "default-on-track",            type: "dash_on_track",            title: "On Track",             grid: { x: 0, y: 0,  w: 12, h: 4 },  config: {} },
    { id: "default-accounts",            type: "dash_accounts",            title: "Accounts",             grid: { x: 0, y: 4,  w: 4,  h: 9 },  config: {} },
    { id: "default-account-forecast",    type: "dash_account_forecast",    title: "Month-End Forecast",   grid: { x: 4, y: 4,  w: 8,  h: 9 },  config: {} },
    { id: "default-spending",            type: "dash_spending",            title: "Spending by Category", grid: { x: 0, y: 13, w: 4,  h: 6 },  config: {} },
    { id: "default-budget",              type: "dash_budget",              title: "Budget Progress",      grid: { x: 4, y: 13, w: 4,  h: 6 },  config: {} },
    { id: "default-forecast-category",   type: "dash_forecast_category",   title: "Forecast by Category", grid: { x: 8, y: 13, w: 4,  h: 6 },  config: {} },
    { id: "default-recent-transactions", type: "dash_recent_transactions", title: "Recent Transactions",  grid: { x: 0, y: 19, w: 12, h: 11 }, config: {} },
  ],
};

const DASHBOARD_RESPONSE = {
  id: 1,
  org_id: 1,
  layout_json: DEFAULT_LAYOUT,
  canvas_filters_json: {},
};

function makeApiFetchHandler() {
  return async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [];
    if (url.startsWith("/api/v1/settings/billing-periods")) return [{ id: 1, start_date: "2026-05-01", end_date: null }];
    if (url.startsWith("/api/v1/settings/billing-period")) return { id: 1, start_date: "2026-05-01", end_date: null };
    if (url.startsWith("/api/v1/settings/billing-cycle")) return { billing_cycle_day: 1 };
    if (url.startsWith("/api/v1/forecast-plans/current")) return null;
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
  vi.mocked(apiFetch).mockImplementation(makeApiFetchHandler() as never);
});

/** Every DASHBOARD_TOUR_STEPS anchor that CustomDashboard is responsible for. */
const CUSTOM_DASHBOARD_ANCHORS = [
  "dashboard.header",
  "dashboard.on-track-tile",
  "dashboard.accounts-tile",
  "dashboard.account-forecast",
  "dashboard.recent-transactions",
  "dashboard.customize",
];

describe("CustomDashboard — first-run tour anchors", () => {
  it("renders every finance-tile and customize tour anchor on the default layout", async () => {
    const { container } = render(<CustomDashboard />);

    await waitFor(() =>
      expect(screen.queryByTestId("custom-dashboard-loading")).not.toBeInTheDocument(),
    );

    for (const id of CUSTOM_DASHBOARD_ANCHORS) {
      const el = container.querySelector(`[data-tour-id="${id}"]`);
      expect(el, `missing data-tour-id="${id}"`).not.toBeNull();
      // Anchor must sit on a real layout element, never a TourAnchor wrapper span.
      expect(
        (el as HTMLElement).getAttribute("data-testid"),
        `data-tour-id="${id}" should not be on a TourAnchor wrapper span`,
      ).not.toBe("tour-anchor");
    }
  });

  it("covers all DASHBOARD_TOUR_STEPS between CustomDashboard and the period nav", () => {
    // The period-nav anchor lives in DashboardPeriodNav (stubbed here), so it
    // is the only step not asserted above. Guard that the split stays complete.
    const covered = new Set([...CUSTOM_DASHBOARD_ANCHORS, "dashboard.period-nav"]);
    for (const step of DASHBOARD_TOUR_STEPS) {
      expect(covered.has(step), `tour step "${step}" is not anchored anywhere`).toBe(true);
    }
  });

  it("auto-degrades: a tile the user removed leaves no orphan anchor", async () => {
    // A layout missing the accounts tile must not render its anchor; the
    // overlay auto-skips absent anchors at runtime.
    vi.mocked(dashboardApi.getDashboard).mockResolvedValue({
      ...DASHBOARD_RESPONSE,
      layout_json: {
        version: 1,
        widgets: DEFAULT_LAYOUT.widgets.filter((w) => w.type !== "dash_accounts"),
      },
    } as never);

    const { container } = render(<CustomDashboard />);
    await waitFor(() =>
      expect(screen.queryByTestId("custom-dashboard-loading")).not.toBeInTheDocument(),
    );

    expect(container.querySelector('[data-tour-id="dashboard.accounts-tile"]')).toBeNull();
    // The other tiles' anchors are still present.
    expect(container.querySelector('[data-tour-id="dashboard.on-track-tile"]')).not.toBeNull();
  });
});
