import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { useParams, useRouter } from "next/navigation";

import AdminSubscriptionDetailPage from "@/app/admin/subscriptions/[id]/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: vi.fn(),
  useParams: vi.fn(),
}));

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
  return {
    ...actual,
    apiFetch: vi.fn(),
  };
});


function makeUser(opts: { isSuperadmin?: boolean; permissions?: string[] } = {}) {
  return {
    id: 1,
    username: "alice",
    email: "alice@example.com",
    first_name: "Alice",
    last_name: "Tester",
    phone: null,
    avatar_url: null,
    email_verified: true,
    role: "owner" as const,
    org_id: 1,
    org_name: "Test Org",
    billing_cycle_day: 1,
    is_superadmin: opts.isSuperadmin ?? false,
    is_active: true,
    mfa_enabled: false,
    subscription_status: null,
    subscription_plan: null,
    trial_end: null,
    permissions: opts.permissions,
  };
}


const SAMPLE_DETAIL = {
  subscription_id: 42,
  org: {
    id: 100,
    name: "Acme Corp",
    billing_cycle_day: 1,
    created_at: "2026-01-01T00:00:00",
    member_count: 5,
  },
  plan: {
    id: 1,
    slug: "pro",
    name: "Pro",
    description: "Pro plan",
    price_monthly: "9.99",
    price_yearly: "99.00",
    max_users: null,
    retention_days: null,
    features: {},
    is_custom: false,
    is_active: true,
  },
  status: "active" as const,
  billing_interval: "monthly" as const,
  trial_start: null,
  trial_end: null,
  current_period_start: "2026-05-01",
  current_period_end: "2026-06-01",
  created_at: "2026-01-15T08:00:00",
  updated_at: "2026-05-01T08:00:00",
  feature_overrides: [
    {
      feature_key: "ai.budget",
      value: true,
      set_at: "2026-04-01T08:00:00",
      expires_at: null,
      is_expired: false,
      note: "comped",
    },
    {
      feature_key: "ai.forecast",
      value: true,
      set_at: "2026-04-01T08:00:00",
      expires_at: "2026-04-10T08:00:00",
      is_expired: true,
      note: null,
    },
  ],
  mock_revenue_amount: "0.00",
  mock_revenue: true,
};


describe("/admin/subscriptions/[id] detail page", () => {
  const replace = vi.fn();

  beforeEach(() => {
    replace.mockReset();
    vi.clearAllMocks();
    vi.mocked(useRouter).mockReturnValue({ replace } as never);
    vi.mocked(useParams).mockReturnValue({ id: "42" });
  });

  it("redirects users without subscriptions.view to /dashboard", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: false }),
      loading: false,
    } as never);
    vi.mocked(apiFetch).mockResolvedValue(SAMPLE_DETAIL);

    render(<AdminSubscriptionDetailPage />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/dashboard"));
    expect(vi.mocked(apiFetch)).not.toHaveBeenCalled();
  });

  it("renders org / plan / subscription cards for superadmin", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    vi.mocked(apiFetch).mockResolvedValue(SAMPLE_DETAIL);

    render(<AdminSubscriptionDetailPage />);

    // Page title is the org name (matches the URL the admin clicked from).
    expect(await screen.findByRole("heading", { name: "Acme Corp" })).toBeInTheDocument();
    // Status renders.
    expect(screen.getByText("active")).toBeInTheDocument();
    // Plan column rendered.
    expect(screen.getByText("Pro")).toBeInTheDocument();
    // Revenue tile labelled mock.
    expect(screen.getByText("$0.00")).toBeInTheDocument();
    expect(screen.getAllByText("mock").length).toBeGreaterThanOrEqual(1);
  });

  it("links Override subscription back to the org page", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    vi.mocked(apiFetch).mockResolvedValue(SAMPLE_DETAIL);

    render(<AdminSubscriptionDetailPage />);

    const overrideLink = await screen.findByRole("link", {
      name: /Override subscription/i,
    });
    expect(overrideLink.getAttribute("href")).toBe("/admin/orgs/100");
  });

  it("renders feature overrides with expired badge", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    vi.mocked(apiFetch).mockResolvedValue(SAMPLE_DETAIL);

    render(<AdminSubscriptionDetailPage />);

    expect(await screen.findByText("ai.budget")).toBeInTheDocument();
    expect(screen.getByText("ai.forecast")).toBeInTheDocument();
    expect(screen.getByText("expired")).toBeInTheDocument();
  });

  it("renders an empty-state when no overrides exist", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    vi.mocked(apiFetch).mockResolvedValue({
      ...SAMPLE_DETAIL,
      feature_overrides: [],
    });

    render(<AdminSubscriptionDetailPage />);

    expect(await screen.findByText(/No overrides on this org/i)).toBeInTheDocument();
  });
});
