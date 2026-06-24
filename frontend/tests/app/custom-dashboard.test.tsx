/**
 * custom-dashboard.test.tsx
 *
 * Tests the flag-switching behaviour of DashboardPage:
 *
 *   (a) Flag OFF (default): the existing legacy dashboard renders.
 *       We assert a known testid / text that the legacy dashboard
 *       produces — ``data-testid="reset-banner"`` exists in the legacy
 *       code, but a simpler sentinel is the "Dashboard" heading that
 *       LegacyDashboard renders inside AppShell when not loading.
 *       We also assert ``data-testid="custom-dashboard"`` is absent so
 *       the test is unambiguous about which branch ran.
 *
 *   (b) Flag ON + mocked getDashboard: the Canvas shell renders, and
 *       clicking Save calls saveDashboard.
 *
 *   (c) Phase 2a wiring: flag ON with a dash_* default layout — period
 *       nav renders, the 3 finance tiles are present, Save calls
 *       saveDashboard with the dash_* widget types.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import DashboardPage from "@/app/dashboard/page";
import { useAuth } from "@/components/auth/AuthProvider";
import * as dashboardApi from "@/lib/dashboard/api";
import { apiFetch } from "@/lib/api";

// ── Mocks ──────────────────────────────────────────────────────────────────

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

// Stub the dashboard API (Task 4).
vi.mock("@/lib/dashboard/api", () => ({
  getDashboard: vi.fn(),
  saveDashboard: vi.fn(),
}));

// Stub the legacy dashboard's apiFetch so it doesn't fire real requests.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

// Stub AppShell — keeps output minimal and avoids SWR / router deps.
vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

// Stub the Canvas (react-grid-layout can't measure widths in jsdom).
// The stub renders a hidden "simulate layout change" button so tests can
// mark the dashboard dirty (needed to enable the Save button).
vi.mock("@/components/reports/Canvas", () => ({
  default: ({
    layout,
    renderWidget,
    onLayoutChange,
  }: {
    layout: { widgets: { id: string }[] };
    renderWidget: (w: { id: string }) => React.ReactNode;
    onLayoutChange: (next: { version: number; widgets: { id: string }[] }) => void;
  }) => (
    <div data-testid="reports-canvas">
      <button
        data-testid="canvas-simulate-change"
        style={{ display: "none" }}
        onClick={() => onLayoutChange({ version: 1, widgets: layout.widgets })}
      />
      {layout.widgets.map((w) => (
        <div key={w.id} data-widget-id={w.id}>
          {renderWidget(w as never)}
        </div>
      ))}
    </div>
  ),
}));

// Stub WidgetShell — the real shell renders WidgetFilterChips which calls
// sourceSupportsDateFilter via SWR. In tests we only need to verify the
// shell is present for each widget; skip all the filter chip machinery.
vi.mock("@/components/reports/WidgetShell", () => ({
  default: ({
    widgetId,
    children,
  }: {
    widgetId: string;
    children: React.ReactNode;
  }) => (
    <div data-testid={`widget-shell-${widgetId}`}>{children}</div>
  ),
}));

// Stub all report widget types — their real implementations call useReportQuery
// → useReportSources → SWR → apiFetch. Tests only verify the dashboard
// frame (load/customize/save), not individual widget data loading.
vi.mock("@/components/reports/widgets/KPIWidget", () => ({
  default: ({ widget }: { widget: { id: string; title: string } }) => (
    <div data-testid={`widget-stub-${widget.id}`}>{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/BarWidget", () => ({
  default: ({ widget }: { widget: { id: string; title: string } }) => (
    <div data-testid={`widget-stub-${widget.id}`}>{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/LineWidget", () => ({
  default: ({ widget }: { widget: { id: string; title: string } }) => (
    <div data-testid={`widget-stub-${widget.id}`}>{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/AreaWidget", () => ({
  default: ({ widget }: { widget: { id: string; title: string } }) => (
    <div data-testid={`widget-stub-${widget.id}`}>{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/PieWidget", () => ({
  default: ({ widget }: { widget: { id: string; title: string } }) => (
    <div data-testid={`widget-stub-${widget.id}`}>{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/SparklineWidget", () => ({
  default: ({ widget }: { widget: { id: string; title: string } }) => (
    <div data-testid={`widget-stub-${widget.id}`}>{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/StackedBarWidget", () => ({
  default: ({ widget }: { widget: { id: string; title: string } }) => (
    <div data-testid={`widget-stub-${widget.id}`}>{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/TableWidget", () => ({
  default: ({ widget }: { widget: { id: string; title: string } }) => (
    <div data-testid={`widget-stub-${widget.id}`}>{widget.title}</div>
  ),
}));
vi.mock("@/components/reports/widgets/SankeyWidget", () => ({
  default: ({ widget }: { widget: { id: string; title: string } }) => (
    <div data-testid={`widget-stub-${widget.id}`}>{widget.title}</div>
  ),
}));

// Stub the 3 dash_* finance tile widget wrappers.
// The real implementations call useDashboard() which requires the provider;
// for these tests we only verify they are rendered by the canvas dispatch.
vi.mock("@/components/dashboard/widgets/OnTrackWidget", () => ({
  default: () => <div data-testid="tile-on-track">OnTrack</div>,
}));
vi.mock("@/components/dashboard/widgets/AccountsWidget", () => ({
  default: () => <div data-testid="tile-accounts">Accounts</div>,
}));
vi.mock("@/components/dashboard/widgets/AccountForecastWidget", () => ({
  default: () => <div data-testid="tile-account-forecast">AccountForecast</div>,
}));

// Stub DashboardDataProvider — the real provider fires apiFetch for
// billing-period, accounts, etc.  For dashboard-frame tests we just need
// the children to render.  DashboardPeriodNav (which reads useDashboard) is
// also stubbed below so the provider context is not required in tests.
vi.mock("@/components/dashboard/DashboardDataProvider", () => ({
  DashboardDataProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="dashboard-data-provider">{children}</div>
  ),
  useDashboard: vi.fn(),
}));

// Stub DashboardPeriodNav — avoids the useDashboard() context requirement.
// The "period nav renders" tests check for this testid directly.
vi.mock("@/components/dashboard/DashboardPeriodNav", () => ({
  default: () => (
    <div data-testid="dashboard-period-nav">
      <button aria-label="Previous period" />
      <span>2026-06-01 – 2026-06-30</span>
      <button aria-label="Next period" />
      <span data-testid="period-nav-current-badge">CURRENT</span>
      <a href="/transactions">View All Transactions</a>
    </div>
  ),
}));

// Stub the mobile-detection hook — default to desktop so editModeActive
// can be toggled on in the flag-ON tests.
vi.mock("@/lib/hooks/use-is-mobile", () => ({
  useIsMobile: vi.fn().mockReturnValue(false),
}));

// Stub next/navigation.
const replaceMock = vi.fn();
const pushMock = vi.fn();
const stableRouter = { push: pushMock, replace: replaceMock };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/dashboard",
}));

// ── Shared fixtures ────────────────────────────────────────────────────────

const BASE_USER = {
  id: 1,
  username: "alice",
  email: "alice@example.com",
  first_name: "Alice",
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

const BASE_DASHBOARD_RESPONSE = {
  id: 1,
  owner_user_id: 1,
  org_id: 1,
  layout_json: {
    version: 1,
    widgets: [
      {
        id: "w_kpi1",
        type: "kpi" as const,
        title: "Total Expenses",
        grid: { x: 0, y: 0, w: 3, h: 2 },
        config: {
          dataset: "transactions" as const,
          measure: { agg: "sum" as const, field: "amount" as const },
          format: "currency" as const,
          compare_prior_period: false,
        },
      },
    ],
  },
  canvas_filters_json: {},
  schema_version: 1,
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
};

/** Phase-2a default layout response — the 3 dash_* finance tiles. */
const DASH_TILES_RESPONSE = {
  id: 1,
  owner_user_id: 1,
  org_id: 1,
  layout_json: {
    version: 1,
    widgets: [
      {
        id: "default-on-track",
        type: "dash_on_track",
        title: "On Track",
        grid: { x: 0, y: 0, w: 12, h: 3 },
        config: {},
      },
      {
        id: "default-accounts",
        type: "dash_accounts",
        title: "Accounts",
        grid: { x: 0, y: 3, w: 4, h: 5 },
        config: {},
      },
      {
        id: "default-account-forecast",
        type: "dash_account_forecast",
        title: "Month-End Forecast",
        grid: { x: 4, y: 3, w: 8, h: 5 },
        config: {},
      },
    ],
  },
  canvas_filters_json: {},
  schema_version: 1,
  created_at: "2026-06-01T00:00:00Z",
  updated_at: "2026-06-01T00:00:00Z",
};

