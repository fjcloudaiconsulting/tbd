/**
 * Tests for the Plans page (spec 2026-05-22).
 *
 * Architect-locked checks:
 * - Empty state copy renders when there are zero plans yet.
 * - List view renders one row per plan returned by the API.
 * - "New plan" with template = Trip opens the modal with the
 *   destination field; submit POSTs to /api/v1/scenarios with the
 *   right body shape.
 * - "Re-simulate" button calls POST /api/v1/scenarios/{id}/simulate.
 * - Verdict badge color matches the API verdict.
 */
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import PlansPage from "@/app/plans/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

const replaceMock = vi.fn();
let searchParamsString = "";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/plans",
  useSearchParams: () => new URLSearchParams(searchParamsString),
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
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

// Stub Recharts: the real chart renders into a JSDOM-incompatible SVG
// pipeline. The structural stub keeps it out of the way while letting
// us assert that the chart wrapper renders by data-testid.
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-responsive">{children}</div>
  ),
  ComposedChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-chart">{children}</div>
  ),
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Legend: () => null,
  Area: () => null,
  Line: () => null,
  ReferenceDot: () => null,
  Dot: () => null,
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

const TRIP_PLAN = {
  id: 7,
  org_id: 1,
  user_id: 1,
  name: "Lisbon trip",
  scenario_type: "trip" as const,
  params_json: {
    scenario_type: "trip",
    destination: "Lisbon, Portugal",
    start_date: "2026-09-15",
    duration_days: 10,
    currency: "EUR",
    transport_cost: "450.00",
    accommodation_per_night: "85.00",
    daily_budget: "70.00",
    one_off_extras: [],
    source_account_id: 12,
  },
  projection_json: {
    engine_name: "analytic_v1",
    computed_at: "2026-05-22T09:15:00",
    horizon_months: 24,
    currency: "EUR",
    per_account_series: [
      {
        account_id: 12,
        account_name: "Main checking",
        currency: "EUR",
        points: [{ month: "2026-06", projected_balance: "4200.00" }],
      },
    ],
    alerts: [],
    verdict: {
      color: "yellow" as const,
      headline: "Plan is feasible but cuts close",
      reason: "x",
    },
    suggestions: [],
  },
  projection_engine: "analytic_v1",
  projection_computed_at: "2026-05-22T09:15:00",
  horizon_months: 24,
  is_active: true,
  created_at: "2026-05-22T00:00:00",
  updated_at: "2026-05-22T00:00:00",
};

const RETIREMENT_PLAN = {
  id: 8,
  org_id: 1,
  user_id: 1,
  name: "Retire at 65",
  scenario_type: "retirement" as const,
  params_json: {
    scenario_type: "retirement",
    target_retirement_date: "2050-01-01",
    currency: "EUR",
    monthly_contribution: "500.00",
    contribution_account_id: 12,
    target_balance: "100000.00",
    annual_return_pct: "6.0",
    inflation_pct: "2.5",
    contribution_curve: [],
  },
  projection_json: {
    engine_name: "analytic_v1",
    computed_at: "2026-05-22T09:15:00",
    horizon_months: 360,
    currency: "EUR",
    per_account_series: [
      {
        account_id: 12,
        account_name: "Retirement",
        currency: "EUR",
        points: [
          { month: "2026-06", projected_balance: "500.00" },
          { month: "2026-07", projected_balance: "1002.50" },
        ],
      },
    ],
    alerts: [],
    verdict: {
      color: "red" as const,
      headline: "Retirement target out of reach at current pace.",
      reason: "Real-terms balance falls below the target.",
    },
    suggestions: [
      {
        action: "raise_monthly_contribution",
        by_amount: "200.00",
        expected_outcome: "Raise the monthly contribution by 200.00 to close the gap.",
      },
    ],
    real_terms_series: {
      points: [
        { month: "2026-06", projected_balance: "499.00" },
        { month: "2026-07", projected_balance: "997.00" },
      ],
      inflation_pct: "2.50",
    },
    smoothed_with_regression: false,
  },
  projection_engine: "analytic_v1",
  projection_computed_at: "2026-05-22T09:15:00",
  horizon_months: 360,
  is_active: true,
  created_at: "2026-05-22T00:00:00",
  updated_at: "2026-05-22T00:00:00",
};

const SAMPLE_ACCOUNT = {
  id: 12,
  name: "Main checking",
  currency: "EUR",
  balance: "5000.00",
};

