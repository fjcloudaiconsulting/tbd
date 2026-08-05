/**
 * TBD-197 PR 2 — the three "From Forecast" affordances on /budgets.
 *
 * Budgets stays fully ON here. This is the CROSS-FEATURE case: an org that
 * switched Forecast off keeps its budgets page, but every control on it that
 * reads a ForecastPlan is now a button to a 404 —
 * `POST /api/v1/budgets/from-forecast` carries the Forecast gate as an
 * additional handler-level dependency (fence F8).
 *
 * The three sites, all of them, in one file:
 *   1. the header "From Forecast" button (current period only)
 *   2. the next-period seed panel's "From forecast" button
 *   3. the current-period empty-state prose naming "From Forecast"
 *
 * ⚠ Each assertion is paired with a `forecast: true` control in the same
 * fixture, because sites 2 and 3 are already conditional on the selected
 * period: a fence that only checked absence would pass against a build that
 * changed nothing, simply by landing on the wrong period.
 */
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import BudgetsPage from "@/app/budgets/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { useAiStatus } from "@/lib/hooks/use-ai-status";

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

vi.mock("@/lib/hooks/use-ai-status", () => ({
  useAiStatus: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/budgets",
  useSearchParams: () => ({ get: () => null }),
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
  allow_manual_balance_adjustment: false,
};

// Two periods: the OPEN current one, and a future stub so the next-period
// seed panel is reachable.
//
// ⚠ Anchored to the WALL CLOCK, not to date literals. `periodStatus` calls the
// upcoming branch only for `end_date !== null && start_date > today`, so a
// fixed "2026-06-01" stub silently becomes `past` the moment the calendar
// passes it and the seed panel stops rendering — a fence that goes green by
// never reaching the thing it tests.
function iso(offsetDays: number): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

const PERIOD_CURRENT = { id: 1, start_date: iso(-10), end_date: null };
const PERIOD_NEXT = { id: 2, start_date: iso(10), end_date: iso(40) };

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
      // Budgets stays ON: this file is about the cross-feature affordances.
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
  vi.mocked(useAiStatus).mockReset();
  vi.mocked(useAiStatus).mockReturnValue({
    status: null,
    loading: false,
    error: null,
  } as never);
  vi.mocked(apiFetch).mockImplementation((async (url: string) => {
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods"))
      return [PERIOD_NEXT, PERIOD_CURRENT] as never;
    if (url.startsWith("/api/v1/budgets")) return [] as never;
    return null as never;
  }) as never);
});

describe("/budgets — From Forecast affordances (TBD-197 PR 2)", () => {
  it("hides the header From Forecast button when features.forecast is false, and the page itself still renders", async () => {
    setAuth(false);
    render(<BudgetsPage />);

    // The page is NOT the disabled notice — Budgets is on.
    expect(
      await screen.findByRole("heading", { name: "Budgets" }),
    ).toBeTruthy();
    expect(screen.queryByTestId("feature-disabled-notice")).toBeNull();

    // ⚠ Queried BY ROLE, not by text. "From Forecast" also appears as a
    // <strong> inside the empty-state prose (site 3 below), so a bare
    // `queryByText` matches two nodes and throws — a fence that fails for a
    // reason unrelated to the button it means to check.
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "From Forecast" }),
      ).toBeNull();
    });
  });

  it("control: renders the header From Forecast button when forecast is true", async () => {
    setAuth(true);
    render(<BudgetsPage />);
    expect(
      await screen.findByRole("button", { name: "From Forecast" }),
    ).toBeTruthy();
  });

  it("hides the current-period empty-state prose that names From Forecast", async () => {
    setAuth(false);
    render(<BudgetsPage />);
    await screen.findByRole("heading", { name: "Budgets" });

    await waitFor(() => {
      expect(screen.queryByText(/No budgets set/)).toBeTruthy();
    });
    // The prose still explains how to add one; it just stops advertising a
    // control that now 404s.
    const empty = screen.getByText(/No budgets set/);
    expect(empty.textContent).toMatch(/\+ Add Budget/);
    expect(empty.textContent).not.toMatch(/From Forecast/);
  });

  it("control: the empty-state prose names From Forecast when forecast is true", async () => {
    setAuth(true);
    render(<BudgetsPage />);
    await screen.findByRole("heading", { name: "Budgets" });
    await waitFor(() => {
      expect(screen.getByText(/No budgets set/).textContent).toMatch(
        /From Forecast/,
      );
    });
  });

  it("hides the next-period seed panel's From forecast button while keeping its siblings", async () => {
    setAuth(false);
    render(<BudgetsPage />);
    await screen.findByRole("heading", { name: "Budgets" });

    // Move the selection to the next period, where the seed panel lives.
    fireEvent.click(await screen.findByLabelText("Next period"));

    const panel = await screen.findByTestId("next-period-seed");
    expect(panel.textContent).not.toMatch(/From forecast/i);
    // Controls that do not read a ForecastPlan are untouched.
    expect(panel.textContent).toMatch(/Copy this period/);
    expect(panel.textContent).toMatch(/Start blank/);
  });

  it("control: the next-period seed panel keeps From forecast when forecast is true", async () => {
    setAuth(true);
    render(<BudgetsPage />);
    await screen.findByRole("heading", { name: "Budgets" });

    fireEvent.click(await screen.findByLabelText("Next period"));

    const panel = await screen.findByTestId("next-period-seed");
    expect(panel.textContent).toMatch(/From forecast/i);
  });
});
