import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import DashboardPage from "@/app/dashboard/page";
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

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/dashboard",
}));

const USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
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

// One transaction in the visible list, settled — used as the click target
// for the status toggle. Its description is the button's aria-label suffix.
const SETTLED_TX = {
  id: 999,
  account_id: 10,
  amount: "50.00",
  type: "expense",
  status: "settled",
  date: "2026-05-01",
  description: "Coffee",
  category_id: null,
  category_name: null,
  account_name: "Amex Primary",
  currency: "EUR",
  linked_transaction_id: null,
  is_imported: false,
  settled_date: "2026-05-01",
};

describe("DashboardPage — pending refetch on status toggle (L3.4)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    window.history.pushState({}, "", "/dashboard");
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
  });

  it("fires the all-time pending fetch after a status toggle on page > 0", async () => {
    let pendingCalls = 0;
    let toggleCallSeen = false;

    // Page 0: 11 rows so hasMore is true and the Next button is enabled.
    // Each row has a unique id so React keys are stable.
    const PAGE_0_ROWS = Array.from({ length: 11 }, (_, i) => ({
      ...SETTLED_TX,
      id: 200 + i,
      description: `Row ${i}`,
    }));
    // Page 1: SETTLED_TX (description "Coffee"). The aria-label match
    // below targets exactly this row, so the toggle test is unambiguous
    // about which page it's on.
    const PAGE_1_ROWS = [SETTLED_TX];

    vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
      if (url === "/api/v1/accounts") return Promise.resolve([]);
      if (url === "/api/v1/categories") return Promise.resolve([]);
      if (url === "/api/v1/budgets" || url.startsWith("/api/v1/budgets?"))
        return Promise.resolve([]);
      if (url === "/api/v1/settings/billing-cycle")
        return Promise.resolve({ billing_cycle_day: 1 });
      if (url === "/api/v1/settings/billing-period")
        return Promise.resolve({ id: 1, start_date: "2026-05-01", end_date: null });
      if (url === "/api/v1/settings/billing-periods")
        return Promise.resolve([{ id: 1, start_date: "2026-05-01", end_date: null }]);
      if (url.startsWith("/api/v1/forecast-plans/current")) return Promise.resolve(null);
      if (url.startsWith("/api/v1/forecast?period_start=")) return Promise.resolve(null);
      // PUT /api/v1/transactions/{id} — the status toggle write.
      if (url.startsWith("/api/v1/transactions/") && init?.method === "PUT") {
        toggleCallSeen = true;
        return Promise.resolve({});
      }
      // fetchAll paginator hits ?status=pending&limit=200&offset=N. Count
      // these calls and return [] so fetchAll exits on the first page.
      if (url.startsWith("/api/v1/transactions?status=pending")) {
        pendingCalls += 1;
        return Promise.resolve([]);
      }
      // limit=200 is the period-scoped "all" fetch (donut, charts, etc.).
      // limit=11 is the paginated visible-page fetch (PAGE_SIZE+1).
      if (url.startsWith("/api/v1/transactions?limit=200"))
        return Promise.resolve([...PAGE_0_ROWS, ...PAGE_1_ROWS]);
      if (url.startsWith("/api/v1/transactions?limit=11&offset=0"))
        return Promise.resolve(PAGE_0_ROWS);
      if (url.startsWith("/api/v1/transactions?limit=11&offset=10"))
        return Promise.resolve(PAGE_1_ROWS);
      return Promise.resolve({});
    }) as never);

    render(<DashboardPage />);

    // Wait for the initial page-0 render. The Next button is enabled
    // (hasMore=true because PAGE_0_ROWS.length = 11 > PAGE_SIZE).
    await waitFor(
      () => expect(screen.getByRole("button", { name: /^Next$/ })).not.toBeDisabled(),
      { timeout: 3000 },
    );

    // Navigate to page 1 (page index 1 = "page > 0", the regression case).
    fireEvent.click(screen.getByRole("button", { name: /^Next$/ }));

    // Wait for page-1 render. SETTLED_TX (Coffee) is now visible; its
    // status-toggle button has the destination-aware aria-label set in
    // PR #132.
    await waitFor(
      () => expect(screen.getByLabelText(/Settled status for Coffee/)).toBeInTheDocument(),
      { timeout: 3000 },
    );

    const pendingCallsBeforeToggle = pendingCalls;
    expect(pendingCallsBeforeToggle).toBeGreaterThan(0);

    // Toggle the status while the user is on page 1. Pre-fix, this
    // only called loadTransactions(page=1), which never refetches
    // pending — strip totals would silently go stale. The fix wires
    // loadPendingTransactions() into the toggle handler regardless
    // of the visible page index.
    fireEvent.click(screen.getByLabelText(/Settled status for Coffee/));

    await waitFor(() => expect(toggleCallSeen).toBe(true));
    // Critical assertion: pending endpoint was re-called AFTER the
    // toggle on page 1, not just on initial mount or the page-change
    // refetch.
    await waitFor(() => expect(pendingCalls).toBeGreaterThan(pendingCallsBeforeToggle));
  });
});
