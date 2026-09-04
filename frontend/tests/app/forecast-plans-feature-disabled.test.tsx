/**
 * TBD-197 PR 2 — G5 (Forecast half).
 *
 * When the org has switched Forecast off, /forecast-plans replaces its body
 * with the one-line notice instead of rendering a page whose every fetch 404s.
 *
 * Mounted through the CLIENT ISLAND, which is the whole point: spec §9 claims
 * `app/forecast-plans/page.tsx` needs ZERO changes because `serverFetch`'s null
 * contract turns the gated 404 into `initialPlan = null` and the island mounts
 * normally. The first test below feeds exactly that shape —
 * `initialPlan={null}`, empty periods, empty categories — and asserts the
 * island still renders the notice rather than a spinner or a crash.
 *
 * The notice carries NO btnPrimary: docs/design/DESIGN.md's One Brass Rule reserves brass
 * for a page's primary action, and a page whose message is absence has none.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import ForecastPlansClient from "@/app/forecast-plans/ForecastPlansClient";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

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
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/forecast-plans",
  useSearchParams: () => ({ get: () => null }),
}));

const USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner" as const,
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
  allow_manual_balance_adjustment: false,
};

const PERIOD_OPEN = { id: 1, start_date: "2026-05-01", end_date: null };

function setAuth(forecast: boolean, role: "owner" | "member" = "owner") {
  vi.mocked(useAuth).mockReturnValue({
    user: { ...USER, role } as never,
    loading: false,
    needsSetup: false,
    features: {
      reports: false,
      plans: false,
      customDashboard: false,
      forecast,
      budgets: true,
    },
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation((async (url: string) => {
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods"))
      return [PERIOD_OPEN] as never;
    return null as never;
  }) as never);
});

describe("/forecast-plans — feature-disabled notice (TBD-197 G5)", () => {
  it("renders the notice from the RSC's gated shape (initialPlan null), names the org, and uses no brass", async () => {
    setAuth(false);
    const { container } = render(
      <ForecastPlansClient
        initialPeriods={[]}
        initialCategories={[]}
        initialPlan={null}
      />,
    );

    const notice = await screen.findByTestId("feature-disabled-notice");
    expect(notice.textContent).toMatch(/Forecast/);
    expect(notice.textContent).toMatch(/Acme/);
    expect(notice.textContent).toMatch(/Planning tools/i);

    // No brass: btnPrimary is the only style primitive carrying `bg-accent`.
    expect(container.querySelector('[class*="bg-accent"]')).toBeNull();

    // The real page body is gone, not merely overlaid.
    expect(screen.queryByRole("heading", { name: "Forecast Plans" })).toBeNull();
  });

  it("makes no /forecast-plans request when the feature is off", async () => {
    setAuth(false);
    render(
      <ForecastPlansClient
        initialPeriods={[PERIOD_OPEN]}
        initialCategories={[]}
        initialPlan={null}
      />,
    );
    await screen.findByTestId("feature-disabled-notice");

    const urls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
    expect(urls.filter((u) => u.startsWith("/api/v1/forecast-plans"))).toEqual(
      [],
    );
  });

  it("control: renders the ordinary page when forecast is true", async () => {
    setAuth(true);
    render(
      <ForecastPlansClient
        initialPeriods={[PERIOD_OPEN]}
        initialCategories={[]}
        initialPlan={null}
      />,
    );
    await waitFor(() => {
      expect(screen.queryByTestId("feature-disabled-notice")).toBeNull();
    });
    expect(
      screen.getByRole("heading", { name: "Forecast Plans" }),
    ).toBeTruthy();
  });
});
