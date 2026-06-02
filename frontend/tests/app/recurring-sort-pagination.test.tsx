import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";

import RecurringPage from "@/app/recurring/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import type { RecurringTransaction } from "@/lib/types";

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
  usePathname: () => "/recurring",
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
  password_set: true,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
  allow_manual_balance_adjustment: false,
};

function rec(over: Partial<RecurringTransaction>): RecurringTransaction {
  return {
    id: 1,
    account_id: 1,
    account_name: "Checking",
    category_id: 1,
    category_name: "Bills",
    description: "Item",
    amount: 10,
    type: "expense",
    frequency: "monthly",
    next_due_date: "2026-01-01",
    auto_settle: false,
    is_active: true,
    ...over,
  };
}

function mockApiWith(items: RecurringTransaction[]) {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/recurring") return Promise.resolve(items);
    return Promise.resolve({});
  }) as never);
}

function setAuth() {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

// Returns the active table's row descriptions in DOM order (desktop grid).
function activeRowOrder(): string[] {
  const table = screen.getByTestId("recurring-active-table");
  // desktop grid rows carry data-testid="recurring-row"
  const rows = within(table).queryAllByTestId("recurring-row");
  return rows.map((r) => r.getAttribute("data-description") ?? "");
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  window.localStorage.clear();
  setAuth();
});

describe("RecurringPage — default sort", () => {
  it("sorts active rows by next due date ascending by default", async () => {
    mockApiWith([
      rec({ id: 1, description: "Gamma", next_due_date: "2026-03-15" }),
      rec({ id: 2, description: "Alpha", next_due_date: "2026-01-05" }),
      rec({ id: 3, description: "Beta", next_due_date: "2026-02-10" }),
    ]);
    render(<RecurringPage />);
    await waitFor(() => expect(activeRowOrder().length).toBe(3));
    expect(activeRowOrder()).toEqual(["Alpha", "Beta", "Gamma"]);
  });
});

describe("RecurringPage — sorting by header click", () => {
  it("re-sorts by name when the Name header is clicked (asc then desc)", async () => {
    mockApiWith([
      rec({ id: 1, description: "Gamma", next_due_date: "2026-03-15" }),
      rec({ id: 2, description: "Alpha", next_due_date: "2026-01-05" }),
      rec({ id: 3, description: "Beta", next_due_date: "2026-02-10" }),
    ]);
    render(<RecurringPage />);
    await waitFor(() => expect(activeRowOrder().length).toBe(3));

    const table = screen.getByTestId("recurring-active-table");
    const nameHeader = within(table).getByRole("button", { name: /^Name/ });
    fireEvent.click(nameHeader);
    expect(activeRowOrder()).toEqual(["Alpha", "Beta", "Gamma"]);
    fireEvent.click(nameHeader);
    expect(activeRowOrder()).toEqual(["Gamma", "Beta", "Alpha"]);
  });

  it("sorts amount numerically (not lexicographically)", async () => {
    mockApiWith([
      rec({ id: 1, description: "Two", amount: 2, next_due_date: "2026-01-01" }),
      rec({ id: 2, description: "Ten", amount: 10, next_due_date: "2026-01-02" }),
      rec({ id: 3, description: "Nine", amount: 9, next_due_date: "2026-01-03" }),
    ]);
    render(<RecurringPage />);
    await waitFor(() => expect(activeRowOrder().length).toBe(3));

    const table = screen.getByTestId("recurring-active-table");
    const amtHeader = within(table).getByRole("button", { name: /^Amount/ });
    fireEvent.click(amtHeader);
    expect(activeRowOrder()).toEqual(["Two", "Nine", "Ten"]);
  });

  it("sorts nulls last for category regardless of direction", async () => {
    mockApiWith([
      rec({ id: 1, description: "HasCat", category_name: "Zed", next_due_date: "2026-01-01" }),
      rec({ id: 2, description: "NoCat", category_name: null as never, next_due_date: "2026-01-02" }),
      rec({ id: 3, description: "AlsoCat", category_name: "Abc", next_due_date: "2026-01-03" }),
    ]);
    render(<RecurringPage />);
    await waitFor(() => expect(activeRowOrder().length).toBe(3));

    const table = screen.getByTestId("recurring-active-table");
    const catHeader = within(table).getByRole("button", { name: /^Category/ });
    fireEvent.click(catHeader); // asc: Abc, Zed, then null
    expect(activeRowOrder()).toEqual(["AlsoCat", "HasCat", "NoCat"]);
    fireEvent.click(catHeader); // desc: Zed, Abc, then null still last
    expect(activeRowOrder()).toEqual(["HasCat", "AlsoCat", "NoCat"]);
  });

  it("keeps empty account_name last in descending sort (nulls-last stable)", async () => {
    mockApiWith([
      rec({ id: 1, description: "A", account_name: "Zeta", next_due_date: "2026-01-01" }),
      rec({ id: 2, description: "B", account_name: "", next_due_date: "2026-01-02" }),
      rec({ id: 3, description: "C", account_name: "Alpha", next_due_date: "2026-01-03" }),
    ]);
    render(<RecurringPage />);
    await waitFor(() => expect(activeRowOrder().length).toBe(3));

    const table = screen.getByTestId("recurring-active-table");
    const acctHeader = within(table).getByRole("button", { name: /^Account/ });
    fireEvent.click(acctHeader); // asc: Alpha, Zeta, empty-last
    expect(activeRowOrder()).toEqual(["C", "A", "B"]);
    fireEvent.click(acctHeader); // desc: Zeta, Alpha, empty still last
    expect(activeRowOrder()).toEqual(["A", "C", "B"]);
  });

  it("keeps empty description last in descending sort (nulls-last stable)", async () => {
    mockApiWith([
      rec({ id: 1, description: "Zeta", next_due_date: "2026-01-01" }),
      rec({ id: 2, description: "", next_due_date: "2026-01-02" }),
      rec({ id: 3, description: "Alpha", next_due_date: "2026-01-03" }),
    ]);
    render(<RecurringPage />);
    await waitFor(() => expect(activeRowOrder().length).toBe(3));

    const table = screen.getByTestId("recurring-active-table");
    // Default sort is next_due_date asc; click Name to sort by description
    const nameHeader = within(table).getByRole("button", { name: /^Name/ });
    fireEvent.click(nameHeader); // asc: Alpha, Zeta, empty-last
    expect(activeRowOrder()).toEqual(["Alpha", "Zeta", ""]);
    fireEvent.click(nameHeader); // desc: Zeta, Alpha, empty still last
    expect(activeRowOrder()).toEqual(["Zeta", "Alpha", ""]);
  });
});

