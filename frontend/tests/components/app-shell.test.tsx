import { act, render, screen } from "@testing-library/react";

import AppShell from "@/components/AppShell";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
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

// AppShellAddTransactionCta loads accounts/categories on mount; stub the
// fetch so these system-nav-focused tests don't trip the act() warning
// when the CTA's loadRefs settles after assertions complete.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(async () => [] as never),
  };
});

const BASE_USER = {
  id: 1,
  username: "alice",
  email: "alice@example.com",
  first_name: "Alice",
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

async function renderShell() {
  // The AppShell-level CTA fires apiFetch in a useEffect. Wrap render
  // in act() so the resulting state updates flush before assertions,
  // skipping this trips the React act() warning in the existing
  // synchronous tests.
  await act(async () => {
    render(
      <AppShell>
        <p>page body</p>
      </AppShell>,
    );
  });
}

describe("AppShell — system nav gating", () => {
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    useAuthMock.mockReset();
  });

  it("hides the System nav for a regular user without admin.view", async () => {
    useAuthMock.mockReturnValue({
      user: BASE_USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.queryByText(/^System$/)).toBeNull();
    // Sidebar nav still shows Dashboard for the user.
    expect(screen.getAllByText("Dashboard").length).toBeGreaterThan(0);
  });

  it("shows the System nav for a superadmin (short-circuit)", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, is_superadmin: true } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    expect(screen.getByText("Admin")).toBeInTheDocument();
    expect(screen.getByText("Organizations")).toBeInTheDocument();
    expect(screen.getByText("Audit log")).toBeInTheDocument();
  });

  it("shows the System nav for a non-superadmin who carries admin.view in permissions", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["admin.view"] } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    expect(screen.getByText("Admin")).toBeInTheDocument();
    // admin.view alone does NOT grant the more specific destinations.
    expect(screen.queryByText("Organizations")).toBeNull();
    expect(screen.queryByText("Audit log")).toBeNull();
    expect(screen.queryByText("Plans")).toBeNull();
  });

  it("shows only the Audit log link for a non-superadmin with audit.view alone", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["audit.view"] } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    expect(screen.getByText("Audit log")).toBeInTheDocument();
    expect(screen.queryByText("Admin")).toBeNull();
    expect(screen.queryByText("Organizations")).toBeNull();
    expect(screen.queryByText("Plans")).toBeNull();
  });

  it("shows only the Organizations link for a non-superadmin with orgs.view alone", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["orgs.view"] } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    expect(screen.getByText("Organizations")).toBeInTheDocument();
    expect(screen.queryByText("Admin")).toBeNull();
    expect(screen.queryByText("Audit log")).toBeNull();
    expect(screen.queryByText("Plans")).toBeNull();
  });

  it("shows only the Plans link for a non-superadmin with plans.manage alone", async () => {
    useAuthMock.mockReturnValue({
      user: { ...BASE_USER, permissions: ["plans.manage"] } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    await renderShell();

    expect(screen.getByText(/^System$/)).toBeInTheDocument();
    expect(screen.getByText("Plans")).toBeInTheDocument();
    expect(screen.queryByText("Admin")).toBeNull();
    expect(screen.queryByText("Organizations")).toBeNull();
    expect(screen.queryByText("Audit log")).toBeNull();
  });
});
