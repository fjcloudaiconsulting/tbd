import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import AdminRolesPage from "@/app/admin/roles/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual =
    await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
      "@/components/auth/AuthProvider",
    );
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const replaceMock = vi.fn();
let searchParamsValue = new URLSearchParams("");
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/admin/roles",
  useSearchParams: () => searchParamsValue,
}));

const SUPERADMIN = {
  id: 1,
  username: "root",
  email: "root@platform.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Platform",
  billing_cycle_day: 1,
  is_superadmin: true,
  is_active: true,
  mfa_enabled: false,
  password_set: true,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

const CATALOG = {
  namespaces: {
    admin: ["admin.view"],
    audit: ["audit.view"],
    orgs: ["orgs.manage", "orgs.view"],
    plans: ["plans.manage"],
    roles: ["roles.manage"],
  },
  keys: [
    "admin.view",
    "audit.view",
    "orgs.manage",
    "orgs.view",
    "plans.manage",
    "roles.manage",
  ],
};

describe("AdminRolesPage", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    replaceMock.mockReset();
    searchParamsValue = new URLSearchParams("");
    useAuthMock.mockReturnValue({
      user: SUPERADMIN as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  });

  it("renders the seeded superadmin role with system badge", async () => {
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/v1/admin/roles")) {
        return {
          items: [
            {
              id: 1,
              slug: "superadmin",
              name: "Superadmin",
              description: "Full platform access.",
              is_system_frozen: true,
              permission_count: 6,
              created_at: "2026-05-07T09:00:00",
              updated_at: "2026-05-07T09:00:00",
            },
          ],
          total: 1,
        };
      }
      if (path === "/api/v1/admin/permissions") return CATALOG;
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AdminRolesPage />);

    await screen.findByText("Superadmin");
    expect(screen.getByText("system")).toBeInTheDocument();
    expect(screen.getByText("6")).toBeInTheDocument();
  });

  it("fetches a default page with no sort_by and renders sortable headers", async () => {
    const calls: string[] = [];
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/v1/admin/roles")) {
        calls.push(path);
        return { items: [], total: 0 };
      }
      if (path === "/api/v1/admin/permissions") return CATALOG;
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AdminRolesPage />);

    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    // Default order sends limit/offset but no sort_by (backend frozen-first).
    expect(calls[0]).toContain("limit=25");
    expect(calls[0]).toContain("offset=0");
    expect(calls[0]).not.toContain("sort_by");
    // Column headers are clickable sort buttons.
    expect(
      screen.getByRole("button", { name: /name/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /permissions/i }),
    ).toBeInTheDocument();
  });

  it("sends sort_by/sort_dir when a column header is clicked", async () => {
    const calls: string[] = [];
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/v1/admin/roles")) {
        calls.push(path);
        return { items: [], total: 0 };
      }
      if (path === "/api/v1/admin/permissions") return CATALOG;
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AdminRolesPage />);

    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    fireEvent.click(screen.getByRole("button", { name: /^name/i }));

    await waitFor(() =>
      expect(calls.some((c) => c.includes("sort_by=name"))).toBe(true),
    );
    const sorted = calls.find((c) => c.includes("sort_by=name"))!;
    expect(sorted).toContain("sort_dir=asc");
  });

  it("seeds sort + page from the URL query string", async () => {
    searchParamsValue = new URLSearchParams(
      "sort_by=permission_count&sort_dir=desc&page_size=10",
    );
    const calls: string[] = [];
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/v1/admin/roles")) {
        calls.push(path);
        return { items: [], total: 0 };
      }
      if (path === "/api/v1/admin/permissions") return CATALOG;
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AdminRolesPage />);

    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    expect(calls[0]).toContain("sort_by=permission_count");
    expect(calls[0]).toContain("sort_dir=desc");
    expect(calls[0]).toContain("limit=10");
  });

  it("does not flash the empty row for an over-offset page that clamps to a populated page", async () => {
    // total > 0 but the seeded offset is past the end, so the first fetch
    // returns an empty items page. The clamp effect refetches the last valid
    // page (which has items). Gating the empty row on the CURRENT page's items
    // (parity with /admin/orgs) plus !fetching means the "No roles defined."
    // row never appears while data exists.
    searchParamsValue = new URLSearchParams("offset=9999&page_size=10");
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/v1/admin/roles")) {
        // Over-offset request -> empty page; clamped request -> populated page.
        if (path.includes("offset=9999")) {
          return { items: [], total: 3 };
        }
        return {
          items: [
            {
              id: 1,
              slug: "superadmin",
              name: "Superadmin",
              description: null,
              is_system_frozen: true,
              permission_count: 6,
              created_at: "2026-05-07T09:00:00",
              updated_at: "2026-05-07T09:00:00",
            },
          ],
          total: 3,
        };
      }
      if (path === "/api/v1/admin/permissions") return CATALOG;
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AdminRolesPage />);

    // The clamped page renders its real rows...
    await screen.findByText("Superadmin");
    // ...and the empty-state row never appears.
    expect(screen.queryByText("No roles defined.")).toBeNull();
  });

  it("redirects non-superadmin users without roles.manage away", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminRolesPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("renders for a non-superadmin who carries roles.manage in permissions", async () => {
    apiFetchMock.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/v1/admin/roles")) return { items: [], total: 0 };
      if (path === "/api/v1/admin/permissions") return CATALOG;
      throw new Error(`unexpected path: ${path}`);
    });
    useAuthMock.mockReturnValue({
      user: {
        ...SUPERADMIN,
        is_superadmin: false,
        permissions: ["roles.manage"],
      } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminRolesPage />);

    // Reaches the data-load path (so the "New role" button is rendered) and
    // does NOT redirect to /dashboard.
    await screen.findByRole("button", { name: /new role/i });
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });

  it("opens the create modal and submits a new role", async () => {
    apiFetchMock.mockImplementation(async (path: string, init?: RequestInit) => {
      if (
        path.startsWith("/api/v1/admin/roles") &&
        (!init || init.method === undefined)
      ) {
        return { items: [], total: 0 };
      }
      if (path === "/api/v1/admin/permissions") return CATALOG;
      if (path === "/api/v1/admin/roles" && init?.method === "POST") {
        const body = JSON.parse(init.body as string);
        expect(body.slug).toBe("support");
        expect(body.name).toBe("Support");
        expect(body.permissions).toEqual(["admin.view"]);
        return {
          id: 2,
          slug: "support",
          name: "Support",
          description: null,
          is_system_frozen: false,
          permissions: ["admin.view"],
          created_at: "2026-05-07T09:00:00",
          updated_at: "2026-05-07T09:00:00",
        };
      }
      throw new Error(`unexpected path: ${path}`);
    });

    render(<AdminRolesPage />);

    // Wait for the load to complete (button enables once catalog arrives).
    const newButton = await screen.findByRole("button", { name: /new role/i });
    await waitFor(() => expect(newButton).not.toBeDisabled());
    fireEvent.click(newButton);

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/slug/i), {
      target: { value: "support" },
    });
    fireEvent.change(within(dialog).getByLabelText(/^name$/i), {
      target: { value: "Support" },
    });
    // Toggle the admin.view permission checkbox.
    fireEvent.click(within(dialog).getByLabelText(/admin\.view/i));

    fireEvent.click(within(dialog).getByRole("button", { name: /create role/i }));

    await waitFor(() => {
      // Modal closed.
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});
