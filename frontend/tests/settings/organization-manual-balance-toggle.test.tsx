import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import OrganizationSettingsPage from "@/app/settings/organization/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, mutate: vi.fn(() => Promise.resolve()) };
});

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
  usePathname: () => "/settings/organization",
}));

const ORG_ID = 42;

function makeUser(allow: boolean) {
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
    allow_manual_balance_adjustment: allow,
    subscription_status: null,
    subscription_plan: null,
    trial_end: null,
  };
}

function baseFixtures() {
  return (url: string, _init?: RequestInit) => {
    if (url === "/api/v1/settings/billing-cycle")
      return Promise.resolve({ billing_cycle_day: 1 });
    if (url === "/api/v1/settings/billing-period")
      return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
    if (url === "/api/v1/settings") return Promise.resolve([]);
    if (typeof url === "string" && url.startsWith("/api/v1/orgs/members?")) return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
    if (typeof url === "string" && url.startsWith("/api/v1/orgs/invitations?")) return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 });
    if (url === "/api/v1/category-rules") return Promise.resolve([]);
    return Promise.resolve({});
  };
}

describe("OrganizationSettingsPage — manual balance adjustment toggle (Track E)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
  });

  it("toggle ON: confirm dialog → PUT enabled=true → success message", async () => {
    const refreshMe = vi.fn().mockResolvedValue(undefined);
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser(false) as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe,
    } as never);

    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (
        init?.method === "PUT" &&
        url === "/api/v1/settings/manual-balance-adjustment"
      ) {
        return Promise.resolve({ enabled: true });
      }
      return baseFixtures()(url, init);
    }) as never);

    render(<OrganizationSettingsPage />);

    const enableBtn = await screen.findByRole("button", {
      name: /Enable manual balance adjustment/i,
    });
    fireEvent.click(enableBtn);

    // Confirmation modal renders with the warning text.
    const confirmBtn = await screen.findByRole("button", { name: /^Enable$/i });
    expect(
      screen.getByText(/Admins will be able to set account balances directly/i)
    ).toBeTruthy();
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      const call = vi.mocked(apiFetch).mock.calls.find(
        ([url, opts]) =>
          url === "/api/v1/settings/manual-balance-adjustment" &&
          (opts as RequestInit | undefined)?.method === "PUT"
      );
      expect(call).toBeTruthy();
      const body = JSON.parse(String((call![1] as RequestInit).body));
      expect(body).toEqual({ enabled: true });
    });

    await waitFor(() => expect(refreshMe).toHaveBeenCalled());
    await waitFor(() => {
      expect(
        screen.getByText(/Manual balance adjustment enabled/i)
      ).toBeInTheDocument();
    });
  });

  it("toggle OFF: confirm dialog shows the off-state warning copy", async () => {
    const refreshMe = vi.fn().mockResolvedValue(undefined);
    vi.mocked(useAuth).mockReturnValue({
      user: makeUser(true) as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe,
    } as never);

    vi.mocked(apiFetch).mockImplementation(baseFixtures());

    render(<OrganizationSettingsPage />);

    const disableBtn = await screen.findByRole("button", {
      name: /Disable manual balance adjustment/i,
    });
    fireEvent.click(disableBtn);

    expect(
      await screen.findByText(
        /Admins will no longer be able to set account balances directly/i
      )
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Disable$/i })
    ).toBeTruthy();
  });
});
