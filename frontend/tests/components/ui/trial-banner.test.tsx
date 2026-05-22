import React from "react";
import { render, screen } from "@testing-library/react";

import TrialBanner from "@/components/ui/TrialBanner";
import { useAuth } from "@/components/auth/AuthProvider";
import type { User } from "@/lib/types";

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return {
    ...actual,
    useAuth: vi.fn(),
  };
});

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 1,
    username: "alice",
    email: "alice@example.com",
    first_name: "Alice",
    last_name: "Tester",
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
    subscription_status: "trialing",
    subscription_plan: "pro",
    trial_end: "2099-12-31",
    ...overrides,
  };
}

function mockAuth(billingUiEnabled: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: null,
    loading: false,
    needsSetup: false,
    billingUiEnabled,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

describe("TrialBanner — BILLING_UI_ENABLED kill switch", () => {
  it("renders nothing when billingUiEnabled=false even for a trialing user", () => {
    mockAuth(false);
    const { container } = render(<TrialBanner user={makeUser()} />);
    // Hidden state must produce zero DOM output — gate sits before
    // every other branch.
    expect(container.firstChild).toBeNull();
  });

  it("renders the Pro Trial chip when billingUiEnabled=true", () => {
    mockAuth(true);
    render(<TrialBanner user={makeUser()} />);
    expect(screen.getByText(/Pro Trial/i)).toBeInTheDocument();
  });
});
