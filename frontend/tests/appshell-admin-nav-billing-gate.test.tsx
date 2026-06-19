import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

// Mock useAuth BEFORE importing AppShell.
vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/dashboard",
}));

// Stub out heavy sub-components so AppShell renders cleanly in jsdom.
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

const BASE_USER = {
  id: 1,
  username: "superadmin",
  email: "superadmin@example.com",
  first_name: "Super",
  last_name: "Admin",
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner" as const,
  org_id: 1,
  org_name: "Test Org",
  billing_cycle_day: 1,
  is_superadmin: true,
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

const makeSuperadminAuth = (billingUiEnabled: boolean) => ({
  user: BASE_USER,
  loading: false,
  needsSetup: false,
  billingUiEnabled,
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  refreshMe: vi.fn(),
});

describe("AppShell admin nav billing gate", () => {
  beforeEach(() => vi.clearAllMocks());

  it("hides Subscriptions and Plan Catalog when billingUiEnabled=false", () => {
    (useAuth as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
      makeSuperadminAuth(false),
    );
    render(<AppShell><div /></AppShell>);
    expect(screen.queryByText("Subscriptions")).toBeNull();
    expect(screen.queryByText("Plan Catalog")).toBeNull();
  });

  it("shows Subscriptions and Plan Catalog when billingUiEnabled=true", () => {
    (useAuth as unknown as ReturnType<typeof vi.fn>).mockReturnValue(
      makeSuperadminAuth(true),
    );
    render(<AppShell><div /></AppShell>);
    expect(screen.getByText("Subscriptions")).toBeInTheDocument();
    expect(screen.getByText("Plan Catalog")).toBeInTheDocument();
  });
});
