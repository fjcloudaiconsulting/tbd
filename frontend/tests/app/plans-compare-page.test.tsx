/**
 * Tests for the /plans/compare page (PR3 of the Plans train).
 *
 * Architect-locked checks:
 * - Picker renders one checkbox per plan from /api/v1/scenarios.
 * - Selecting plans + clicking Compare POSTs to
 *   /api/v1/scenarios/compare with the chosen ids + horizon.
 * - ComparisonView renders once results come back.
 * - The verdict matrix shows one row per scenario.
 */
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import ComparePlansPage from "@/app/plans/compare/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

const replaceMock = vi.fn();
const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  usePathname: () => "/plans/compare",
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-responsive">{children}</div>
  ),
  LineChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-line-chart">{children}</div>
  ),
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Legend: () => null,
  Line: () => null,
}));

const USER = {
  id: 1,
  username: "alice",
  email: "alice@acme.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner" as const,
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

const PLANS = [
  { id: 1, name: "Lisbon trip", scenario_type: "trip" as const, horizon_months: 24 },
  { id: 2, name: "Used car", scenario_type: "purchase" as const, horizon_months: 36 },
  { id: 3, name: "Retire at 65", scenario_type: "retirement" as const, horizon_months: 360 },
  { id: 4, name: "Sabbatical", scenario_type: "custom" as const, horizon_months: 12 },
];

function makeProjection(id: number, name: string, color: "green" | "yellow" | "red") {
  return {
    scenario_id: id,
    name,
    scenario_type: "trip" as const,
    projection: {
      engine_name: "analytic_v1",
      horizon_months: 24,
      currency: "EUR",
      per_account_series: [
        {
          account_id: 12,
          account_name: "Main",
          currency: "EUR",
          points: [
            { month: "2026-06", projected_balance: "4000.00" },
            { month: "2026-07", projected_balance: "4100.00" },
          ],
        },
      ],
      alerts: color === "red" ? [{
        account_id: 12,
        month: "2026-06",
        projected_balance: "-100.00",
        trigger: "trip_lump_sum",
        severity: "warn" as const,
      }] : [],
      verdict: {
        color,
        headline: "h",
        reason: "r",
      },
    },
  };
}

describe("/plans/compare page", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    replaceMock.mockReset();
    pushMock.mockReset();
  });

  function setUser() {
    useAuthMock.mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  }

  it("renders one checkbox per plan from the API", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/scenarios") return Promise.resolve(PLANS);
      return Promise.resolve(undefined);
    }) as never);
    render(<ComparePlansPage />);
    await waitFor(() => {
      expect(screen.getByTestId("compare-plan-checkbox-1")).toBeInTheDocument();
      expect(screen.getByTestId("compare-plan-checkbox-2")).toBeInTheDocument();
      expect(screen.getByTestId("compare-plan-checkbox-3")).toBeInTheDocument();
      expect(screen.getByTestId("compare-plan-checkbox-4")).toBeInTheDocument();
    });
  });

  it("selecting and clicking Compare POSTs to /api/v1/scenarios/compare and renders results", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string, opts?: { method?: string; body?: string }) => {
      if (url === "/api/v1/scenarios" && (!opts || !opts.method)) {
        return Promise.resolve(PLANS);
      }
      if (url === "/api/v1/scenarios/compare") {
        return Promise.resolve({
          projections: [
            makeProjection(1, "Lisbon trip", "yellow"),
            makeProjection(2, "Used car", "green"),
          ],
        });
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<ComparePlansPage />);
    await screen.findByTestId("compare-plan-checkbox-1");

    fireEvent.click(screen.getByTestId("compare-plan-checkbox-1"));
    fireEvent.click(screen.getByTestId("compare-plan-checkbox-2"));
    fireEvent.click(screen.getByTestId("compare-run"));

    await screen.findByTestId("compare-results");
    // Verdict matrix renders one row per scenario.
    expect(screen.getByTestId("comparison-view-row-1")).toBeInTheDocument();
    expect(screen.getByTestId("comparison-view-row-2")).toBeInTheDocument();

    // Verify the POST body shape.
    const compareCall = apiFetchMock.mock.calls.find(
      (c) => c[0] === "/api/v1/scenarios/compare",
    );
    expect(compareCall).toBeDefined();
    const body = JSON.parse((compareCall![1] as { body: string }).body);
    expect(body.scenario_ids).toEqual([1, 2]);
    expect(body.horizon_months).toBe(24);
  });

  it("disables further selection beyond the architect cap of 3", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/scenarios") return Promise.resolve(PLANS);
      return Promise.resolve(undefined);
    }) as never);
    render(<ComparePlansPage />);
    await screen.findByTestId("compare-plan-checkbox-1");
    fireEvent.click(screen.getByTestId("compare-plan-checkbox-1"));
    fireEvent.click(screen.getByTestId("compare-plan-checkbox-2"));
    fireEvent.click(screen.getByTestId("compare-plan-checkbox-3"));
    // The 4th checkbox should now be disabled.
    const fourth = screen.getByTestId("compare-plan-checkbox-4") as HTMLInputElement;
    expect(fourth.disabled).toBe(true);
  });
});
