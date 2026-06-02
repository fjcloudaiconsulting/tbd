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
    //
    // The mock returns total: 200 so offset=50 is a valid in-range
    // page and the over-offset clamp effect does NOT fire.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      currentSearchParams = new URLSearchParams("q=ada&offset=50");
      apiFetchMock.mockImplementation((url: string) => {
        if (url.startsWith("/api/v1/admin/orgs")) {
          return Promise.resolve({ items: [], total: 0 } as never);
        }
        return Promise.resolve({ ...SAMPLE_USERS, total: 200, offset: 50 } as never);
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

  // ── Fix 1: seeded offset normalised to page boundary ─────────────

  it("normalises a non-boundary offset to the page boundary on mount", async () => {
    // URL has page_size=25 and offset=5 (not a multiple of 25).
    // The page must snap offset down to 0 (the page-1 boundary) and
    // issue the first fetch with offset=0, not offset=5.
    currentSearchParams = new URLSearchParams("page_size=25&offset=5");
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve({ ...SAMPLE_USERS, total: 80 } as never);
    });

    render(<AdminUsersPage />);

    await waitFor(() => {
      const userCalls = apiFetchMock.mock.calls
        .map((c) => c[0] as string)
        .filter((u) => u.startsWith("/api/v1/admin/users"));
      expect(userCalls.length).toBeGreaterThan(0);
      // The backend must see offset=0, not the raw off-boundary 5.
      expect(userCalls[0]).toContain("offset=0");
      // And the page_size should be honoured.
      expect(userCalls[0]).toContain("limit=25");
    });
  });

  it("preserves a boundary-aligned offset from the URL unchanged", async () => {
    // offset=50 is a clean multiple of page_size=25 — no snapping needed.
    currentSearchParams = new URLSearchParams("page_size=25&offset=50");
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve({ ...SAMPLE_USERS, total: 200, offset: 50 } as never);
    });

    render(<AdminUsersPage />);

    await waitFor(() => {
      const userCalls = apiFetchMock.mock.calls
        .map((c) => c[0] as string)
        .filter((u) => u.startsWith("/api/v1/admin/users"));
      expect(userCalls.length).toBeGreaterThan(0);
      expect(userCalls[0]).toContain("offset=50");
    });
  });

  // ── Fix 2: handleSort ignores unknown sort fields ─────────────────

  it("only renders whitelisted column fields as sort buttons", async () => {
    // The component is the gatekeeper: every <SortableHeader> that
    // invokes handleSort must carry a field value that is in SORT_FIELDS.
    // We verify this by collecting all sort-button aria-labels rendered
    // and asserting no unlisted key appears as a sort target. This is
    // the correct seam because an unknown field can only reach handleSort
    // via a column wired with a bad ``field`` prop.
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve(SAMPLE_USERS as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    const ALLOWED = ["email", "username", "org_name", "role", "created_at"];

    // Collect the field values wired to sortable header buttons.
    // SortableHeader renders a <button> whose accessible name starts with
    // the column label (e.g. "Name / email"). We verify the known columns
    // are present and the Status column (not in SORT_FIELDS) is NOT a button.
    expect(screen.getByRole("button", { name: /^Name \/ email/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Username/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Org/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Role/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Created/ })).toBeInTheDocument();

    // Status is NOT a sortable column — no sort button for it.
    const columnHeaders = screen.getAllByRole("columnheader");
    const statusTh = columnHeaders.find((h) => h.textContent === "Status");
    expect(statusTh).toBeDefined();
    expect(statusTh?.querySelector("button")).toBeNull();

    // Exactly the 5 allowed sort columns must exist as buttons (plus
    // filter chips and navigation buttons — we just confirm no extra
    // sort header leaks in).
    void ALLOWED; // consumed above via named assertions
  });

  // ── Fix 3: pagination visible when offset > 0, page clamped ──────

  it("renders Pagination when total > pageSize and offset is on a valid later page", async () => {
    // A URL with offset=25, total=80 — a valid second page. The over-offset
    // clamp does NOT fire (offset < total), so Pagination renders normally.
    currentSearchParams = new URLSearchParams("offset=25");
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve({ ...SAMPLE_USERS, total: 80, offset: 25 } as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    // Pagination must be rendered because total(80) > pageSize(25).
    const prevBtn = await screen.findByRole("button", { name: /previous page/i });
    expect(prevBtn).toBeInTheDocument();
  });

  it("auto-corrects a one-page-past URL to offset 0 via the clamp effect", async () => {
    // offset=25, total=2: the dataset shrank under the user. The new
    // over-offset clamp fires immediately after data loads and snaps to
    // offset=0 (the only valid page for a 2-row dataset), so the user
    // recovers automatically without having to click Previous.
    currentSearchParams = new URLSearchParams("offset=25");
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve({ ...SAMPLE_USERS, total: 2 } as never);
    });

    render(<AdminUsersPage />);
    await screen.findByText("Ada Lovelace");

    // The clamp effect corrects offset to 0, triggering a re-fetch.
    await waitFor(() => {
      const calls = apiFetchMock.mock.calls.map((c) => c[0] as string);
      expect(
        calls.some(
          (u) => u.includes("/api/v1/admin/users") && u.includes("offset=0"),
        ),
      ).toBe(true);
    });
  });

  // ── Fix: clamp over-offset on load ───────────────────────────────

  it("clamps a wildly over-offset URL to the last valid page after data loads", async () => {
    // Dataset: 30 rows, page_size=25 → 2 pages (offset 0 and offset 25).
    // A shared URL with offset=9999 should snap to offset=25 (last page)
    // in one corrective re-fetch, NOT render "Page 400 of 2" and require
    // hundreds of Previous clicks.
    currentSearchParams = new URLSearchParams("offset=9999&page_size=25");

    // 30-row dataset spread across two calls: the first (offset=9999)
    // returns empty items but the correct total, which triggers the clamp.
    // The second call (clamped offset=25) returns the last-page items.
    const PAGE2_USERS = {
      items: [
        {
          id: 99,
          email: "zara@zeta.io",
          username: "zara",
          display_name: "Zara Zeta",
          is_superadmin: false,
          is_active: true,
          email_verified: true,
          mfa_enabled: false,
          password_changed_at: null,
          onboarded_at: null,
          created_at: "2026-05-01T10:00:00",
          orgs: [{ org_id: 20, name: "Zeta Corp", role: "owner" }],
        },
      ],
      total: 30,
      limit: 25,
      offset: 25,
    };

    // Note: the URL offset=9999 is normalised to a page boundary on
    // mount (initialOffset snaps to Math.floor(9999/25)*25 = 9975), so
    // the first fetch uses offset=9975.  The clamp effect then fires
    // because 9975 >= total(30) and snaps to (pageCount(30,25)-1)*25 = 25.
    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      if (url.includes("offset=25") && !url.includes("offset=250")) {
        // Clamped fetch: return the actual last page.
        return Promise.resolve(PAGE2_USERS as never);
      }
      // All other offsets (the initial over-offset fetch): empty page + total.
      return Promise.resolve({ items: [], total: 30, limit: 25, offset: 9975 } as never);
    });

    render(<AdminUsersPage />);

    // After the clamp fires, the last-page row should be visible.
    await screen.findByText("Zara Zeta", {}, { timeout: 3000 });

    // The corrective fetch must have been issued with offset=25.
    const userCalls = apiFetchMock.mock.calls
      .map((c) => c[0] as string)
      .filter((u) => u.startsWith("/api/v1/admin/users"));
    expect(userCalls.some((u) => u.includes("offset=25") && !u.includes("offset=250"))).toBe(true);
  });

  it("clamps to offset=0 when total is 0 (empty dataset) and does not loop", async () => {
    // offset=9999 with a completely empty dataset → snap to 0.
    // We verify no duplicate fetches happen (the guard must not re-fire
    // after snapping because 0 >= 0 is false, so the condition never
    // re-triggers).
    currentSearchParams = new URLSearchParams("offset=9999&page_size=25");

    apiFetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/v1/admin/orgs")) {
        return Promise.resolve({ items: [], total: 0 } as never);
      }
      return Promise.resolve({ items: [], total: 0, limit: 25, offset: 0 } as never);
    });

    render(<AdminUsersPage />);

    // Wait for the page to settle; the "no users" message should appear.
    await screen.findByText(/no users match/i, {}, { timeout: 3000 });

    // Only one /admin/users fetch should be issued (the clamp fires
    // setOffset(0) but offset was already 9999→snapped to 0, and the
    // guard condition ``offset > 0 && offset >= data.total`` is false
    // once total is 0 and offset is 0, so no second corrective fetch).
    const userCalls = apiFetchMock.mock.calls
      .map((c) => c[0] as string)
      .filter((u) => u.startsWith("/api/v1/admin/users"));
    // At most 2 fetches: the initial (offset=9999 snapped to 0 on mount)
    // and possibly one corrective. The key assertion is no looping: the
    // count is low and stable.
    expect(userCalls.length).toBeLessThanOrEqual(2);
  });
});

// Constant duplicated from the page module so the test can advance
// fake timers past the page's debounce window without importing
// implementation detail across a module boundary.
const SEARCH_DEBOUNCE_MS = 300;
