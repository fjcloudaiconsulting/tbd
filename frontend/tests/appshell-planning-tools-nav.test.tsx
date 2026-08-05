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
import { DEFAULT_FEATURES, parseFeatures } from "@/lib/features";

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

  // F15b / F13b (PR 2). `buildNavItems` already carried the `forecast === false`
  // filter from PR 1, but PR 1 shipped no fence for it: with the slug absent
  // from the writable allow-list, `features.forecast` could never BE false in
  // production, so the branch was unreachable. PR 2 makes it reachable, which
  // is what makes it worth pinning — in both polarities.
  it("F15b: hides the Forecast Plans nav item when features.forecast is false", () => {
    (useAuth as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
      makeAuth({ ...DEFAULT_FEATURES, forecast: false }),
    );
    render(
      <AppShell>
        <div />
      </AppShell>,
    );
    expect(screen.queryAllByText("Forecast Plans")).toHaveLength(0);
    // Controls within the same render: the sibling planning tool and an
    // ordinary base item both survive, so this is not a blanket hide.
    expect(screen.queryAllByText("Budgets").length).toBeGreaterThan(0);
    expect(screen.queryAllByText("Accounts").length).toBeGreaterThan(0);
  });

  it("F13b: renders the Forecast Plans nav item when features.forecast is true", () => {
    (useAuth as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
      makeAuth({ ...DEFAULT_FEATURES, forecast: true }),
    );
    render(
      <AppShell>
        <div />
      </AppShell>,
    );
    expect(screen.queryAllByText("Forecast Plans").length).toBeGreaterThan(0);
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

  // F16b — the wire → flags parser. AuthProvider hand-rolled this block three
  // times (boot unauthenticated, boot authenticated, `login()`); a mutant that
  // flattened the polarity in the `login()` copy alone survived the entire
  // suite, so every interactive sign-in would lose Budgets and Forecast while
  // both boot paths kept the tests green. It is now one function, and this is
  // its fence.
  it("F16b: parseFeatures applies the polarity split to an /auth/status payload", () => {
    // Absent keys = an API revision predating these flags, mid-deploy.
    expect(parseFeatures(undefined)).toEqual(DEFAULT_FEATURES);
    expect(parseFeatures({})).toEqual(DEFAULT_FEATURES);
    // Only an EXPLICIT false closes a table-stakes surface.
    const off = parseFeatures({ forecast: false, budgets: false });
    expect(off.forecast).toBe(false);
    expect(off.budgets).toBe(false);
    // ...while the opt-in flags are plain truthiness, and snake_case maps over.
    const on = parseFeatures({ reports: true, custom_dashboard: true });
    expect(on.reports).toBe(true);
    expect(on.customDashboard).toBe(true);
    expect(on.plans).toBe(false);
  });
});
