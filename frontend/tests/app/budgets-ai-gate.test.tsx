import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
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

const PERIOD_OPEN = { id: 1, start_date: "2026-05-01", end_date: null };

const BUDGET = {
  id: 1,
  category_id: 10,
  category_name: "Groceries",
  amount: "500",
  spent: "200",
  percent_used: 40,
};

function setupAuth(role: string = "owner") {
  vi.mocked(useAuth).mockReturnValue({
    user: { ...USER, role } as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

function setupApiFetch() {
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD_OPEN] as never;
    if (url.startsWith("/api/v1/budgets")) return [BUDGET] as never;
    return null as never;
  });
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(useAiStatus).mockReset();
  setupAuth();
});

describe("Budgets page — AI rebalance button 3-state gating", () => {
  it("not entitled: renders neither the rebalance button nor Set up AI", async () => {
    vi.mocked(useAiStatus).mockReturnValue({
      categorize: { entitled: false, configured: false },
      forecast: { entitled: false, configured: false },
      budget: { entitled: false, configured: false },
    });
    setupApiFetch();

    render(<BudgetsPage />);

    await waitFor(() => {
      expect(screen.queryByTestId("suggest-rebalance-btn")).toBeNull();
    });
    expect(screen.queryByText(/set up ai/i)).toBeNull();
  });

  it("entitled but not configured: renders Set up AI, no rebalance button", async () => {
    vi.mocked(useAiStatus).mockReturnValue({
      categorize: { entitled: false, configured: false },
      forecast: { entitled: false, configured: false },
      budget: { entitled: true, configured: false },
    });
    setupApiFetch();

    render(<BudgetsPage />);

    await waitFor(() => {
      expect(screen.getByText(/set up ai/i)).toBeInTheDocument();
    });
    expect(screen.queryByTestId("suggest-rebalance-btn")).toBeNull();
  });

  it("entitled and configured: renders the rebalance button, no Set up AI", async () => {
    vi.mocked(useAiStatus).mockReturnValue({
      categorize: { entitled: false, configured: false },
      forecast: { entitled: false, configured: false },
      budget: { entitled: true, configured: true },
    });
    setupApiFetch();

    render(<BudgetsPage />);

    await waitFor(() => {
      expect(screen.getByTestId("suggest-rebalance-btn")).toBeInTheDocument();
    });
    expect(screen.queryByText(/set up ai/i)).toBeNull();
  });
});
