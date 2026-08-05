/**
 * TBD-197 PR 1 — F15 / F13 / F16.
 *
 * F15 is the fence v1 and v2 both lacked: `baseNavItems` carries "/budgets"
 * UNCONDITIONALLY, and `buildNavItems` only ever filtered Reports and Plans.
 * An implementation that gates every backend route but never touches
 * `buildNavItems` passes the whole rest of the suite while shipping a nav
 * link straight to a 404.
 *
 * F16 pins the polarity split in DEFAULT_FEATURES: reports/plans/customDashboard
 * are opt-in (ship OFF), forecast/budgets are table stakes (ship ON). Three
 * hand-maintained copies of that literal existed before this ticket; the split
 * is exactly the kind of thing a later reader "corrects".
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/dashboard",
}));

vi.mock("@/components/AppShellAddTransactionCta", () => ({
  default: () => null,
  shouldShowAddTransactionCta: () => false,
}));
vi.mock("@/components/announcements/AnnouncementBar", () => ({ default: () => null }));
vi.mock("@/components/notifications/NotificationBell", () => ({ default: () => null }));
vi.mock("@/components/AppShellFooter", () => ({ default: () => null }));
vi.mock("@/components/brand/Logo", () => ({ Logo: () => null }));
vi.mock("@/components/ui/TrialBanner", () => ({ default: () => null }));
vi.mock("@/components/ui/ThemeToggle", () => ({ default: () => null }));
vi.mock("@/lib/keep-warm", () => ({ startKeepWarm: () => () => undefined }));
vi.mock("@/lib/help/tour", () => ({
  TOUR_FLAG_KEY: "tour",
  TOUR_FLAG_VALUE_EXTENDED: "extended",
}));

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";
import { DEFAULT_FEATURES } from "@/lib/features";

const BASE_USER = {
  id: 1,
  username: "member",
  email: "member@example.com",
  first_name: "Mem",
  last_name: "Ber",
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner" as const,
  org_id: 1,
  org_name: "Test Org",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  password_set: true,
  onboarded_at: "2026-01-01T00:00:00Z",
  allow_manual_balance_adjustment: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
  permissions: undefined,
};

const makeAuth = (features: Record<string, boolean>) => ({
  user: BASE_USER,
  loading: false,
  needsSetup: false,
  billingUiEnabled: false,
  features,
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  refreshMe: vi.fn(),
});

describe("AppShell — planning-tool nav gating (TBD-197)", () => {
  beforeEach(() => vi.clearAllMocks());

  it("F15: hides the Budgets nav item when features.budgets is false", () => {
    (useAuth as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
      makeAuth({ ...DEFAULT_FEATURES, budgets: false }),
    );
    render(
      <AppShell>
        <div />
      </AppShell>,
    );
    expect(screen.queryAllByText("Budgets")).toHaveLength(0);
    // Control within the same render: a sibling base item is untouched.
    expect(screen.queryAllByText("Accounts").length).toBeGreaterThan(0);
  });

  it("F13: renders the Budgets nav item when features.budgets is true", () => {
    (useAuth as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
      makeAuth({ ...DEFAULT_FEATURES, budgets: true }),
    );
    render(
      <AppShell>
        <div />
      </AppShell>,
    );
    expect(screen.queryAllByText("Budgets").length).toBeGreaterThan(0);
  });

  it("F16: DEFAULT_FEATURES keeps the opt-in / table-stakes polarity split", () => {
    expect(DEFAULT_FEATURES.forecast).toBe(true);
    expect(DEFAULT_FEATURES.budgets).toBe(true);
    expect(DEFAULT_FEATURES.reports).toBe(false);
    expect(DEFAULT_FEATURES.plans).toBe(false);
    expect(DEFAULT_FEATURES.customDashboard).toBe(false);
  });
});
