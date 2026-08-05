/**
 * TBD-197 PR 1 — G6.
 *
 * The legacy dashboard shell puts `/api/v1/budgets` inside a `Promise.all`
 * (loadRefs and loadTransactions both). A rejection there does not degrade one
 * tile — it replaces the ENTIRE page with "Failed to load dashboard data".
 * With Budgets gated off server-side the route 404s, so the legacy shell needs
 * its own fetch skips on top of the conditional hiding.
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

/** Mirrors a server with Budgets gated off: every /api/v1/budgets call 404s. */
function budgetsGatedOffHandler() {
  return async (url: string) => {
    if (url.startsWith("/api/v1/budgets")) {
      throw new Error("Not Found");
    }
    if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD] as never;
    if (url.startsWith("/api/v1/settings/billing-period")) return PERIOD as never;
    if (url.startsWith("/api/v1/settings/billing-cycle"))
      return { billing_cycle_day: 1 } as never;
    if (url.startsWith("/api/v1/forecast-plans/current")) return null as never;
    if (url.startsWith("/api/v1/forecast/account-balances")) return null as never;
    if (url.startsWith("/api/v1/forecast")) return null as never;
    if (url.startsWith("/api/v1/transactions")) return { items: [], total: 0 } as never;
    return null as never;
  };
}

function setAuth(budgets: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    needsSetup: false,
    features: {
      reports: false,
      plans: false,
      customDashboard: false,
      forecast: true,
      budgets,
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
  vi.mocked(apiFetch).mockImplementation(budgetsGatedOffHandler() as never);
});

describe("Legacy dashboard — Budgets disabled (TBD-197 G6)", () => {
  it("does not surface the whole-page error banner and does not call /api/v1/budgets", async () => {
    setAuth(false);
    render(<DashboardPage />);

    // Wait for the reference load to settle (accounts arrive).
    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.startsWith("/api/v1/accounts"))).toBe(true);
    });
    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.startsWith("/api/v1/transactions"))).toBe(true);
    });

    const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
    expect(calls.filter((u) => u.startsWith("/api/v1/budgets"))).toEqual([]);
    expect(screen.queryByText(/Failed to load dashboard data/i)).toBeNull();
    // The Budget Progress card is hidden rather than rendering an empty state
    // that cannot be acted on.
    expect(screen.queryByText("Budget Progress")).toBeNull();
  });

  it("control: with budgets enabled the card renders and the fetch happens", async () => {
    setAuth(true);
    vi.mocked(apiFetch).mockImplementation((async (url: string) => {
      if (url.startsWith("/api/v1/budgets")) return [] as never;
      return budgetsGatedOffHandler()(url);
    }) as never);

    render(<DashboardPage />);

    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.startsWith("/api/v1/budgets"))).toBe(true);
    });
    expect(await screen.findByText("Budget Progress")).toBeTruthy();
  });
});
