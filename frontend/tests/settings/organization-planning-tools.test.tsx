/**
 * TBD-197 PR 1 — F14.
 *
 * The "Planning tools" card. Deliberately NOT called "Features": that word is
 * already taken on the admin surface by OrgFeatureGateCard / FeatureOverridesCard,
 * and conflating tenant preference with platform entitlement is the exact fault
 * line this ticket's first two designs fell across.
 *
 * ⚠ SCOPING: `getByRole("switch")` now matches seven or more nodes on this page
 * (SchedulerSettingsCard has three, SmartRulesSection one, and PR 2 adds a
 * second planning-tool switch). Never index positionally — TBD-313. Every
 * query below is scoped by container id: first to the card, then to the
 * per-tool ROW. The row scope became load-bearing in PR 2 — with two switches
 * the card carries two "Enabled"/"Disabled" spans, so a card-level
 * `getByText("Enabled")` is ambiguous and would throw.
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

function setAuth(budgets: boolean, forecast = true) {
  vi.mocked(useAuth).mockReturnValue({
    user: makeUser() as never,
    loading: false,
    needsSetup: false,
    features: {
      reports: false,
      plans: false,
      customDashboard: false,
      forecast,
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

/** Scope to ONE tool's row — never positional, never card-wide (TBD-313). */
async function toolRow(tool: "forecast" | "budgets") {
  const row = await screen.findByTestId(`planning-tool-${tool}`);
  return within(row);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(baseFixtures() as never);
});

describe("OrganizationSettingsPage — Planning tools card (TBD-197 F14)", () => {
  it("renders a labelled switch with aria-checked=true and visible Enabled text when Budgets is on", async () => {
    setAuth(true);
    render(<OrganizationSettingsPage />);

    const row = await toolRow("budgets");
    const sw = row.getByRole("switch", { name: /budgets/i });
    expect(sw.getAttribute("aria-checked")).toBe("true");
    expect(row.getByText("Enabled")).toBeTruthy();
  });

  it("renders aria-checked=false and visible Disabled text when Budgets is off", async () => {
    setAuth(false);
    render(<OrganizationSettingsPage />);

    const row = await toolRow("budgets");
    const sw = row.getByRole("switch", { name: /budgets/i });
    expect(sw.getAttribute("aria-checked")).toBe("false");
    expect(row.getByText("Disabled")).toBeTruthy();
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
    const row = await toolRow("budgets");
    const sw = row.getByRole("switch", { name: /budgets/i });
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
        row.getByRole("switch", { name: /budgets/i }).getAttribute("aria-checked"),
      ).toBe("false");
    });
    expect(row.getByText("Disabled")).toBeTruthy();
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
    const row = await toolRow("budgets");
    // Not on load — /auth/status returns one resolved boolean and cannot tell
    // "global off" from "org opted out".
    expect(row.queryByText(/set by your administrator/i)).toBeNull();

    fireEvent.click(row.getByRole("switch", { name: /budgets/i }));
    await waitFor(() => {
      expect(row.getByText(/set by your administrator/i)).toBeTruthy();
    });
  });

  // ── PR 2 — the second switch ───────────────────────────────────────────────

  it("F14b: renders a Forecast switch ALONGSIDE Budgets, each reflecting its own flag", async () => {
    setAuth(true, false);
    render(<OrganizationSettingsPage />);

    const card = await planningToolsCard();
    // Two switches on the card, not one. A build that shipped the backend gate
    // and forgot to widen `tools` leaves the org unable to undo its own opt-out
    // from the UI — the exact trap PR 1 narrowed the allow-list to avoid.
    expect(card.getAllByRole("switch")).toHaveLength(2);

    const forecast = await toolRow("forecast");
    expect(
      forecast.getByRole("switch", { name: /forecast/i }).getAttribute("aria-checked"),
    ).toBe("false");
    expect(forecast.getByText("Disabled")).toBeTruthy();

    // The sibling is independent, in the same render — so a build that read one
    // flag for both switches cannot pass.
    const budgets = await toolRow("budgets");
    expect(
      budgets.getByRole("switch", { name: /budgets/i }).getAttribute("aria-checked"),
    ).toBe("true");
    expect(budgets.getByText("Enabled")).toBeTruthy();
  });

  it("F14c: clicking the Forecast switch PUTs /settings/features/forecast and flips only that row", async () => {
    setAuth(true, true);
    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (
        init?.method === "PUT" &&
        url === "/api/v1/settings/features/forecast"
      ) {
        return Promise.resolve({ feature: "forecast", enabled: false });
      }
      return baseFixtures()(url, init);
    }) as never);

    render(<OrganizationSettingsPage />);
    const forecast = await toolRow("forecast");
    fireEvent.click(forecast.getByRole("switch", { name: /forecast/i }));

    await waitFor(() => {
      expect(
        vi
          .mocked(apiFetch)
          .mock.calls.some(
            ([url, init]) =>
              url === "/api/v1/settings/features/forecast" &&
              (init as RequestInit | undefined)?.method === "PUT" &&
              String((init as RequestInit).body).includes('"enabled":false'),
          ),
      ).toBe(true);
    });

    await waitFor(() => {
      expect(
        forecast
          .getByRole("switch", { name: /forecast/i })
          .getAttribute("aria-checked"),
      ).toBe("false");
    });
    // The Budgets row is untouched: the write echo is keyed per tool.
    const budgets = await toolRow("budgets");
    expect(
      budgets.getByRole("switch", { name: /budgets/i }).getAttribute("aria-checked"),
    ).toBe("true");
  });
});