// ── Helper: mock apiFetch for the legacy path ─────────────────────────────

function mockLegacyApiFetch() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/accounts") return Promise.resolve([]);
    if (url === "/api/v1/categories") return Promise.resolve([]);
    if (url === "/api/v1/budgets") return Promise.resolve([]);
    if (url === "/api/v1/settings/billing-cycle")
      return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period")
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings/billing-periods")
      return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
    if (url.startsWith("/api/v1/transactions"))
      return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    if (url.startsWith("/api/v1/forecast-plans/current"))
      return Promise.resolve(null);
    return Promise.resolve({});
  }) as never);
}

// ── Tests ──────────────────────────────────────────────────────────────────

describe("DashboardPage — feature flag", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.pushState({}, "", "/dashboard");
  });

  // ────────────────────────────────────────────────────────────────────────
  // (a) FLAG OFF — legacy dashboard renders, Canvas shell absent
  // ────────────────────────────────────────────────────────────────────────
  describe("flag OFF (default)", () => {
    beforeEach(() => {
      vi.mocked(useAuth).mockReturnValue({
        user: BASE_USER as never,
        loading: false,
        needsSetup: false,
        features: { reports: true, plans: false, customDashboard: false },
        login: vi.fn(),
        register: vi.fn(),
        logout: vi.fn(),
        refreshMe: vi.fn(),
      } as never);
      mockLegacyApiFetch();
    });

    it("renders the legacy dashboard heading", async () => {
      render(<DashboardPage />);
      // The legacy DashboardPage renders an <h1>Dashboard</h1>
      await waitFor(() =>
        expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument(),
      );
    });

    it("does NOT render the custom dashboard shell", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.queryByTestId("custom-dashboard")).toBeNull(),
      );
    });

    it("does NOT call getDashboard", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument(),
      );
      expect(vi.mocked(dashboardApi.getDashboard)).not.toHaveBeenCalled();
    });

    it("does NOT render the period nav", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument(),
      );
      expect(screen.queryByTestId("dashboard-period-nav")).toBeNull();
    });
  });

  // ────────────────────────────────────────────────────────────────────────
  // (b) FLAG ON — Canvas shell renders; Save calls saveDashboard
  // ────────────────────────────────────────────────────────────────────────
  describe("flag ON", () => {
    beforeEach(() => {
      vi.mocked(useAuth).mockReturnValue({
        user: BASE_USER as never,
        loading: false,
        needsSetup: false,
        features: { reports: true, plans: false, customDashboard: true },
        login: vi.fn(),
        register: vi.fn(),
        logout: vi.fn(),
        refreshMe: vi.fn(),
      } as never);

      // getDashboard resolves with the default layout.
      vi.mocked(dashboardApi.getDashboard).mockResolvedValue(
        BASE_DASHBOARD_RESPONSE as never,
      );
      // saveDashboard resolves with the same response.
      vi.mocked(dashboardApi.saveDashboard).mockResolvedValue(
        BASE_DASHBOARD_RESPONSE as never,
      );
    });

    it("renders the custom dashboard shell after load", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByTestId("custom-dashboard")).toBeInTheDocument(),
      );
    });

    it("calls getDashboard on mount", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(vi.mocked(dashboardApi.getDashboard)).toHaveBeenCalledTimes(1),
      );
    });

    it("shows the Canvas after load with the widget from the layout", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByTestId("reports-canvas")).toBeInTheDocument(),
      );
      expect(screen.getByTestId("custom-dashboard")).toBeInTheDocument();
    });

    it("renders DashboardDataProvider wrapper", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByTestId("dashboard-data-provider")).toBeInTheDocument(),
      );
    });

    it("renders the period nav chrome", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByTestId("dashboard-period-nav")).toBeInTheDocument(),
      );
    });

    it("clicking Save calls saveDashboard", async () => {
      render(<DashboardPage />);
      // Wait for load to complete.
      await waitFor(() =>
        expect(screen.getByTestId("custom-dashboard")).toBeInTheDocument(),
      );

      // Enter Customize mode to make Save visible.
      const customizeBtn = screen.getByTestId("custom-dashboard-customize");
      fireEvent.click(customizeBtn);

      // The Canvas stub renders a hidden "simulate change" button.
      // Clicking it calls onLayoutChange, which sets dirty=true and
      // enables the Save button.
      const simulateChangeBtn = screen.getByTestId("canvas-simulate-change");
      fireEvent.click(simulateChangeBtn);

      // Save should now be enabled.
      const saveBtn = await screen.findByTestId("custom-dashboard-save");
      expect(saveBtn).not.toBeDisabled();

      fireEvent.click(saveBtn);

      await waitFor(() =>
        expect(vi.mocked(dashboardApi.saveDashboard)).toHaveBeenCalledTimes(1),
      );
      expect(vi.mocked(dashboardApi.saveDashboard)).toHaveBeenCalledWith(
        expect.objectContaining({
          version: 1,
          widgets: expect.arrayContaining([
            expect.objectContaining({ id: "w_kpi1" }),
          ]),
        }),
        expect.any(Object),
      );
    });

    it("shows a loading spinner before getDashboard resolves", async () => {
      // Never resolve getDashboard — keeps the loading state indefinitely.
      vi.mocked(dashboardApi.getDashboard).mockReturnValue(new Promise(() => {}));
      render(<DashboardPage />);
      expect(screen.getByTestId("custom-dashboard-loading")).toBeInTheDocument();
    });

    it("shows an error state when getDashboard rejects", async () => {
      vi.mocked(dashboardApi.getDashboard).mockRejectedValue(
        new Error("Network error"),
      );
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByTestId("custom-dashboard-error")).toBeInTheDocument(),
      );
      expect(screen.getByTestId("custom-dashboard-error")).toHaveTextContent(
        "Network error",
      );
    });
  });

  // ────────────────────────────────────────────────────────────────────────
  // (c) FLAG ON + dash_* layout — Phase 2a finance tiles + period nav
  // ────────────────────────────────────────────────────────────────────────
  describe("flag ON — Phase 2a dash_* tiles", () => {
    beforeEach(() => {
      vi.mocked(useAuth).mockReturnValue({
        user: BASE_USER as never,
        loading: false,
        needsSetup: false,
        features: { reports: true, plans: false, customDashboard: true },
        login: vi.fn(),
        register: vi.fn(),
        logout: vi.fn(),
        refreshMe: vi.fn(),
      } as never);

      // getDashboard resolves with the 3-tile default layout.
      vi.mocked(dashboardApi.getDashboard).mockResolvedValue(
        DASH_TILES_RESPONSE as never,
      );
      // saveDashboard returns the same layout.
      vi.mocked(dashboardApi.saveDashboard).mockResolvedValue(
        DASH_TILES_RESPONSE as never,
      );
    });

    it("renders the period nav after load", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByTestId("dashboard-period-nav")).toBeInTheDocument(),
      );
    });

    it("renders the CURRENT period badge in the period nav", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByTestId("period-nav-current-badge")).toBeInTheDocument(),
      );
    });

    it("renders the View All Transactions link", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(
          screen.getByRole("link", { name: /view all transactions/i }),
        ).toBeInTheDocument(),
      );
    });

    it("renders all 3 dash_* tile widgets via the canvas dispatch", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByTestId("reports-canvas")).toBeInTheDocument(),
      );
      // Each dash_* type is dispatched to its tile stub
      expect(screen.getByTestId("tile-on-track")).toBeInTheDocument();
      expect(screen.getByTestId("tile-accounts")).toBeInTheDocument();
      expect(screen.getByTestId("tile-account-forecast")).toBeInTheDocument();
    });

    it("wraps content in DashboardDataProvider", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByTestId("dashboard-data-provider")).toBeInTheDocument(),
      );
    });

    it("Save calls saveDashboard with the dash_* layout", async () => {
      render(<DashboardPage />);
      await waitFor(() =>
        expect(screen.getByTestId("custom-dashboard")).toBeInTheDocument(),
      );

      // Enter Customize mode.
      fireEvent.click(screen.getByTestId("custom-dashboard-customize"));

      // Trigger a layout change to mark dirty.
      fireEvent.click(screen.getByTestId("canvas-simulate-change"));

      const saveBtn = await screen.findByTestId("custom-dashboard-save");
      expect(saveBtn).not.toBeDisabled();
      fireEvent.click(saveBtn);

      await waitFor(() =>
        expect(vi.mocked(dashboardApi.saveDashboard)).toHaveBeenCalledTimes(1),
      );

      // The layout passed to saveDashboard must contain the dash_* widget ids
      expect(vi.mocked(dashboardApi.saveDashboard)).toHaveBeenCalledWith(
        expect.objectContaining({
          version: 1,
          widgets: expect.arrayContaining([
            expect.objectContaining({ type: "dash_on_track" }),
            expect.objectContaining({ type: "dash_accounts" }),
            expect.objectContaining({ type: "dash_account_forecast" }),
          ]),
        }),
        expect.any(Object),
      );
    });
  });
});
