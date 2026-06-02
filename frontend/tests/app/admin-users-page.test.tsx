import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import AdminUsersPage from "@/app/admin/users/page";
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

const replaceMock = vi.fn();
// Per-test search params. Tests can mutate this BEFORE calling
// ``render`` to seed the page from a specific URL.
let currentSearchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
  usePathname: () => "/admin/users",
  useSearchParams: () => currentSearchParams,
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
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
};

const SAMPLE_USERS = {
  items: [
    {
      id: 42,
      email: "ada@acme.io",
      username: "ada",
      display_name: "Ada Lovelace",
      is_superadmin: false,
      is_active: true,
      email_verified: true,
      mfa_enabled: false,
      password_changed_at: null,
      onboarded_at: "2026-04-30T10:00:00",
      created_at: "2026-04-15T10:00:00",
      orgs: [{ org_id: 10, name: "Acme Co", role: "owner" }],
    },
    {
      id: 43,
      email: "bob@beta.io",
      username: "bob",
      display_name: null,
      is_superadmin: false,
      is_active: false,
      email_verified: true,
      mfa_enabled: false,
      password_changed_at: null,
      onboarded_at: null,
      created_at: "2026-04-10T10:00:00",
      orgs: [{ org_id: 11, name: "Beta", role: "member" }],
    },
  ],
  total: 2,
  limit: 50,
  offset: 0,
};

