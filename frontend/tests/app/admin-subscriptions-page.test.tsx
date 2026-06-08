import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useRouter } from "next/navigation";

import AdminSubscriptionsPage from "@/app/admin/subscriptions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: vi.fn(),
  usePathname: () => "/admin/subscriptions",
  useSearchParams: () => new URLSearchParams(),
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


// One-stop response stubber. Branches on the URL the page fetches so
// tests can mock both the KPI strip and the list table without spelling
// out per-call wiring.
function setupApiFetchStubs(opts: {
  kpis?: object;
  list?: object;
  fail?: boolean;
}) {
  const defaultKpis = {
    total_subscriptions: 4,
    active: 2,
    trial: 1,
    past_due: 1,
    cancelled: 0,
    signups_last_7d: 2,
    trial_expiring_next_7d: 1,
    plan_distribution: [
      { plan_id: 1, plan_slug: "pro", plan_name: "Pro", subscription_count: 3 },
      { plan_id: 2, plan_slug: "free", plan_name: "Free", subscription_count: 1 },
    ],
    mock_mrr: "0.00",
    mock_arr: "0.00",
    mock_revenue: true,
    generated_at: "2026-05-13T12:00:00",
  };
  const defaultList = {
    items: [
      {
        subscription_id: 10,
        org_id: 100,
        org_name: "Acme Corp",
        plan_id: 1,
        plan_slug: "pro",
        plan_name: "Pro",
        status: "active",
        billing_interval: "monthly",
        trial_start: null,
        trial_end: null,
        current_period_start: "2026-05-01",
        current_period_end: "2026-06-01",
        created_at: "2026-04-15T08:00:00",
        updated_at: "2026-04-15T08:00:00",
      },
      {
        subscription_id: 11,
        org_id: 101,
        org_name: "Globex",
        plan_id: 2,
        plan_slug: "free",
        plan_name: "Free",
        status: "trialing",
        billing_interval: "monthly",
        trial_start: "2026-05-10",
        trial_end: "2026-05-24",
        current_period_start: null,
        current_period_end: null,
        created_at: "2026-05-10T08:00:00",
        updated_at: "2026-05-10T08:00:00",
      },
    ],
    total: 2,
    limit: 50,
    offset: 0,
  };
  const kpis = opts.kpis ?? defaultKpis;
  const list = opts.list ?? defaultList;
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (opts.fail) throw new Error("boom");
    if (url.includes("/kpis")) return kpis as never;
    return list as never;
  });
}


describe("/admin/subscriptions list page", () => {
  const replace = vi.fn();

  beforeEach(() => {
    replace.mockReset();
    vi.clearAllMocks();
    vi.mocked(useRouter).mockReturnValue({ replace } as never);
  });

  it("redirects users without subscriptions.view to /dashboard", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: false }),
      loading: false,
    } as never);
    setupApiFetchStubs({});

    render(<AdminSubscriptionsPage />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/dashboard"));
    expect(screen.queryByTestId("app-shell")).not.toBeInTheDocument();
    expect(vi.mocked(apiFetch)).not.toHaveBeenCalled();
  });

  it("redirects unauthenticated visitors to /login", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      loading: false,
    } as never);
    setupApiFetchStubs({});

    render(<AdminSubscriptionsPage />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(vi.mocked(apiFetch)).not.toHaveBeenCalled();
  });

  it("renders KPI strip with mock revenue labelling for superadmin", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    setupApiFetchStubs({});

    render(<AdminSubscriptionsPage />);

    expect(await screen.findByText("Subscriptions")).toBeInTheDocument();
    // KPI tiles render counts. Use findAllByText since the badge labels
    // are duplicated (status chip + KPI tile + status badge inside the
    // table all carry the same text). Just assert at least one match.
    await waitFor(() => {
      expect(screen.getAllByText("Active").length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText("Trial").length).toBeGreaterThan(0);
    // Mock labels appear for $$ tiles (MRR / ARR) — at least 2.
    const mockBadges = screen.getAllByText("mock");
    expect(mockBadges.length).toBeGreaterThanOrEqual(2);
    // The disclosure copy also tells admins this is not real revenue.
    expect(
      screen.getByText(/Revenue figures are mock/i),
    ).toBeInTheDocument();
  });

  it("renders the subscription table with org rows", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    setupApiFetchStubs({});

    render(<AdminSubscriptionsPage />);

    expect(await screen.findByText("Acme Corp")).toBeInTheDocument();
    expect(screen.getByText("Globex")).toBeInTheDocument();
    // Status badges render the textual status, not just the colour.
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("trialing")).toBeInTheDocument();
  });

  it("filters by status when an admin clicks a status chip", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    setupApiFetchStubs({});

    render(<AdminSubscriptionsPage />);

    await screen.findByText("Acme Corp");
    vi.mocked(apiFetch).mockClear();

    const trialChip = screen.getByRole("button", { name: "Trial" });
    fireEvent.click(trialChip);

    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.includes("status=trialing"))).toBe(true);
    });
  });

  it("renders an empty-state when no subscriptions match", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    setupApiFetchStubs({
      list: { items: [], total: 0, limit: 50, offset: 0 },
    });

    render(<AdminSubscriptionsPage />);

    expect(
      await screen.findByText(/No subscriptions match the current filters/i),
    ).toBeInTheDocument();
  });

  it("exposes a backend search box", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    setupApiFetchStubs({});

    render(<AdminSubscriptionsPage />);

    const search = await screen.findByRole("searchbox", {
      name: /search subscriptions/i,
    });
    expect(search).toBeInTheDocument();
  });
});