describe("/plans page", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    replaceMock.mockReset();
    searchParamsString = "";
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

  it("redirects an unauthenticated visitor to /login", async () => {
    useAuthMock.mockReturnValue({
      user: null,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    render(<PlansPage />);
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/login");
    });
  });

  it("renders the empty state when the user has no plans", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/scenarios") return Promise.resolve([]);
      if (url === "/api/v1/accounts") return Promise.resolve([SAMPLE_ACCOUNT]);
      return Promise.resolve(undefined);
    }) as never);
    render(<PlansPage />);
    await screen.findByTestId("plans-empty");
    expect(screen.getByTestId("plans-empty")).toHaveTextContent(/No plans yet/i);
  });

  it("renders a row per plan and shows the verdict badge", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/scenarios") return Promise.resolve([TRIP_PLAN]);
      if (url === "/api/v1/accounts") return Promise.resolve([SAMPLE_ACCOUNT]);
      return Promise.resolve(undefined);
    }) as never);
    render(<PlansPage />);
    await screen.findByText("Lisbon trip");
    expect(screen.getByText(/Trip.*Horizon 24mo/i)).toBeInTheDocument();
    expect(screen.getByText("YELLOW")).toBeInTheDocument();
  });

  it("opens the New plan modal with the destination field for Trip template", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/scenarios") return Promise.resolve([]);
      if (url === "/api/v1/accounts") return Promise.resolve([SAMPLE_ACCOUNT]);
      return Promise.resolve(undefined);
    }) as never);
    render(<PlansPage />);
    await screen.findByTestId("plans-empty");
    fireEvent.click(screen.getByTestId("plans-new"));
    await screen.findByTestId("new-plan-modal");
    expect(screen.getByTestId("new-plan-destination")).toBeInTheDocument();
    // The template selector defaults to trip.
    const select = screen.getByTestId("new-plan-template") as HTMLSelectElement;
    expect(select.value).toBe("trip");
  });

  it("POSTs to /api/v1/scenarios when the Create button is clicked", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string, options?: RequestInit) => {
      if (url === "/api/v1/scenarios" && !options?.method) {
        return Promise.resolve([]);
      }
      if (url === "/api/v1/accounts") {
        return Promise.resolve([SAMPLE_ACCOUNT]);
      }
      if (url === "/api/v1/scenarios" && options?.method === "POST") {
        return Promise.resolve({ ...TRIP_PLAN, id: 9, name: "Sample trip" });
      }
      return Promise.resolve(undefined);
    }) as never);
    render(<PlansPage />);
    await screen.findByTestId("plans-empty");
    fireEvent.click(screen.getByTestId("plans-new"));
    await screen.findByTestId("new-plan-modal");
    fireEvent.change(screen.getByTestId("new-plan-name"), {
      target: { value: "Sample trip" },
    });
    fireEvent.click(screen.getByTestId("new-plan-submit"));
    await waitFor(() => {
      const postCall = apiFetchMock.mock.calls.find(
        ([url, opts]) =>
          url === "/api/v1/scenarios" && (opts as RequestInit | undefined)?.method === "POST",
      );
      expect(postCall).toBeDefined();
      const body = JSON.parse((postCall![1] as RequestInit).body as string);
      expect(body.name).toBe("Sample trip");
      expect(body.scenario_type).toBe("trip");
      expect(body.horizon_months).toBe(24);
      expect(body.params.scenario_type).toBe("trip");
      expect(body.params.source_account_id).toBe(SAMPLE_ACCOUNT.id);
    });
  });

  it("renders the retirement form with target date and curve editor", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/scenarios") return Promise.resolve([RETIREMENT_PLAN]);
      if (url === "/api/v1/accounts") return Promise.resolve([SAMPLE_ACCOUNT]);
      return Promise.resolve(undefined);
    }) as never);
    render(<PlansPage />);
    await screen.findByText("Retire at 65");
    fireEvent.click(screen.getByTestId(`plan-row-${RETIREMENT_PLAN.id}`));
    await screen.findByTestId("plan-editor");
    expect(screen.getByTestId("ret-target-date")).toBeInTheDocument();
    expect(screen.getByTestId("ret-target-balance")).toBeInTheDocument();
    expect(screen.getByTestId("ret-monthly")).toBeInTheDocument();
    expect(screen.getByTestId("ret-curve-table")).toBeInTheDocument();
    expect(screen.getByTestId("ret-curve-add")).toBeInTheDocument();
  });

  it("retirement curve editor adds a row when 'Add step' is clicked", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/scenarios") return Promise.resolve([RETIREMENT_PLAN]);
      if (url === "/api/v1/accounts") return Promise.resolve([SAMPLE_ACCOUNT]);
      return Promise.resolve(undefined);
    }) as never);
    render(<PlansPage />);
    await screen.findByText("Retire at 65");
    fireEvent.click(screen.getByTestId(`plan-row-${RETIREMENT_PLAN.id}`));
    await screen.findByTestId("plan-editor");
    fireEvent.click(screen.getByTestId("ret-curve-add"));
    expect(screen.getByTestId("ret-curve-row-0")).toBeInTheDocument();
    expect(screen.getByTestId("ret-curve-from-0")).toBeInTheDocument();
  });

  it("retirement curve editor rejects out-of-order date entries", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/scenarios") return Promise.resolve([RETIREMENT_PLAN]);
      if (url === "/api/v1/accounts") return Promise.resolve([SAMPLE_ACCOUNT]);
      return Promise.resolve(undefined);
    }) as never);
    render(<PlansPage />);
    await screen.findByText("Retire at 65");
    fireEvent.click(screen.getByTestId(`plan-row-${RETIREMENT_PLAN.id}`));
    await screen.findByTestId("plan-editor");
    fireEvent.click(screen.getByTestId("ret-curve-add"));
    fireEvent.click(screen.getByTestId("ret-curve-add"));
    fireEvent.change(screen.getByTestId("ret-curve-from-0"), {
      target: { value: "2030-01-01" },
    });
    fireEvent.change(screen.getByTestId("ret-curve-from-1"), {
      target: { value: "2028-01-01" },
    });
    await waitFor(() => {
      expect(screen.getByTestId("ret-curve-error")).toHaveTextContent(/ascending/i);
    });
  });

  it("retirement plan renders chart, verdict badge color, and suggestion text", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/scenarios") return Promise.resolve([RETIREMENT_PLAN]);
      if (url === "/api/v1/accounts") return Promise.resolve([SAMPLE_ACCOUNT]);
      return Promise.resolve(undefined);
    }) as never);
    render(<PlansPage />);
    await screen.findByText("Retire at 65");
    fireEvent.click(screen.getByTestId(`plan-row-${RETIREMENT_PLAN.id}`));
    await screen.findByTestId("plan-editor");
    expect(screen.getByTestId("projection-chart")).toBeInTheDocument();
    const badge = screen.getByTestId("verdict-badge");
    expect(badge).toHaveTextContent("RED");
    expect(badge.className).toMatch(/danger/);
    expect(screen.getByTestId("projection-suggestions")).toHaveTextContent(
      /close the gap/i,
    );
  });

  it("Simulate button on a row POSTs to /api/v1/scenarios/{id}/simulate", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string, options?: RequestInit) => {
      if (url === "/api/v1/scenarios" && !options?.method) {
        return Promise.resolve([TRIP_PLAN]);
      }
      if (url === "/api/v1/accounts") {
        return Promise.resolve([SAMPLE_ACCOUNT]);
      }
      if (
        url === `/api/v1/scenarios/${TRIP_PLAN.id}/simulate`
        && options?.method === "POST"
      ) {
        return Promise.resolve(TRIP_PLAN);
      }
      return Promise.resolve(undefined);
    }) as never);
    render(<PlansPage />);
    await screen.findByText("Lisbon trip");
    fireEvent.click(screen.getByTestId(`plan-simulate-${TRIP_PLAN.id}`));
    await waitFor(() => {
      const simulateCall = apiFetchMock.mock.calls.find(
        ([url, opts]) =>
          url === `/api/v1/scenarios/${TRIP_PLAN.id}/simulate`
          && (opts as RequestInit | undefined)?.method === "POST",
      );
      expect(simulateCall).toBeDefined();
      const body = JSON.parse((simulateCall![1] as RequestInit).body as string);
      expect(body.engine).toBe("analytic");
    });
  });

  it(
    "test_open_query_param_opens_matching_scenario_in_editor: "
    + "?open=<id> matches a loaded scenario → editor renders, "
    + "URL is replaced to /plans (no query string)",
    async () => {
      setUser();
      // Land on /plans?open=7 — should open TRIP_PLAN in the editor.
      searchParamsString = `open=${TRIP_PLAN.id}`;
      apiFetchMock.mockImplementation(((url: string) => {
        if (url === "/api/v1/scenarios") return Promise.resolve([TRIP_PLAN]);
        if (url === "/api/v1/accounts") return Promise.resolve([SAMPLE_ACCOUNT]);
        return Promise.resolve(undefined);
      }) as never);
      render(<PlansPage />);
      // The editor renders with the deep-linked plan active.
      await screen.findByTestId("plan-editor");
      // List view is gone.
      expect(screen.queryByTestId("plans-list")).not.toBeInTheDocument();
      // Plan name shows in the editor's name input.
      const nameInput = screen.getByTestId("plan-name-input") as HTMLInputElement;
      expect(nameInput.value).toBe(TRIP_PLAN.name);
      // URL was cleaned to /plans (no query string) so a refresh doesn't
      // re-trigger the open behavior.
      await waitFor(() => {
        expect(replaceMock).toHaveBeenCalledWith("/plans");
      });
    },
  );

  it(
    "test_open_query_param_with_unknown_id_stays_on_list: "
    + "?open=99 with no matching scenario → list renders, URL replaced",
    async () => {
      setUser();
      // Land on /plans?open=99 but the loaded list only has id=7.
      searchParamsString = "open=99";
      apiFetchMock.mockImplementation(((url: string) => {
        if (url === "/api/v1/scenarios") return Promise.resolve([TRIP_PLAN]);
        if (url === "/api/v1/accounts") return Promise.resolve([SAMPLE_ACCOUNT]);
        return Promise.resolve(undefined);
      }) as never);
      render(<PlansPage />);
      // List view renders.
      await screen.findByTestId("plans-list");
      expect(screen.queryByTestId("plan-editor")).not.toBeInTheDocument();
      // URL is still cleaned up so a refresh doesn't retry forever.
      await waitFor(() => {
        expect(replaceMock).toHaveBeenCalledWith("/plans");
      });
    },
  );
});
