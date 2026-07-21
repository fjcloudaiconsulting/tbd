import React from "react";
import { render, screen, waitFor } from "@testing-library/react";

import AnnouncementsLayout from "@/components/AnnouncementsLayout";
import { useAuth } from "@/components/auth/AuthProvider";

const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/system/announcements",
}));

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(),
}));

const SUPERADMIN = {
  id: 1, username: "root", email: "root@platform.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const, org_id: 1, org_name: "Platform",
  billing_cycle_day: 1, is_superadmin: true, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

const NON_SUPERADMIN = { ...SUPERADMIN, id: 2, is_superadmin: false };

describe("AnnouncementsLayout", () => {
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    replaceMock.mockReset();
  });

  function mockAuth(user: unknown, loading = false) {
    useAuthMock.mockReturnValue({
      user,
      loading,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  }

  it("renders both tab labels for a superadmin", () => {
    mockAuth(SUPERADMIN);
    render(
      <AnnouncementsLayout activeTab="/system/announcements">
        <div>child content</div>
      </AnnouncementsLayout>,
    );
    expect(screen.getByText("In-app")).toBeInTheDocument();
    expect(screen.getByText("Email broadcasts")).toBeInTheDocument();
    expect(screen.getByText("child content")).toBeInTheDocument();
  });

  it("highlights the active tab", () => {
    mockAuth(SUPERADMIN);
    render(
      <AnnouncementsLayout activeTab="/system/announcements/broadcasts">
        <div>child</div>
      </AnnouncementsLayout>,
    );
    expect(screen.getByText("Email broadcasts").className).toContain("border-accent");
    expect(screen.getByText("Email broadcasts").className).toContain("text-accent");
    expect(screen.getByText("In-app").className).not.toContain("border-accent");
  });

  it("redirects a non-superadmin user to /dashboard", async () => {
    mockAuth(NON_SUPERADMIN);
    render(
      <AnnouncementsLayout activeTab="/system/announcements">
        <div>child</div>
      </AnnouncementsLayout>,
    );
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
    expect(screen.queryByText("child")).not.toBeInTheDocument();
  });

  it("redirects an unauthenticated visitor to /login", async () => {
    mockAuth(null);
    render(
      <AnnouncementsLayout activeTab="/system/announcements">
        <div>child</div>
      </AnnouncementsLayout>,
    );
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/login");
    });
    expect(screen.queryByText("child")).not.toBeInTheDocument();
  });

  it("shows a spinner while auth is loading, not the children", () => {
    mockAuth(null, true);
    render(
      <AnnouncementsLayout activeTab="/system/announcements">
        <div>child</div>
      </AnnouncementsLayout>,
    );
    expect(screen.queryByText("child")).not.toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });
});
