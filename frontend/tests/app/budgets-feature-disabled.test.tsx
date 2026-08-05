/**
 * TBD-197 PR 1 — G5.
 *
 * When the org has switched Budgets off, /budgets replaces its body with a
 * one-line notice instead of rendering a page whose every fetch now 404s.
 *
 * The notice carries NO btnPrimary: DESIGN.md's One Brass Rule reserves the
 * brass accent for the page's primary action, and a page whose message is
 * absence has no primary action. The admin affordance is a btnLink.
 */
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

function setupAuth(budgets: boolean, role: "owner" | "member" = "owner") {
  vi.mocked(useAuth).mockReturnValue({
    user: { ...USER, role } as never,
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
  vi.mocked(useAiStatus).mockReset();
  vi.mocked(useAiStatus).mockReturnValue({
    status: null,
    loading: false,
    error: null,
  } as never);
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods"))
      return [PERIOD_OPEN] as never;
    if (url.startsWith("/api/v1/budgets")) return [] as never;
    return null as never;
  });
});

describe("/budgets — feature-disabled notice (TBD-197 G5)", () => {
  it("renders the notice, names the org, and uses no brass primary button", async () => {
    setupAuth(false);
    const { container } = render(<BudgetsPage />);

    const notice = await screen.findByTestId("feature-disabled-notice");
    expect(notice.textContent).toMatch(/Budgets/);
    expect(notice.textContent).toMatch(/Acme/);
    expect(notice.textContent).toMatch(/Planning tools/i);

    // No brass: btnPrimary is the only style primitive carrying `bg-accent`.
    expect(container.querySelector('[class*="bg-accent"]')).toBeNull();

    // The real page body is gone, not merely overlaid.
    expect(screen.queryByText("+ Add Budget")).toBeNull();
    expect(screen.queryByText("From Forecast")).toBeNull();
  });

  it("control: renders the ordinary page when budgets is true", async () => {
    setupAuth(true);
    render(<BudgetsPage />);
    await waitFor(() => {
      expect(screen.queryByTestId("feature-disabled-notice")).toBeNull();
    });
    expect(screen.getByRole("heading", { name: "Budgets" })).toBeTruthy();
  });

  it("skips the budgets fetch entirely when the feature is off", async () => {
    setupAuth(false);
    render(<BudgetsPage />);
    await screen.findByTestId("feature-disabled-notice");
    const urls = vi
      .mocked(apiFetch)
      .mock.calls.map((c) => String(c[0]));
    expect(urls.filter((u) => u.startsWith("/api/v1/budgets"))).toEqual([]);
  });
});
