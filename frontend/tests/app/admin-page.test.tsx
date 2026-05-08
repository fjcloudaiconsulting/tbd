import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { useRouter } from "next/navigation";

import AdminDashboardPage from "@/app/admin/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: vi.fn(),
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


describe("/admin page", () => {
  const replace = vi.fn();

  beforeEach(() => {
    replace.mockReset();
    vi.clearAllMocks();
    vi.mocked(useRouter).mockReturnValue({ replace } as never);
  });

  it("redirects users without admin.view without rendering the admin shell", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: false }),
      loading: false,
    } as never);

    render(<AdminDashboardPage />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/dashboard"));
    expect(screen.queryByTestId("app-shell")).not.toBeInTheDocument();
    expect(vi.mocked(apiFetch)).not.toHaveBeenCalled();
  });

  it("redirects unauthenticated visitors to /login (auth settled, user=null)", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: null,
      loading: false,
    } as never);

    render(<AdminDashboardPage />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
    expect(screen.queryByTestId("app-shell")).not.toBeInTheDocument();
    expect(vi.mocked(apiFetch)).not.toHaveBeenCalled();
  });

  it("loads and renders dashboard data for non-superadmins who carry admin.view", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: false, permissions: ["admin.view"] }),
      loading: false,
    } as never);
    vi.mocked(apiFetch).mockResolvedValue({
      kpis: {
        total_orgs: 2,
        total_users: 5,
        active_subscriptions: 1,
        signups_last_7d: 0,
      },
      health: {
        db: { ok: true, latency_ms: 1.2 },
        redis: { ok: true, latency_ms: 0.4 },
      },
    });

    render(<AdminDashboardPage />);

    expect(await screen.findByText("Admin")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/admin/dashboard");
  });

  it("loads and renders dashboard data for superadmins", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    vi.mocked(apiFetch).mockResolvedValue({
      kpis: {
        total_orgs: 7,
        total_users: 42,
        active_subscriptions: 11,
        signups_last_7d: 3,
      },
      health: {
        db: { ok: true, latency_ms: 1.9 },
        redis: { ok: false, error: "timeout" },
      },
    });

    render(<AdminDashboardPage />);

    expect(await screen.findByText("Admin")).toBeInTheDocument();
    // "Organizations" appears in both the KPI label and the quick-link
    // card — at least one match is enough to confirm the page rendered.
    expect(screen.getAllByText("Organizations").length).toBeGreaterThan(0);
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText("System health")).toBeInTheDocument();
    expect(screen.getByText("Audit log")).toBeInTheDocument();
    expect(screen.getByText("Database")).toBeInTheDocument();
    expect(screen.getByText("Redis")).toBeInTheDocument();
    expect(screen.getByText("timeout")).toBeInTheDocument();
    expect(apiFetch).toHaveBeenCalledWith("/api/v1/admin/dashboard");
  });

  it("surfaces fetch failures for superadmins", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    vi.mocked(apiFetch).mockRejectedValue(new Error("dashboard blew up"));

    render(<AdminDashboardPage />);

    expect(await screen.findByText("dashboard blew up")).toBeInTheDocument();
  });

  it("hides admin cards the user lacks permission to open", async () => {
    // User has admin.view (so the hub renders) plus orgs.view, but not
    // audit.view or roles.manage. Only the Organizations card should
    // appear among the quick-link section.
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({
        isSuperadmin: false,
        permissions: ["admin.view", "orgs.view"],
      }),
      loading: false,
    } as never);
    vi.mocked(apiFetch).mockResolvedValue({
      kpis: {
        total_orgs: 1,
        total_users: 1,
        active_subscriptions: 0,
        signups_last_7d: 0,
      },
      health: {
        db: { ok: true, latency_ms: 1 },
        redis: { ok: true, latency_ms: 1 },
      },
    });

    render(<AdminDashboardPage />);

    // Wait for dashboard to settle.
    await screen.findByText("Admin");

    // Find quick-link cards by their href (KPI labels and quick-link
    // card share text like "Organizations", so locate by anchor href).
    const allLinks = screen.getAllByRole("link");
    const orgsLink = allLinks.find((a) => a.getAttribute("href") === "/admin/orgs");
    const auditLink = allLinks.find((a) => a.getAttribute("href") === "/admin/audit");
    const rolesLink = allLinks.find((a) => a.getAttribute("href") === "/admin/roles");
    expect(orgsLink).toBeDefined();
    expect(auditLink).toBeUndefined();
    expect(rolesLink).toBeUndefined();
  });

  it("renders all admin cards for superadmin (hub-side gate covered)", async () => {
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser({ isSuperadmin: true }),
      loading: false,
    } as never);
    vi.mocked(apiFetch).mockResolvedValue({
      kpis: {
        total_orgs: 1,
        total_users: 1,
        active_subscriptions: 0,
        signups_last_7d: 0,
      },
      health: {
        db: { ok: true, latency_ms: 1 },
        redis: { ok: true, latency_ms: 1 },
      },
    });

    render(<AdminDashboardPage />);
    await screen.findByText("Admin");

    const allLinks = screen.getAllByRole("link");
    expect(allLinks.find((a) => a.getAttribute("href") === "/admin/orgs")).toBeDefined();
    expect(allLinks.find((a) => a.getAttribute("href") === "/admin/audit")).toBeDefined();
    expect(allLinks.find((a) => a.getAttribute("href") === "/admin/roles")).toBeDefined();
  });
});
