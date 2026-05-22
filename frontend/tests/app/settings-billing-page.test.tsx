import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { useRouter } from "next/navigation";

import BillingPage from "@/app/settings/billing/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { User } from "@/lib/types";

vi.mock("next/navigation", () => ({
  useRouter: vi.fn(),
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
  return {
    ...actual,
    apiFetch: vi.fn(),
  };
});

function ownerUser(): User {
  return {
    id: 1,
    username: "owner",
    email: "owner@example.com",
    first_name: "Olivia",
    last_name: "Owner",
    phone: null,
    avatar_url: null,
    email_verified: true,
    role: "owner",
    org_id: 1,
    org_name: "Test Org",
    billing_cycle_day: 1,
    is_superadmin: false,
    is_active: true,
    mfa_enabled: false,
    password_set: true,
    allow_manual_balance_adjustment: false,
    subscription_status: null,
    subscription_plan: null,
    trial_end: null,
  };
}

function mockAuth(billingUiEnabled: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: ownerUser(),
    loading: false,
    needsSetup: false,
    billingUiEnabled,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

describe("BillingPage — BILLING_UI_ENABLED kill switch", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    vi.mocked(useRouter).mockReturnValue({
      replace: vi.fn(),
      push: vi.fn(),
    } as never);
  });

  it("renders the architect-locked empty state and skips data fetch when flag is off", async () => {
    mockAuth(false);

    render(<BillingPage />);

    // Empty state copy — must match the architect-locked wording.
    await waitFor(() =>
      expect(
        screen.getByText(
          /Subscriptions are not available yet\. We will let you know when paid plans launch\./,
        ),
      ).toBeInTheDocument(),
    );

    // No call to /api/v1/subscriptions or /api/v1/plans. The owner
    // gating effect doesn't fire any apiFetch — the billing-flag
    // short-circuit returns before the fetch.
    expect(vi.mocked(apiFetch)).not.toHaveBeenCalled();
  });
});
