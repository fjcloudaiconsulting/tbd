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
vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});
vi.mock("@/lib/hooks/use-ai-status", () => ({ useAiStatus: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/budgets",
  useSearchParams: () => ({ get: () => null }),
}));

const USER = {
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner" as const,
  org_id: 1, org_name: "Acme", billing_cycle_day: 1, is_superadmin: false,
  is_active: true, mfa_enabled: false, subscription_status: null,
  subscription_plan: null, trial_end: null, allow_manual_balance_adjustment: false,
};

// PERIOD_OPEN is the current period; NEXT_STUB is a future stub (start after
// today) representing "next period".
const PERIOD_OPEN = { id: 1, start_date: "2026-05-01", end_date: null };
const NEXT_STUB = { id: 2, start_date: "2999-01-01", end_date: "2999-01-31" };

const CURRENT_BUDGET = {
  id: 1, category_id: 10, category_name: "Groceries",
  amount: "500", spent: "200", percent_used: 40,
};

function setupAuth() {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never, loading: false, needsSetup: false,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  } as never);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(useAiStatus).mockReset();
  vi.mocked(useAiStatus).mockReturnValue({ budget: { entitled: false, configured: false } } as never);
  setupAuth();
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.includes("ensure-future")) return [] as never;
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) {
      return [PERIOD_OPEN, NEXT_STUB] as never;
    }
    if (url.includes("period_start=2999-01-01")) return [] as never; // next: empty
    if (url.startsWith("/api/v1/budgets")) return [CURRENT_BUDGET] as never;
    return null as never;
  });
});

it("offers the four seed actions in an empty next period", async () => {
  render(<BudgetsPage />);
  // Loads on the current period first.
  await waitFor(() => expect(screen.getByText("Groceries")).toBeInTheDocument());

  // Navigate to the next period (the › chevron moves toward newer periods).
  fireEvent.click(screen.getByLabelText("Next period"));

  // The empty next-period state shows all four seed actions.
  await waitFor(() =>
    expect(screen.getByTestId("next-period-seed")).toBeInTheDocument(),
  );
  expect(screen.getByRole("button", { name: /from forecast/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /copy this period/i })).toBeInTheDocument();
  expect(screen.getByTestId("ai-draft-btn")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /start blank/i })).toBeInTheDocument();
  // A future period is editable, so it is NOT flagged read-only.
  expect(screen.queryByText(/closed \(read-only\)/i)).toBeNull();
});