describe("RecurringPage — pagination", () => {
  function manyItems(n: number): RecurringTransaction[] {
    return Array.from({ length: n }, (_, i) =>
      rec({
        id: i + 1,
        description: `Item ${String(i + 1).padStart(3, "0")}`,
        next_due_date: `2026-01-${String((i % 28) + 1).padStart(2, "0")}`,
      }),
    );
  }

  it("paginates the active list and Next/Prev change visible rows", async () => {
    mockApiWith(manyItems(30)); // > default page size 25
    render(<RecurringPage />);
    await waitFor(() => expect(activeRowOrder().length).toBe(25));

    const table = screen.getByTestId("recurring-active-table");
    expect(within(table).getByText(/Page 1 of 2/)).toBeInTheDocument();
    expect(within(table).getByText(/30 total/)).toBeInTheDocument();

    const firstPage = activeRowOrder();
    const next = within(table).getByRole("button", { name: /Next page/ });
    fireEvent.click(next);
    await waitFor(() => expect(activeRowOrder().length).toBe(5));
    const secondPage = activeRowOrder();
    expect(secondPage).not.toEqual(firstPage.slice(0, 5));

    const prev = within(table).getByRole("button", { name: /Previous page/ });
    fireEvent.click(prev);
    await waitFor(() => expect(activeRowOrder().length).toBe(25));
    expect(activeRowOrder()).toEqual(firstPage);
  });

  it("does not render pagination when rows fit on one page", async () => {
    mockApiWith(manyItems(3));
    render(<RecurringPage />);
    await waitFor(() => expect(activeRowOrder().length).toBe(3));
    const table = screen.getByTestId("recurring-active-table");
    expect(within(table).queryByText(/Page 1 of/)).not.toBeInTheDocument();
  });
});

