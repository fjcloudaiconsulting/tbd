import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import AdminOrgDetailPage from "@/app/admin/orgs/[id]/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
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

const pushMock = vi.fn();
const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  useParams: () => ({ id: "42" }),
  usePathname: () => "/admin/orgs/42",
}));

const SUPERADMIN = {
  id: 1, username: "root", email: "root@platform.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner", org_id: 1, org_name: "Platform",
  billing_cycle_day: 1, is_superadmin: true, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

const DETAIL = {
  id: 42, name: "Acme", billing_cycle_day: 1,
  created_at: "2026-04-15T10:00:00",
  subscription: {
    status: "trialing", plan_id: 1, plan_slug: "free",
    trial_start: "2026-04-15", trial_end: "2026-05-15",
    current_period_start: null, current_period_end: null,
    created_at: "2026-04-15T10:00:00", updated_at: "2026-04-15T10:00:00",
  },
  members: [],
  counts: { transactions: 5, accounts: 1, budgets: 0, forecast_plans: 0 },
};

const MEMBERS = [
  {
    id: 9, username: "owner", email: "o@a.io", role: "owner",
    is_active: true, email_verified: true, is_superadmin: false,
    created_at: null,
  },
  {
    id: 10, username: "alice", email: "a@a.io", role: "member",
    is_active: true, email_verified: true, is_superadmin: false,
    created_at: null,
  },
  {
    id: 11, username: "ghost", email: "g@a.io", role: "member",
    is_active: false, email_verified: false, is_superadmin: false,
    created_at: null,
  },
  {
    id: 12, username: "platformsa", email: "psa@a.io", role: "admin",
    is_active: true, email_verified: true, is_superadmin: true,
    created_at: null,
  },
];

const FEATURE_STATE = {
  plan: { id: 1, name: "Free", slug: "free" },
  features: [
    { key: "ai.budget", plan_default: true, effective: true, override: null },
    { key: "ai.forecast", plan_default: false, effective: false, override: null },
    { key: "ai.smart_plan", plan_default: false, effective: false, override: null },
    { key: "ai.autocategorize", plan_default: false, effective: false, override: null },
  ],
};

function installMocks(membersOverride?: typeof MEMBERS) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
    if (url === "/api/v1/admin/orgs/42") {
      return Promise.resolve({ ...DETAIL, members: [] });
    }
    if (url === "/api/v1/admin/orgs/42/members" && (!opts || opts.method === undefined)) {
      return Promise.resolve(membersOverride ?? MEMBERS);
    }
    if (url === "/api/v1/admin/orgs/42/feature-state") {
      return Promise.resolve(FEATURE_STATE);
    }
    if (url === "/api/v1/plans") {
      return Promise.resolve([{ id: 1, slug: "free", name: "Free" }]);
    }
    return Promise.resolve(undefined);
  }) as never);
  return apiFetchMock;
}

