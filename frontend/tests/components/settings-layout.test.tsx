import React from "react";
import { render, screen } from "@testing-library/react";

import SettingsLayout from "@/components/SettingsLayout";
import { useAuth } from "@/components/auth/AuthProvider";
import type { User } from "@/lib/types";

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

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

describe("SettingsLayout — BILLING_UI_ENABLED filters Billing tab", () => {
  it("hides the Billing tab from owners when billingUiEnabled=false", () => {
    mockAuth(false);
    render(
      <SettingsLayout activeTab="/settings">
        <div>child</div>
      </SettingsLayout>,
    );
    // Profile / Security / Organization stay; Billing must be gone.
    expect(screen.getByText("Profile")).toBeInTheDocument();
    expect(screen.getByText("Organization")).toBeInTheDocument();
    expect(screen.queryByText("Billing")).not.toBeInTheDocument();
  });

  it("shows the Billing tab to owners when billingUiEnabled=true", () => {
    mockAuth(true);
    render(
      <SettingsLayout activeTab="/settings">
        <div>child</div>
      </SettingsLayout>,
    );
    expect(screen.getByText("Billing")).toBeInTheDocument();
  });
});
