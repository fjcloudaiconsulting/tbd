import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SystemPlansPage from "@/app/system/plans/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/system/plans",
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

const SUPERADMIN = {
  id: 1, username: "root", email: "root@platform.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const, org_id: 1, org_name: "Platform",
  billing_cycle_day: 1, is_superadmin: true, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

const PRO_PLAN = {
  id: 1,
  name: "Pro",
  slug: "pro",
  description: "",
  is_custom: false,
  is_active: true,
  sort_order: 1,
  price_monthly: 10,
  price_yearly: 100,
  max_users: null,
  retention_days: null,
  features: {
    "ai.budget": true,
    "ai.forecast": false,
    "ai.smart_plan": false,
    "ai.autocategorize": false,
  },
  ai_budget_enabled: true,
  ai_forecast_enabled: false,
  ai_smart_plan_enabled: false,
};

describe("/system/plans page — Features section + Duplicate", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    useAuthMock.mockReturnValue({
      user: SUPERADMIN as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  });

  it("renders one Features row per catalog key in the editor", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/plans/all") return Promise.resolve([PRO_PLAN]);
      return Promise.resolve(undefined);
    }) as never);

    render(<SystemPlansPage />);

    // Wait for the row to render before opening the editor.
    const editButton = await screen.findByRole("button", { name: /^Edit$/i });
    fireEvent.click(editButton);

    // The four FEATURE_LABELS labels should each appear in the editor.
    await screen.findByText("AI Budget Rebalancing");
    expect(screen.getByText("AI Smart Forecast")).toBeInTheDocument();
    expect(screen.getByText("AI Goal-Based Plans")).toBeInTheDocument();
    expect(screen.getByText("AI Auto-Categorization")).toBeInTheDocument();
  });

  it("Duplicate row action opens the Duplicate plan modal", async () => {
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/plans/all") return Promise.resolve([PRO_PLAN]);
      return Promise.resolve(undefined);
    }) as never);

    render(<SystemPlansPage />);

    const duplicateButton = await screen.findByRole("button", { name: /^Duplicate$/i });
    fireEvent.click(duplicateButton);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: /Duplicate plan/i }),
      ).toBeInTheDocument();
    });
  });
});
