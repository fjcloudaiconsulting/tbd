/**
 * TBD-197 PR 1 — F14.
 *
 * The "Planning tools" card. Deliberately NOT called "Features": that word is
 * already taken on the admin surface by OrgFeatureGateCard / FeatureOverridesCard,
 * and conflating tenant preference with platform entitlement is the exact fault
 * line this ticket's first two designs fell across.
 *
 * ⚠ SCOPING: `getByRole("switch")` now matches six or more nodes on this page
 * (SchedulerSettingsCard has three, SmartRulesSection one). Never index
 * positionally — TBD-313. Every query below is scoped to the card container.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import OrganizationSettingsPage from "@/app/settings/organization/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(),
    getSchedulerSettings: vi.fn().mockResolvedValue({
      automate_recurring_generation: true,
      automate_billing_close: true,
      billing_close_reminder_lead_days: 3,
      automate_cc_statement_alerts: true,
      cc_statement_reminder_lead_days: 5,
    }),
    updateSchedulerSettings: vi.fn(),
  };
});

vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, mutate: vi.fn(() => Promise.resolve()) };
});

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
  usePathname: () => "/settings/organization",
}));

const ORG_ID = 42;

function makeUser() {
  return {
    id: 1,
    username: "u",
    email: "u@x.io",
    first_name: null,
    last_name: null,
    phone: null,
    avatar_url: null,
    email_verified: true,
    role: "admin" as const,
    org_id: ORG_ID,
    org_name: "Acme",
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

function setAuth(budgets: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: makeUser() as never,
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
    refreshMe: vi.fn().mockResolvedValue(undefined),
  } as never);
}

function baseFixtures() {
  return (url: string, _init?: RequestInit) => {
    if (url === "/api/v1/settings/billing-cycle")
      return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period")
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings") return Promise.resolve([]);
    if (url.startsWith("/api/v1/orgs/members?"))
      return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
    if (url.startsWith("/api/v1/orgs/invitations?"))
      return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
    if (url === "/api/v1/category-rules") return Promise.resolve([]);
    return Promise.resolve({});
  };
}

async function planningToolsCard() {
  const card = await screen.findByTestId("planning-tools-card");
  return within(card);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(baseFixtures() as never);
});

describe("OrganizationSettingsPage — Planning tools card (TBD-197 F14)", () => {
  it("renders a labelled switch with aria-checked=true and visible Enabled text when Budgets is on", async () => {
    setAuth(true);
    render(<OrganizationSettingsPage />);

    const card = await planningToolsCard();
    const sw = card.getByRole("switch", { name: /budgets/i });
    expect(sw.getAttribute("aria-checked")).toBe("true");
    expect(card.getByText("Enabled")).toBeTruthy();
  });

  it("renders aria-checked=false and visible Disabled text when Budgets is off", async () => {
    setAuth(false);
    render(<OrganizationSettingsPage />);

    const card = await planningToolsCard();
    const sw = card.getByRole("switch", { name: /budgets/i });
    expect(sw.getAttribute("aria-checked")).toBe("false");
    expect(card.getByText("Disabled")).toBeTruthy();
  });

  it("clicking the switch PUTs the toggle and flips the visible state, with no confirm dialog", async () => {
    setAuth(true);
    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (
        init?.method === "PUT" &&
        url === "/api/v1/settings/features/budgets"
      ) {
        return Promise.resolve({ feature: "budgets", enabled: false });
      }
      return baseFixtures()(url, init);
    }) as never);

    render(<OrganizationSettingsPage />);
    const card = await planningToolsCard();
    const sw = card.getByRole("switch", { name: /budgets/i });
    fireEvent.click(sw);

    await waitFor(() => {
      expect(
        vi
          .mocked(apiFetch)
          .mock.calls.some(
            ([url, init]) =>
              url === "/api/v1/settings/features/budgets" &&
              (init as RequestInit | undefined)?.method === "PUT" &&
              String((init as RequestInit).body).includes('"enabled":false'),
          ),
      ).toBe(true);
    });

    // Non-destructive change: immediate mutation, no ConfirmModal in the way.
    await waitFor(() => {
      expect(
        card.getByRole("switch", { name: /budgets/i }).getAttribute("aria-checked"),
      ).toBe("false");
    });
    expect(card.getByText("Disabled")).toBeTruthy();
  });

  it("shows the administrator badge only when a re-enable comes back still disabled", async () => {
    setAuth(false);
    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (init?.method === "PUT" && url === "/api/v1/settings/features/budgets") {
        // Global "off" wins: the write echo disagrees with the request.
        return Promise.resolve({ feature: "budgets", enabled: false });
      }
      return baseFixtures()(url, init);
    }) as never);

    render(<OrganizationSettingsPage />);
    const card = await planningToolsCard();
    // Not on load — /auth/status returns one resolved boolean and cannot tell
    // "global off" from "org opted out".
    expect(card.queryByText(/set by your administrator/i)).toBeNull();

    fireEvent.click(card.getByRole("switch", { name: /budgets/i }));
    await waitFor(() => {
      expect(card.getByText(/set by your administrator/i)).toBeTruthy();
    });
  });
});
