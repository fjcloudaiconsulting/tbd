import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SystemAnnouncementsPage from "@/app/system/announcements/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

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

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const SUPERADMIN = {
  id: 1, username: "root", email: "root@platform.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const, org_id: 1, org_name: "Platform",
  billing_cycle_day: 1, is_superadmin: true, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

const NON_SUPERADMIN = { ...SUPERADMIN, id: 2, is_superadmin: false };

const SAMPLE_ROW = {
  id: 7,
  title: "Maintenance window",
  body: "We are upgrading the database.",
  severity: "maintenance" as const,
  is_active: true,
  start_at: null,
  end_at: null,
  created_at: "2026-05-22T00:00:00",
  updated_at: "2026-05-22T00:00:00",
  created_by_user_id: 1,
};

describe("/system/announcements page", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    replaceMock.mockReset();
  });

  function setSuperadmin() {
    useAuthMock.mockReturnValue({
      user: SUPERADMIN as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  }

  it("redirects a non-superadmin user to /dashboard", async () => {
    useAuthMock.mockReturnValue({
      user: NON_SUPERADMIN as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    render(<SystemAnnouncementsPage />);
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("redirects an unauthenticated visitor to /login", async () => {
    useAuthMock.mockReturnValue({
      user: null,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    render(<SystemAnnouncementsPage />);
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/login");
    });
  });

  it("lists existing rows for a superadmin", async () => {
    setSuperadmin();
    apiFetchMock.mockImplementation(((url: string) => {
      if (url === "/api/v1/admin/announcements") return Promise.resolve([SAMPLE_ROW]);
      return Promise.resolve(undefined);
    }) as never);
    render(<SystemAnnouncementsPage />);
    await screen.findByText("Maintenance window");
    expect(screen.getByText(/Maintenance$/i)).toBeInTheDocument();
  });

  it("POSTs the form payload when creating a new announcement", async () => {
    setSuperadmin();
    apiFetchMock.mockImplementation(((url: string, options?: RequestInit) => {
      if (url === "/api/v1/admin/announcements" && !options?.method) {
        return Promise.resolve([]);
      }
      if (url === "/api/v1/admin/announcements" && options?.method === "POST") {
        return Promise.resolve(SAMPLE_ROW);
      }
      return Promise.resolve(undefined);
    }) as never);
    render(<SystemAnnouncementsPage />);
    await screen.findByTestId("announcement-empty");

    fireEvent.click(screen.getByTestId("announcement-new"));
    fireEvent.change(screen.getByTestId("announcement-form-title"), {
      target: { value: "New" },
    });
    fireEvent.change(screen.getByTestId("announcement-form-body"), {
      target: { value: "Body" },
    });
    fireEvent.change(screen.getByTestId("announcement-form-severity"), {
      target: { value: "promo" },
    });
    fireEvent.click(screen.getByTestId("announcement-form-submit"));

    await waitFor(() => {
      const postCall = apiFetchMock.mock.calls.find(
        (c) => c[0] === "/api/v1/admin/announcements" && c[1]?.method === "POST",
      );
      expect(postCall).toBeTruthy();
      const payload = JSON.parse(postCall![1]!.body as string);
      expect(payload.title).toBe("New");
      expect(payload.body).toBe("Body");
      expect(payload.severity).toBe("promo");
      expect(payload.is_active).toBe(true);
      expect(payload.start_at).toBeNull();
      expect(payload.end_at).toBeNull();
    });
  });

  it("pre-fills the form when editing", async () => {
    setSuperadmin();
    apiFetchMock.mockResolvedValueOnce([SAMPLE_ROW]);
    render(<SystemAnnouncementsPage />);
    const editBtn = await screen.findByTestId("announcement-edit");
    fireEvent.click(editBtn);
    const titleInput = screen.getByTestId(
      "announcement-form-title",
    ) as HTMLInputElement;
    const bodyInput = screen.getByTestId(
      "announcement-form-body",
    ) as HTMLTextAreaElement;
    expect(titleInput.value).toBe("Maintenance window");
    expect(bodyInput.value).toBe("We are upgrading the database.");
  });
});