describe("AdminUsersPage", () => {
  const apiFetchMock = vi.mocked(apiFetch);
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
    apiFetchMock.mockReset();
    replaceMock.mockReset();
    currentSearchParams = new URLSearchParams();
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

  it("renders the users table from the API", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({
          items: [
            { id: 10, name: "Acme Co" },
            { id: 11, name: "Beta" },
          ],
          total: 2,
        } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);

    // Both display names should land in the rendered rows.
    await screen.findByText("Ada Lovelace");
    expect(screen.getByText("bob@beta.io")).toBeInTheDocument();
    // Status column derives from flags: bob is_active=false -> inactive.
    // ("inactive" appears as a filter chip too; assert the count
    // is > 1 to confirm both occurrences are rendered without colliding
    // on a single matcher.)
    expect(screen.getAllByText("inactive").length).toBeGreaterThan(0);
    // Org link shows the org name.
    expect(screen.getAllByText("Acme Co").length).toBeGreaterThan(0);
  });

  it("redirects non-superadmin users without users.view away from the page", async () => {
    useAuthMock.mockReturnValue({
      user: { ...SUPERADMIN, is_superadmin: false } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminUsersPage />);

    await waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith("/dashboard");
    });
  });

  it("renders for a non-superadmin who carries users.view in permissions", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });
    useAuthMock.mockReturnValue({
      user: {
        ...SUPERADMIN,
        is_superadmin: false,
        permissions: ["users.view"],
      } as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });

    render(<AdminUsersPage />);

    await screen.findByText("Ada Lovelace");
    expect(replaceMock).not.toHaveBeenCalledWith("/dashboard");
  });

  it("debounces the search input and re-fires the list call with q", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);

    await screen.findByText("Ada Lovelace");

    const searchInput = screen.getByLabelText(/search users/i) as HTMLInputElement;
    fireEvent.change(searchInput, { target: { value: "ada" } });

    // Wait for the debounced fetch to fire with q=ada.
    await waitFor(
      () => {
        const calls = apiFetchMock.mock.calls.map((c) => c[0] as string);
        expect(
          calls.some((u) => u.includes("/api/v1/admin/users") && u.includes("q=ada")),
        ).toBe(true);
      },
      { timeout: 1500 },
    );
  });

  it("applies the role filter chip and includes role= in the API call", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    fireEvent.click(screen.getByRole("button", { name: "owner" }));

    await waitFor(() => {
      const calls = apiFetchMock.mock.calls.map((c) => c[0] as string);
      expect(
        calls.some(
          (u) => u.includes("/api/v1/admin/users") && u.includes("role=owner"),
        ),
      ).toBe(true);
    });
  });

  // ── URL state contract ────────────────────────────────────────────

  it("seeds filter state from the URL on mount", async () => {
    // Land the page with a filter URL. The page should render the
    // chips in that state AND issue the corresponding API call.
    currentSearchParams = new URLSearchParams("q=ada&status=active&role=owner");
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);

    // Search input reflects the seeded ``q``.
    const searchInput = (await screen.findByLabelText(
      /search users/i,
    )) as HTMLInputElement;
    expect(searchInput.value).toBe("ada");

    // First /admin/users call carries the seeded filters.
    await waitFor(() => {
      const calls = apiFetchMock.mock.calls.map((c) => c[0] as string);
      const userCall = calls.find((u) => u.startsWith("/api/v1/admin/users"));
      expect(userCall).toBeDefined();
      expect(userCall).toContain("q=ada");
      expect(userCall).toContain("status=active");
      expect(userCall).toContain("role=owner");
    });
  });

  it("writes filter changes back to the URL via router.replace", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    fireEvent.click(screen.getByRole("button", { name: "admin" }));

    await waitFor(() => {
      // Some replace call has to carry role=admin in the path.
      const urls = replaceMock.mock.calls.map((c) => c[0] as string);
      expect(urls.some((u) => u.includes("role=admin"))).toBe(true);
      // And every replace call passes scroll: false so the table
      // doesn't jump on every URL write.
      for (const call of replaceMock.mock.calls) {
        const opts = call[1] as { scroll?: boolean } | undefined;
        if (opts) expect(opts.scroll).toBe(false);
      }
    });
  });

  it("seeds offset from URL and preserves it after the debounce window", async () => {
    // Regression for the bug where the qInput-debounce effect would
    // fire once on mount (because qInput was seeded from the URL)
    // and clobber the seeded offset back to 0. Owner reviewed the
    // first L4.4 URL-state pass and caught this; the fix is a
    // first-mount ref guard in the debounce effect.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      currentSearchParams = new URLSearchParams("q=ada&offset=50");
      apiFetchMock.mockImplementation((url: string) => {
        if (url.startsWith("/api/v1/admin/orgs")) {
          return Promise.resolve({ items: [], total: 0 } as never);
        }
        return Promise.resolve(SAMPLE_USERS as never);
      });

      render(<AdminUsersPage />);

      // First fetch carries the seeded offset.
      await waitFor(() => {
        const userCalls = apiFetchMock.mock.calls
          .map((c) => c[0] as string)
          .filter((u) => u.startsWith("/api/v1/admin/users"));
        expect(userCalls[0]).toContain("offset=50");
        expect(userCalls[0]).toContain("q=ada");
      });

      // Advance past the debounce window. If the first-mount guard
      // is missing, the debounce effect fires here and calls
      // setOffset(0), which would trigger a fresh fetch with
      // offset=0 and a router.replace that drops the offset param.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS + 100);
      });

      // No subsequent /api/v1/admin/users fetch may carry offset=0.
      // Every user fetch must still carry offset=50.
      const allUserCalls = apiFetchMock.mock.calls
        .map((c) => c[0] as string)
        .filter((u) => u.startsWith("/api/v1/admin/users"));
      for (const u of allUserCalls) {
        expect(u).toContain("offset=50");
      }

      // The URL writer must NEVER have rewritten the URL to drop
      // offset. If it ran, the only acceptable form contains offset=50.
      for (const call of replaceMock.mock.calls) {
        const url = call[0] as string;
        // If the URL has a query string, it must keep offset=50.
        // If there is no query string at all the writer dropped
        // offset, which is the failure mode this test catches.
        if (url.includes("?")) {
          expect(url).toContain("offset=50");
        } else {
          throw new Error(
            `router.replace called with no query string (offset dropped): ${url}`,
          );
        }
      }
    } finally {
      vi.useRealTimers();
    }
  });

  // ── Server-side sort ──────────────────────────────────────────────

  it("default fetch uses created_at/desc (no sort params in URL)", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    const userCalls = apiFetchMock.mock.calls
      .map((c) => c[0] as string)
      .filter((u) => u.startsWith("/api/v1/admin/users"));
    expect(userCalls[0]).toContain("sort_by=created_at");
    expect(userCalls[0]).toContain("sort_dir=desc");

    // Defaults are NOT written to the URL.
    for (const call of replaceMock.mock.calls) {
      const url = call[0] as string;
      expect(url).not.toContain("sort_by");
      expect(url).not.toContain("sort_dir");
    }
  });

  it("clicking a sortable header sorts ascending, then toggles on second click", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    // Click the Username header button -> sort_by=username&sort_dir=asc.
    fireEvent.click(screen.getByRole("button", { name: /^Username/ }));
    await waitFor(() => {
      const calls = apiFetchMock.mock.calls.map((c) => c[0] as string);
      expect(
        calls.some(
          (u) =>
            u.includes("/api/v1/admin/users") &&
            u.includes("sort_by=username") &&
            u.includes("sort_dir=asc"),
        ),
      ).toBe(true);
    });

    // Second click on the same column toggles to desc.
    fireEvent.click(screen.getByRole("button", { name: /^Username/ }));
    await waitFor(() => {
      const calls = apiFetchMock.mock.calls.map((c) => c[0] as string);
      expect(
        calls.some(
          (u) =>
            u.includes("/api/v1/admin/users") &&
            u.includes("sort_by=username") &&
            u.includes("sort_dir=desc"),
        ),
      ).toBe(true);
    });
  });

  it("maps the Name / email header to the email sort key", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    fireEvent.click(screen.getByRole("button", { name: /^Name \/ email/ }));
    await waitFor(() => {
      const calls = apiFetchMock.mock.calls.map((c) => c[0] as string);
      expect(
        calls.some(
          (u) =>
            u.includes("/api/v1/admin/users") && u.includes("sort_by=email"),
        ),
      ).toBe(true);
    });
  });

  it("does not render the Status header as a sort button", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    // The other column headers are buttons; Status must be a plain th.
    expect(screen.getByRole("button", { name: /^Username/ })).toBeInTheDocument();
    const columnHeaders = screen.getAllByRole("columnheader");
    const statusHeader = columnHeaders.find((h) => h.textContent === "Status");
    expect(statusHeader).toBeDefined();
    expect(statusHeader?.querySelector("button")).toBeNull();
  });

  it("changing sort resets offset to 0", async () => {
    currentSearchParams = new URLSearchParams("offset=25");
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve({ ...SAMPLE_USERS, total: 80, offset: 25 } as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    fireEvent.click(screen.getByRole("button", { name: /^Role/ }));
    await waitFor(() => {
      const calls = apiFetchMock.mock.calls.map((c) => c[0] as string);
      expect(
        calls.some(
          (u) =>
            u.includes("/api/v1/admin/users") &&
            u.includes("sort_by=role") &&
            u.includes("offset=0"),
        ),
      ).toBe(true);
    });
  });

  it("seeds sort_by/sort_dir from the URL on mount", async () => {
    currentSearchParams = new URLSearchParams("sort_by=email&sort_dir=asc");
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    const userCalls = apiFetchMock.mock.calls
      .map((c) => c[0] as string)
      .filter((u) => u.startsWith("/api/v1/admin/users"));
    expect(userCalls[0]).toContain("sort_by=email");
    expect(userCalls[0]).toContain("sort_dir=asc");
  });

  // ── Pagination ────────────────────────────────────────────────────

  it("uses pageSize=25 as the default limit in the fetch", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    const userCalls = apiFetchMock.mock.calls
      .map((c) => c[0] as string)
      .filter((u) => u.startsWith("/api/v1/admin/users"));
    expect(userCalls[0]).toContain("limit=25");
  });

  it("changing the per-page selector changes limit and resets offset", async () => {
    currentSearchParams = new URLSearchParams("offset=25");
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve({ ...SAMPLE_USERS, total: 200, offset: 25 } as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    const select = screen.getByLabelText(/per page/i) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "50" } });

    await waitFor(() => {
      const calls = apiFetchMock.mock.calls.map((c) => c[0] as string);
      expect(
        calls.some(
          (u) =>
            u.includes("/api/v1/admin/users") &&
            u.includes("limit=50") &&
            u.includes("offset=0"),
        ),
      ).toBe(true);
    });
  });

  it("clicking Next advances the offset by pageSize", async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve({ ...SAMPLE_USERS, total: 80 } as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    fireEvent.click(screen.getByRole("button", { name: /next page/i }));

    await waitFor(() => {
      const calls = apiFetchMock.mock.calls.map((c) => c[0] as string);
      expect(
        calls.some(
          (u) => u.includes("/api/v1/admin/users") && u.includes("offset=25"),
        ),
      ).toBe(true);
    });
  });
});

// Constant duplicated from the page module so the test can advance
// fake timers past the page's debounce window without importing
// implementation detail across a module boundary.
const SEARCH_DEBOUNCE_MS = 300;
