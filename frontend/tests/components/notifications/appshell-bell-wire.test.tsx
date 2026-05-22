import { act, render, screen } from "@testing-library/react";

// THIS file exercises the real NotificationBell inside AppShell to
// pin that the bell mounts in the header row when the user is
// authenticated. vitest.setup.ts globally stubs the bell to no-op so
// other AppShell tests don't trip on its /api/v1/notifications
// fetch; we unmock here.
vi.unmock("@/components/notifications/NotificationBell");

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

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(async () => ({ items: [], next_cursor: null })),
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

function mockAuth(user: Record<string, unknown> | null, loading = false) {
  vi.mocked(useAuth).mockReturnValue({
    user: user as never,
    loading,
    needsSetup: false,
    billingUiEnabled: true,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  });
}

describe("AppShell — notification bell wire (PR3 of notif train)", () => {
  beforeEach(() => {
    vi.mocked(useAuth).mockReset();
  });

  it("renders the bell when the user is authenticated", async () => {
    mockAuth(BASE_USER);
    await act(async () => {
      render(
        <AppShell>
          <p>page body</p>
        </AppShell>,
      );
    });
    expect(screen.getByTestId("notification-bell")).toBeInTheDocument();
  });

  it("does not render the bell when the user is anonymous", async () => {
    // loading=false + user=null is the "redirect to /login" branch
    // — AppShell renders the bare spinner / loading shell. The bell
    // must not appear because the entire authed chrome subtree is
    // gated on `user`.
    mockAuth(null);
    await act(async () => {
      render(
        <AppShell>
          <p>page body</p>
        </AppShell>,
      );
    });
    expect(screen.queryByTestId("notification-bell")).toBeNull();
  });

  it("does not collide with the existing Plans / Reports nav entries", async () => {
    mockAuth(BASE_USER);
    await act(async () => {
      render(
        <AppShell>
          <p>page body</p>
        </AppShell>,
      );
    });
    // Sidebar nav links remain present alongside the bell — pin
    // that the new icon doesn't displace existing routes.
    expect(screen.getByTestId("notification-bell")).toBeInTheDocument();
    // The sidebar nav uses Link elements; just check a couple of
    // known nav anchors are still in the document.
    expect(screen.getByRole("link", { name: /^docs$/i })).toBeInTheDocument();
  });
});
