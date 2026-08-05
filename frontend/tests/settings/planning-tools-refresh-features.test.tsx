/**
 * TBD-197 PR 1 — F18. Review fold.
 *
 * A successful toggle must take effect in the SESSION THAT MADE IT.
 *
 * `AuthProvider` resolves `features` exactly three times — boot unauthenticated,
 * boot authenticated, and `login()` — and never unmounts on a client-side
 * navigation. `PlanningToolsCard` used to update only its own local state, so
 * the admin who switched Budgets off kept `budgets: true` in context for the
 * rest of the session: the nav entry survived and linked to a page that 404s
 * into an error banner rather than the notice, and on an org with
 * `customDashboard:false` the legacy dashboard's `/api/v1/budgets` fetch — part
 * of a `Promise.all` — rejected and painted "Failed to load dashboard data"
 * over a deliberate setting. That is precisely what the G6 fetch skip exists to
 * prevent, reached through the happy path.
 *
 * This is the only test in the suite that runs the REAL `AuthProvider` against
 * the REAL `AppShell`. Everything else mocks `useAuth`, which is exactly why
 * this defect survived: with the context hand-fed, the missing write to it is
 * unobservable.
 *
 * Mutant killed: deleting the `await refreshFeatures?.()` from `handleToggle`
 * (nav entry stays), and a `refreshFeatures` that re-fetches without calling
 * `setFeatures` (same).
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/settings/organization",
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
import { AuthProvider } from "@/components/auth/AuthProvider";
import PlanningToolsCard from "@/components/settings/PlanningToolsCard";
import { apiFetch } from "@/lib/api";

const USER = {
  id: 1,
  username: "admin",
  email: "admin@example.com",
  first_name: "Ad",
  last_name: "Min",
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "admin" as const,
  org_id: 7,
  org_name: "Acme",
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

/** Server-side truth, mutated by the PUT exactly as the backend would. */
let budgetsOnServer = true;
let statusCalls = 0;

function wireApi() {
  vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
    if (url === "/api/v1/auth/status") {
      statusCalls += 1;
      return Promise.resolve({
        needs_setup: false,
        billing_ui_enabled: false,
        features: {
          reports: false,
          plans: false,
          custom_dashboard: false,
          forecast: true,
          budgets: budgetsOnServer,
        },
      });
    }
    if (url === "/api/v1/auth/refresh")
      return Promise.resolve({ access_token: "tok", token_type: "bearer" });
    if (url === "/api/v1/auth/me") return Promise.resolve(USER);
    if (
      url === "/api/v1/settings/features/budgets" &&
      init?.method === "PUT"
    ) {
      budgetsOnServer = false;
      return Promise.resolve({ feature: "budgets", enabled: false });
    }
    return Promise.resolve({});
  }) as never);
}

beforeEach(() => {
  budgetsOnServer = true;
  statusCalls = 0;
  vi.mocked(apiFetch).mockReset();
  wireApi();
});

describe("Planning tools toggle propagates into the live session (TBD-197 F18)", () => {
  it("re-resolves /auth/status after a successful disable and drops the nav entry without a remount", async () => {
    render(
      <AuthProvider>
        <AppShell>
          <PlanningToolsCard tools={["budgets"]} />
        </AppShell>
      </AuthProvider>,
    );

    // Boot has resolved: the nav entry is present because the server says on.
    const navLink = await screen.findByRole("link", { name: /budgets/i });
    expect(navLink.getAttribute("href")).toBe("/budgets");

    const card = within(await screen.findByTestId("planning-tools-card"));
    const sw = card.getByRole("switch", { name: /budgets/i });
    expect(sw.getAttribute("aria-checked")).toBe("true");

    const statusCallsBeforeToggle = statusCalls;
    fireEvent.click(sw);

    // 1. The write went out...
    await waitFor(() => {
      expect(
        vi
          .mocked(apiFetch)
          .mock.calls.some(
            ([url, init]) =>
              url === "/api/v1/settings/features/budgets" &&
              (init as RequestInit | undefined)?.method === "PUT",
          ),
      ).toBe(true);
    });
    // 2. ...and was followed by a fresh /auth/status.
    await waitFor(() => expect(statusCalls).toBeGreaterThan(statusCallsBeforeToggle));

    // 3. The nav entry is gone — same mounted AuthProvider, no reload.
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: /budgets/i })).toBeNull();
    });

    // Control: a sibling nav entry is untouched, so this is the feature filter
    // and not the whole shell having unmounted.
    expect(screen.getByRole("link", { name: /accounts/i })).toBeTruthy();
    // ...and the switch itself still reflects the change.
    expect(
      within(screen.getByTestId("planning-tools-card"))
        .getByRole("switch", { name: /budgets/i })
        .getAttribute("aria-checked"),
    ).toBe("false");
  });
});
