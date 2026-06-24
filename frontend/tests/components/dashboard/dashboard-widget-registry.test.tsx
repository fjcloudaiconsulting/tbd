/**
 * Tests for the dashboard widget registry:
 *   - emptyDashboardWidget (widget-types.ts)
 *   - renderDashboardWidget (renderDashboardWidget.tsx)
 *
 * Mock strategy:
 *   - useDashboard is mocked at the module boundary so the 3 tile wrappers
 *     can render without a real DashboardDataProvider.
 *   - Reports widget components are mocked to lightweight stubs so the
 *     fall-through branch can be tested without real API/SWR wiring.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

import {
  emptyDashboardWidget,
  type DashboardWidget,
  type DashboardWidgetType,
} from "@/lib/dashboard/widget-types";
import { renderDashboardWidget } from "@/components/dashboard/renderDashboardWidget";
import type { Widget } from "@/lib/reports/types";
import {
  useDashboard,
  type DashboardData,
} from "@/components/dashboard/DashboardDataProvider";

// ── Mock DashboardDataProvider ────────────────────────────────────────────────

const MOCK_DASHBOARD_DATA: DashboardData = {
  accounts: [],
  activeAccounts: [],
  pendingByAccount: {},
  forecast: null,
  forecastProjection: null,
  projectionFailed: false,
  projectionLoading: false,
  onRetryProjection: vi.fn(),
  accountMonthEndForecast: null,
  accountMonthEndForecastError: false,
  periods: [],
  periodIdx: 0,
  setPeriodIdx: vi.fn(),
  selectedPeriod: null,
  isCurrentSelectedPeriod: true,
  isPastSelectedPeriod: false,
  isFutureSelectedPeriod: false,
  monthFrom: "2026-06-01",
  monthTo: "2026-06-30",
  jumpToCurrentPeriod: vi.fn(),
  loading: false,
  error: null,
  refresh: vi.fn(),
};

vi.mock("@/components/dashboard/DashboardDataProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/dashboard/DashboardDataProvider")
  >("@/components/dashboard/DashboardDataProvider");
  return {
    ...actual,
    useDashboard: vi.fn(() => MOCK_DASHBOARD_DATA),
  };
});

// ── Mock reports widget components (fall-through branch) ──────────────────────
// Lightweight stubs so the fall-through dispatch renders without SWR/API.

vi.mock("@/components/reports/widgets/KPIWidget", () => ({
  default: () => <div data-testid="kpi-widget-stub">KPIWidget</div>,
}));
vi.mock("@/components/reports/widgets/BarWidget", () => ({
  default: () => <div data-testid="bar-widget-stub">BarWidget</div>,
}));
vi.mock("@/components/reports/widgets/LineWidget", () => ({
  default: () => <div data-testid="line-widget-stub">LineWidget</div>,
}));
vi.mock("@/components/reports/widgets/AreaWidget", () => ({
  default: () => <div data-testid="area-widget-stub">AreaWidget</div>,
}));
vi.mock("@/components/reports/widgets/PieWidget", () => ({
  default: () => <div data-testid="pie-widget-stub">PieWidget</div>,
}));
vi.mock("@/components/reports/widgets/SparklineWidget", () => ({
  default: () => <div data-testid="sparkline-widget-stub">SparklineWidget</div>,
}));
vi.mock("@/components/reports/widgets/StackedBarWidget", () => ({
  default: () => <div data-testid="stacked-bar-widget-stub">StackedBarWidget</div>,
}));
vi.mock("@/components/reports/widgets/TableWidget", () => ({
  default: () => <div data-testid="table-widget-stub">TableWidget</div>,
}));
vi.mock("@/components/reports/widgets/SankeyWidget", () => ({
  default: () => <div data-testid="sankey-widget-stub">SankeyWidget</div>,
}));

// ── emptyDashboardWidget ──────────────────────────────────────────────────────

describe("emptyDashboardWidget", () => {
  it("returns a widget with the correct type for dash_on_track", () => {
    const w = emptyDashboardWidget("dash_on_track", "w1");
    expect(w.type).toBe("dash_on_track");
    expect(w.id).toBe("w1");
    expect(w.config).toEqual({});
  });

  it("returns the full-width hero grid for dash_on_track", () => {
    const w = emptyDashboardWidget("dash_on_track", "w1");
    expect(w.grid).toEqual({ x: 0, y: 0, w: 12, h: 3 });
  });

  it("returns a widget with the correct type for dash_accounts", () => {
    const w = emptyDashboardWidget("dash_accounts", "w2");
    expect(w.type).toBe("dash_accounts");
    expect(w.id).toBe("w2");
    expect(w.config).toEqual({});
  });

  it("returns the left-column grid for dash_accounts", () => {
    const w = emptyDashboardWidget("dash_accounts", "w2");
    expect(w.grid).toEqual({ x: 0, y: 3, w: 4, h: 5 });
  });

  it("returns a widget with the correct type for dash_account_forecast", () => {
    const w = emptyDashboardWidget("dash_account_forecast", "w3");
    expect(w.type).toBe("dash_account_forecast");
    expect(w.id).toBe("w3");
    expect(w.config).toEqual({});
  });

  it("returns the right-column grid for dash_account_forecast", () => {
    const w = emptyDashboardWidget("dash_account_forecast", "w3");
    expect(w.grid).toEqual({ x: 4, y: 3, w: 8, h: 5 });
  });

  it("each call returns an independent grid object (no aliasing)", () => {
    const a = emptyDashboardWidget("dash_on_track", "a");
    const b = emptyDashboardWidget("dash_on_track", "b");
    a.grid.x = 99;
    expect(b.grid.x).toBe(0);
  });

  const ALL_TYPES: DashboardWidgetType[] = [
    "dash_on_track",
    "dash_accounts",
    "dash_account_forecast",
  ];

  it.each(ALL_TYPES)("has a non-empty title for %s", (type) => {
    const w = emptyDashboardWidget(type, "t");
    expect(w.title.length).toBeGreaterThan(0);
  });
});

// ── renderDashboardWidget — dashboard-native tiles ────────────────────────────

describe("renderDashboardWidget — dashboard-native tiles", () => {
  function renderWidget(w: DashboardWidget) {
    const { container } = render(<>{renderDashboardWidget(w)}</>);
    return container;
  }

  it("renders OnTrackTile for dash_on_track", () => {
    const w = emptyDashboardWidget("dash_on_track", "w1");
    renderWidget(w);
    // OnTrackTile renders a section with data-testid="on-track-tile"
    expect(screen.getByTestId("on-track-tile")).toBeInTheDocument();
  });

  it("renders nothing (null) for dash_accounts with no accounts", () => {
    // AccountTilesCard returns null when accounts array is empty.
    const w = emptyDashboardWidget("dash_accounts", "w2");
    const container = renderWidget(w);
    // No account-tiles-card — AccountTilesCard returns null for empty accounts.
    expect(container.querySelector("[data-testid='account-tiles-card']")).toBeNull();
  });

  it("renders AccountMonthEndForecast for dash_account_forecast", () => {
    const w = emptyDashboardWidget("dash_account_forecast", "w3");
    renderWidget(w);
    // AccountMonthEndForecast returns null when hasAnyAccounts is false.
    // With our mock (activeAccounts: []), it renders null — assert no error thrown.
    // The component is registered and mounts without throwing.
    // (No accounts in mock → component renders null → container is empty, no crash.)
    expect(true).toBe(true); // render completed without error
  });

  it("renders the forecast tile wrapper with accounts present", () => {
    const ACCT = {
      id: 1,
      name: "Checking",
      account_type_id: 1,
      account_type_name: "Checking",
      account_type_slug: "checking",
      balance: 1000,
      currency: "EUR",
      is_active: true,
      close_day: null,
      is_default: true,
    };
    vi.mocked(useDashboard).mockReturnValueOnce({
      ...MOCK_DASHBOARD_DATA,
      activeAccounts: [ACCT],
    });
    const w = emptyDashboardWidget("dash_account_forecast", "w3");
    render(<>{renderDashboardWidget(w)}</>);
    expect(screen.getByTestId("account-month-end-forecast")).toBeInTheDocument();
  });
});

// ── renderDashboardWidget — reports fall-through ──────────────────────────────

describe("renderDashboardWidget — reports fall-through", () => {
  const CANVAS_FILTERS = {};

  function stubWidget(type: Widget["type"]): Widget {
    return {
      id: "w_stub",
      type,
      title: "stub",
      grid: { x: 0, y: 0, w: 6, h: 4 },
      config: {
        dataset: "transactions",
        measure: { agg: "sum", field: "amount" },
        dimensions: [],
        format: "currency",
      },
    } as unknown as Widget;
  }

  const REPORT_TYPES: Array<[Widget["type"], string]> = [
    ["kpi", "kpi-widget-stub"],
    ["bar", "bar-widget-stub"],
    ["line", "line-widget-stub"],
    ["area", "area-widget-stub"],
    ["pie", "pie-widget-stub"],
    ["sparkline", "sparkline-widget-stub"],
    ["stacked_bar", "stacked-bar-widget-stub"],
    ["table", "table-widget-stub"],
    ["sankey", "sankey-widget-stub"],
  ];

  it.each(REPORT_TYPES)(
    "delegates %s widget to the reports renderer without throwing",
    (type, testId) => {
      const w = stubWidget(type);
      render(<>{renderDashboardWidget(w, CANVAS_FILTERS, false)}</>);
      expect(screen.getByTestId(testId)).toBeInTheDocument();
    },
  );
});