describe("RecurringPage — page clamping when row count shrinks", () => {
  function manyItems(n: number): RecurringTransaction[] {
    return Array.from({ length: n }, (_, i) =>
      rec({
        id: i + 1,
        description: `Item ${String(i + 1).padStart(3, "0")}`,
        next_due_date: `2026-01-${String((i % 28) + 1).padStart(2, "0")}`,
      }),
    );
  }

  it("shows remaining rows (not blank) after a delete shrinks below page-2 threshold", async () => {
    // Start: 30 active items => page 1 of 2 (25 visible). Navigate to page 2.
    // Then simulate Delete of the item on page 2, which reloads with 24 items
    // (all fit on page 1). The table must not go blank.
    const initialItems = manyItems(30);

    // First API call returns 30 items; the DELETE call returns {}; the
    // subsequent reload (GET /api/v1/recurring) returns 24 items (the 30
    // minus the 6 that were on page 2 — we just drop id=25..30 for simplicity).
    const afterDeleteItems = manyItems(24);
    vi.mocked(apiFetch).mockImplementation(((url: string, opts?: RequestInit) => {
      if (url === "/api/v1/recurring" && (!opts || !opts.method || opts.method === "GET")) {
        // Second call (after delete) will re-enter here; we'll swap in the short list below.
        return Promise.resolve(initialItems);
      }
      return Promise.resolve({});
    }) as never);

    render(<RecurringPage />);

    // Wait for initial 25 rows (page 1 of 2).
    await waitFor(() => expect(activeRowOrder().length).toBe(25));

    const table = screen.getByTestId("recurring-active-table");

    // Navigate to page 2 (5 rows).
    const next = within(table).getByRole("button", { name: /Next page/ });
    fireEvent.click(next);
    await waitFor(() => expect(activeRowOrder().length).toBe(5));

    // Now re-configure the mock: next GET returns 24 items; DELETE succeeds.
    // The ConfirmModal needs "recurring/1" to match the first item on page 2
    // (id=26), but for simplicity we delete id=1 which triggers the same
    // reload path. We target the first Delete button visible in the table.
    vi.mocked(apiFetch).mockImplementation(((url: string, opts?: RequestInit) => {
      const method = opts?.method?.toUpperCase() ?? "GET";
      if (url === "/api/v1/recurring" && method === "GET") {
        return Promise.resolve(afterDeleteItems);
      }
      if (url.startsWith("/api/v1/recurring/") && method === "DELETE") {
        return Promise.resolve({ pending_removed: 0 });
      }
      return Promise.resolve({});
    }) as never);

    // Click Delete on the first row visible on page 2.
    const deleteButtons = within(table).getAllByRole("button", { name: /^Delete$/ });
    fireEvent.click(deleteButtons[0]);

    // ConfirmModal appears — find the dialog and click its Delete button.
    const dialog = await screen.findByRole("dialog");
    const confirmDeleteBtn = within(dialog).getByRole("button", { name: /^Delete$/ });
    fireEvent.click(confirmDeleteBtn);

    // After reload with 24 items, page 2 no longer exists. The clamp should
    // bring page back to 1, showing all 24 rows (default pageSize=25).
    await waitFor(() => expect(activeRowOrder().length).toBe(24));
  });
});

describe("RecurringPage — existing behavior preserved", () => {
  it("keeps Generate, Stop and Delete actions", async () => {
    mockApiWith([rec({ id: 1, description: "Rent" })]);
    render(<RecurringPage />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Generate this period/ })).toBeInTheDocument(),
    );
    const table = screen.getByTestId("recurring-active-table");
    expect(within(table).getAllByRole("button", { name: /^Stop$/ }).length).toBeGreaterThan(0);
    expect(within(table).getAllByRole("button", { name: /^Delete$/ }).length).toBeGreaterThan(0);
  });

  it("renders the Paused section with its own sortable table", async () => {
    mockApiWith([
      rec({ id: 1, description: "Active1", is_active: true }),
      rec({ id: 2, description: "Paused2", is_active: false, next_due_date: "2026-05-01" }),
      rec({ id: 3, description: "Paused1", is_active: false, next_due_date: "2026-04-01" }),
    ]);
    render(<RecurringPage />);
    await waitFor(() => expect(screen.getByText(/Paused \(2\)/)).toBeInTheDocument());
    const table = screen.getByTestId("recurring-paused-table");
    const rows = within(table).queryAllByTestId("recurring-row");
    expect(rows.map((r) => r.getAttribute("data-description"))).toEqual(["Paused1", "Paused2"]);
  });
});
