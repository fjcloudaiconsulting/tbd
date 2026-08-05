/**
 * TBD-197 PR 2 — G6 (Forecast half).
 *
 * The legacy dashboard shell puts `/api/v1/forecast-plans/current` inside a
 * `Promise.all` in `loadTransactions`. A rejection there does not degrade one
 * tile — it replaces the ENTIRE page with "Failed to load dashboard data".
 * With Forecast gated off server-side the route 404s, so the legacy shell
 * needs its own fetch skips on top of the conditional hiding.
 *
 * ⚠ `/api/v1/forecast/account-balances` is asserted to STILL be requested and
 * its tile to still render: it is an account-projection engine (credit-card
 * cycles + loan amortization) that only shares the URL prefix, and the spec's
 * §5 ruling is that it survives a Forecast opt-out untouched. See the note at
 * the bottom of this file about §6.4's contradictory line reference.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import DashboardPage from "@/app/dashboard/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

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

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/dashboard",
}));

const USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
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

const ACCT = {
  id: 1,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Checking",
  account_type_slug: "checking",
  balance: 100,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const PERIOD = { id: 1, start_date: "2026-05-01", end_date: null };

const ACCOUNT_MONTH_END = {
  period_start: "2026-05-01",
  period_end: "2026-05-31",
  totals: [],
  accounts: [],
};

/**
 * Mirrors a server with Forecast gated off: `/api/v1/forecast` and
 * `/api/v1/forecast-plans/*` 404, while `/forecast/account-balances` — which
 * the gate deliberately leaves open — answers normally.
 */
function forecastGatedOffHandler() {
  return async (url: string) => {
    if (url.startsWith("/api/v1/forecast/account-balances"))
      return ACCOUNT_MONTH_END as never;
    if (
      url.startsWith("/api/v1/forecast-plans") ||
      url.startsWith("/api/v1/forecast")
    ) {
      throw new Error("Not Found");
    }
    if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD] as never;
    if (url.startsWith("/api/v1/settings/billing-period")) return PERIOD as never;
    if (url.startsWith("/api/v1/settings/billing-cycle"))
      return { billing_cycle_day: 1 } as never;
    if (url.startsWith("/api/v1/budgets")) return [] as never;
    if (url.startsWith("/api/v1/transactions")) return { items: [], total: 0 } as never;
    return null as never;
  };
}

function setAuth(forecast: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    needsSetup: false,
    features: {
      reports: false,
      plans: false,
      customDashboard: false,
      forecast,
      budgets: true,
    },
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  window.history.pushState({}, "", "/dashboard");
  vi.mocked(apiFetch).mockImplementation(forecastGatedOffHandler() as never);
});

describe("Legacy dashboard — Forecast disabled (TBD-197 G6)", () => {
  it("skips the gated forecast fetches, keeps account-balances, and shows no whole-page error", async () => {
    setAuth(false);
    render(<DashboardPage />);

    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.startsWith("/api/v1/accounts"))).toBe(true);
    });
    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.startsWith("/api/v1/transactions"))).toBe(true);
    });
    // The ungated account projection must still be requested — this is the
    // clause that dies if someone gates the whole /forecast prefix.
    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(
        calls.some((u) => u.startsWith("/api/v1/forecast/account-balances")),
      ).toBe(true);
    });

    const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
    expect(
      calls.filter(
        (u) =>
          u.startsWith("/api/v1/forecast") &&
          !u.startsWith("/api/v1/forecast/account-balances"),
      ),
    ).toEqual([]);
    expect(calls.filter((u) => u.startsWith("/api/v1/forecast-plans"))).toEqual(
      [],
    );

    expect(screen.queryByText(/Failed to load dashboard data/i)).toBeNull();
    // The two forecast surfaces are hidden rather than rendering empty states
    // whose links now land on the disabled notice.
    expect(screen.queryByText("Forecast by Category")).toBeNull();
    expect(screen.queryByTestId("on-track-tile")).toBeNull();
    // ...while the account projection tile, a Credit-Card / Loan surface, stays
    // and renders REAL data rather than its permanent "Loading…" placeholder.
    const accountTile = screen.getByTestId("account-month-end-forecast");
    expect(accountTile.textContent).not.toMatch(/Loading/);
  });

  it("control: with forecast enabled both surfaces render and both fetches happen", async () => {
    setAuth(true);
    vi.mocked(apiFetch).mockImplementation((async (url: string) => {
      if (url.startsWith("/api/v1/forecast-plans/current")) return null as never;
      if (url.startsWith("/api/v1/forecast/account-balances"))
        return ACCOUNT_MONTH_END as never;
      if (url.startsWith("/api/v1/forecast")) return null as never;
      return forecastGatedOffHandler()(url);
    }) as never);

    render(<DashboardPage />);

    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.startsWith("/api/v1/forecast-plans"))).toBe(
        true,
      );
      expect(
        calls.some(
          (u) =>
            u.startsWith("/api/v1/forecast") &&
            !u.startsWith("/api/v1/forecast/account-balances"),
        ),
      ).toBe(true);
    });
    expect(await screen.findByText("Forecast by Category")).toBeTruthy();
    expect(screen.getByTestId("on-track-tile")).toBeTruthy();
  });
});
