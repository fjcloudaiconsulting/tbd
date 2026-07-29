/**
 * TBD-234 — the billing period roster must be REACHABLE.
 *
 * `/settings/organization/periods` shipped with no entry point anywhere in the
 * app, which meant the feature was deep-link-only. These fences hold the link
 * in the Billing period card and hold it shut for non-admins.
 *
 * ⚠ On the negative case, know what actually enforces it. The link is wrapped
 * in `{admin && ...}`, but that wrapper is belt-and-braces: the page-level
 * `if (loading || !user || !admin)` early return means a member never gets
 * past a spinner, so deleting the wrapper alone leaves this test GREEN. The
 * early return is the gate under test. Revert-and-confirm-red evidence is on
 * the branch commit for TBD-234.
 */

import { render, screen, waitFor } from "@testing-library/react";

import OrganizationSettingsPage from "@/app/settings/organization/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(),
    // SchedulerSettingsCard mounts on this page and calls these directly.
    // They wrap the module's own apiFetch internally, so the override above
    // does NOT intercept them; stub them so mounting the page stays offline.
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

const pushMock = vi.fn();
const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  usePathname: () => "/settings/organization",
}));

function makeUser(role: "owner" | "admin" | "member") {
  return {
    id: 1, username: "u", email: "u@x.io",
    first_name: null, last_name: null, phone: null, avatar_url: null,
    email_verified: true,
    role,
    org_id: 42, org_name: "Acme", billing_cycle_day: 1,
    is_superadmin: false, is_active: true, mfa_enabled: false,
    password_set: true,
    subscription_status: null, subscription_plan: null, trial_end: null,
  };
}

function baseFixtures() {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/settings/billing-cycle") {
      return Promise.resolve({ billing_cycle_day: 1 });
    }
    if (url === "/api/v1/settings/billing-period") {
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    }
    if (url === "/api/v1/settings") return Promise.resolve([]);
    if (typeof url === "string" && url.startsWith("/api/v1/orgs/members?")) {
      return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
    }
    if (typeof url === "string" && url.startsWith("/api/v1/orgs/invitations?")) {
      return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
    }
    if (url === "/api/v1/category-rules") return Promise.resolve([]);
    return Promise.resolve({});
  }) as never);
}

function mockUser(role: "owner" | "admin" | "member") {
  vi.mocked(useAuth).mockReturnValue({
    user: makeUser(role) as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn().mockResolvedValue(undefined),
  } as never);
}

describe("Organization settings: billing period roster entry point", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    pushMock.mockReset();
    replaceMock.mockReset();
    baseFixtures();
  });

  it("links an admin to the period roster from the Billing period card", async () => {
    mockUser("admin");
    render(<OrganizationSettingsPage />);

    const link = await screen.findByRole("link", { name: /period roster/i });
    expect(link).toHaveAttribute("href", "/settings/organization/periods");
  });

  it("does not show the roster link to a non-admin member", async () => {
    mockUser("member");
    const { container } = render(<OrganizationSettingsPage />);

    // Wait on something the member DOES reach, so the negative assertions
    // below cannot pass merely by racing an empty first paint: a member is
    // bounced to /settings by the page's own redirect effect.
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/settings"));

    expect(
      screen.queryByRole("link", { name: /period roster/i }),
    ).not.toBeInTheDocument();
    // Belt-and-braces on the href too, in case the label ever changes.
    expect(
      container.querySelector('a[href="/settings/organization/periods"]'),
    ).toBeNull();
  });
});
