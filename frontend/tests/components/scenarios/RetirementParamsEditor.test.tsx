/**
 * Regression test for the debounced PATCH spam reported in production
 * (2026-05-22): when the user typed in a retirement plan's Monthly
 * field but the contribution curve still had a row with no `from`
 * date, the editor kept firing PATCH /api/v1/scenarios/:id every 400
 * ms and the server returned 422 each time.
 *
 * The fix gates the debounced effect on the editor's client-side
 * validity flag (`onValidityChange` from RetirementParamsEditor flows
 * up to the parent and short-circuits the persist call). This test
 * drives the Plans page editor end-to-end and asserts no PATCH fires
 * while the inline error is visible, then asserts the PATCH starts
 * firing again once the user fills the missing date.
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

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

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  ComposedChart: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
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
  projection_json: null,
  projection_engine: null,
  projection_computed_at: null,
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

function setUser() {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

describe("debounced PATCH gating via client-side validity", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
    replaceMock.mockReset();
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("skips PATCH while a curve row is missing its `from` date", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (url === "/api/v1/scenarios" && !opts?.method) {
        return Promise.resolve([RETIREMENT_PLAN]);
      }
      if (url === "/api/v1/accounts") {
        return Promise.resolve([SAMPLE_ACCOUNT]);
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<PlansPage />);
    await screen.findByText("Retire at 65");
    fireEvent.click(screen.getByTestId(`plan-row-${RETIREMENT_PLAN.id}`));
    await screen.findByTestId("plan-editor");

    // Add a curve row but leave its `from` empty. The editor's
    // validateCurve() returns an inline error.
    fireEvent.click(screen.getByTestId("ret-curve-add"));
    await waitFor(() => {
      expect(screen.getByTestId("ret-curve-error")).toBeInTheDocument();
    });

    // Now change the Monthly base field. Without the gate, this would
    // schedule a debounced PATCH that the server would reject 422.
    const before = apiFetchMock.mock.calls.length;
    fireEvent.change(screen.getByTestId("ret-monthly"), {
      target: { value: "750.00" },
    });

    // Walk well past the architect-locked 400 ms debounce.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    const patchCalls = apiFetchMock.mock.calls
      .slice(before)
      .filter(([, o]) => (o as RequestInit | undefined)?.method === "PATCH");
    expect(patchCalls).toEqual([]);
  });

  it("resumes PATCH once the curve row's `from` date is filled in", async () => {
    setUser();
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (url === "/api/v1/scenarios" && !opts?.method) {
        return Promise.resolve([RETIREMENT_PLAN]);
      }
      if (url === "/api/v1/accounts") {
        return Promise.resolve([SAMPLE_ACCOUNT]);
      }
      if (
        url === `/api/v1/scenarios/${RETIREMENT_PLAN.id}`
        && opts?.method === "PATCH"
      ) {
        return Promise.resolve(RETIREMENT_PLAN);
      }
      if (
        url === `/api/v1/scenarios/${RETIREMENT_PLAN.id}/simulate`
        && opts?.method === "POST"
      ) {
        return Promise.resolve(RETIREMENT_PLAN);
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<PlansPage />);
    await screen.findByText("Retire at 65");
    fireEvent.click(screen.getByTestId(`plan-row-${RETIREMENT_PLAN.id}`));
    await screen.findByTestId("plan-editor");

    // Step 1: add the empty-from row + change Monthly, confirm no PATCH.
    fireEvent.click(screen.getByTestId("ret-curve-add"));
    await waitFor(() => {
      expect(screen.getByTestId("ret-curve-error")).toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId("ret-monthly"), {
      target: { value: "750.00" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(
      apiFetchMock.mock.calls.filter(
        ([, o]) => (o as RequestInit | undefined)?.method === "PATCH",
      ),
    ).toEqual([]);

    // Step 2: fill the row's `from`. Editor flips back to valid. A
    // subsequent edit should trigger PATCH within the debounce window.
    fireEvent.change(screen.getByTestId("ret-curve-from-0"), {
      target: { value: "2040-01-01" },
    });
    await waitFor(() => {
      expect(screen.queryByTestId("ret-curve-error")).not.toBeInTheDocument();
    });
    fireEvent.change(screen.getByTestId("ret-monthly"), {
      target: { value: "800.00" },
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    await waitFor(() => {
      const patchCalls = apiFetchMock.mock.calls.filter(
        ([url, o]) =>
          url === `/api/v1/scenarios/${RETIREMENT_PLAN.id}`
          && (o as RequestInit | undefined)?.method === "PATCH",
      );
      expect(patchCalls.length).toBeGreaterThanOrEqual(1);
    });
  });
});