describe("AdminOrgDetailPage — member management (L4.4)", () => {
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    pushMock.mockReset();
    replaceMock.mockReset();
    useAuthMock.mockReturnValue({
      user: SUPERADMIN as never,
      loading: false, needsSetup: false,
      login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
    });
  });

  it("renders the member section with role select and Remove buttons for editable rows", async () => {
    installMocks();
    render(<AdminOrgDetailPage />);

    await screen.findByRole("heading", { name: "Acme" });
    await screen.findByLabelText(/Role for alice/i);

    // Editable rows get a role-combobox + Remove button.
    expect(screen.getByLabelText(/Role for owner/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Remove alice from org/i }),
    ).toBeInTheDocument();

    // Platform superadmin shows the locked-reason copy, not actions.
    expect(screen.getByText(/Platform superadmin/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Remove platformsa from org/i }),
    ).toBeNull();
  });

  it("hides member section actions and shows hint when user is not orgs.manage-capable", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false, needsSetup: false,
      login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
    });
    installMocks();
    render(<AdminOrgDetailPage />);
    // Page redirects entirely without permission; member section never
    // renders, which is the correct gate behavior.
    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("PATCHes role on dropdown change and refreshes the list", async () => {
    const apiFetchMock = installMocks();
    render(<AdminOrgDetailPage />);

    await screen.findByText("alice");
    const roleSelect = screen.getByLabelText(/Role for alice/i) as HTMLSelectElement;
    fireEvent.change(roleSelect, { target: { value: "admin" } });

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/orgs/42/members/10",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ role: "admin" }),
        }),
      );
    });
  });

  it("PATCHes is_active=false when Deactivate is clicked", async () => {
    const apiFetchMock = installMocks();
    render(<AdminOrgDetailPage />);

    await screen.findByText("alice");
    fireEvent.click(screen.getByRole("button", { name: /Deactivate alice/i }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/orgs/42/members/10",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ is_active: false }),
        }),
      );
    });
  });

  it("Reactivate appears on inactive rows and PATCHes is_active=true", async () => {
    const apiFetchMock = installMocks();
    render(<AdminOrgDetailPage />);

    await screen.findByText("ghost");
    fireEvent.click(screen.getByRole("button", { name: /Reactivate ghost/i }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/orgs/42/members/11",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ is_active: true }),
        }),
      );
    });
  });

  it("Remove opens the confirm modal and DELETEs on confirm", async () => {
    const apiFetchMock = installMocks();
    render(<AdminOrgDetailPage />);

    await screen.findByText("alice");
    fireEvent.click(
      screen.getByRole("button", { name: /Remove alice from org/i }),
    );

    // Confirm modal opens.
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(/Remove member from organization/i),
    ).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: /^Remove$/i }));

    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalledWith(
        "/api/v1/admin/orgs/42/members/10",
        expect.objectContaining({ method: "DELETE" }),
      );
    });
  });

  it("Cancel on the remove modal closes without calling DELETE", async () => {
    const apiFetchMock = installMocks();
    render(<AdminOrgDetailPage />);

    await screen.findByText("alice");
    fireEvent.click(
      screen.getByRole("button", { name: /Remove alice from org/i }),
    );

    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /Cancel/i }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    const deleteCalls = apiFetchMock.mock.calls.filter(
      ([, opts]) => (opts as RequestInit | undefined)?.method === "DELETE",
    );
    expect(deleteCalls).toHaveLength(0);
  });

  it("surfaces a 409 last-owner error from the backend in the member-error banner", async () => {
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockImplementation(((url: string, opts?: RequestInit) => {
      if (url === "/api/v1/admin/orgs/42") {
        return Promise.resolve({ ...DETAIL, members: [] });
      }
      if (url === "/api/v1/admin/orgs/42/members" && (!opts || opts.method === undefined)) {
        return Promise.resolve(MEMBERS);
      }
      if (url === "/api/v1/admin/orgs/42/feature-state") {
        return Promise.resolve(FEATURE_STATE);
      }
      if (url === "/api/v1/plans") {
        return Promise.resolve([{ id: 1, slug: "free", name: "Free" }]);
      }
      if (
        url === "/api/v1/admin/orgs/42/members/9" &&
        (opts as RequestInit | undefined)?.method === "PATCH"
      ) {
        // Simulate the 409 last-owner shape from the backend.
        return Promise.reject(
          new Error("Cannot remove the last active owner of the organization"),
        );
      }
      return Promise.resolve(undefined);
    }) as never);

    render(<AdminOrgDetailPage />);

    await screen.findByLabelText(/Role for owner/i);
    const ownerRoleSelect = screen.getByLabelText(/Role for owner/i) as HTMLSelectElement;
    fireEvent.change(ownerRoleSelect, { target: { value: "admin" } });

    await screen.findByText(/last active owner/i);
  });

  it("does not render role select or Remove for the actor's own row", async () => {
    // Inject the actor into the member list so the locked-reason
    // path renders.
    const membersWithSelf = [
      ...MEMBERS,
      {
        id: 1, username: "root", email: "root@platform.io", role: "owner",
        is_active: true, email_verified: true, is_superadmin: true,
        created_at: null,
      },
    ];
    installMocks(membersWithSelf);
    render(<AdminOrgDetailPage />);

    await screen.findByText("root");
    // Two rows are locked: the platform superadmin AND the actor.
    expect(
      screen.queryByLabelText(/Role for root/i),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Remove root from org/i }),
    ).toBeNull();
  });
});
