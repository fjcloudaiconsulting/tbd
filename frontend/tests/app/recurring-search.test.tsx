import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import RecurringPage from "@/app/recurring/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { FILTERS_KEY_RECURRING, SORT_KEY_RECURRING } from "@/lib/hooks/persisted-keys";
import type { RecurringTransaction } from "@/lib/types";

/**
 * TBD-761. One search box above both recurring tables filters, client side,
 * by name, account, category and amount. It runs before sort and pagination,
 * is persisted, and is cleared by the shared "Reset filters and sort" button.
 */

const searchParamsState = vi.hoisted(() => ({ value: new URLSearchParams() }));

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
  useSearchParams: () => ({ get: (key: string) => searchParamsState.value.get(key) }),
}));

const USER = {
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner", org_id: 1,
  org_name: "Acme", billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, password_set: true, subscription_status: null,
  subscription_plan: null, trial_end: null, allow_manual_balance_adjustment: false,
};

function rec(over: Partial<RecurringTransaction>): RecurringTransaction {
  return {
    id: 1, account_id: 1, account_name: "Checking", category_id: 1,
    category_name: "Bills", description: "Item", amount: "10.00", type: "expense",
    frequency: "monthly", next_due_date: "2026-01-01", auto_settle: false,
    is_active: true, occurrence_count: null, occurrences_elapsed: 0,
    ...over,
  };
}

function mockApiWith(items: RecurringTransaction[]) {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (url === "/api/v1/recurring") return Promise.resolve(items);
    return Promise.resolve({});
  }) as never);
}

// Desktop rows of one table, in DOM order.
function rowsOf(testId: string): string[] {
  const table = screen.queryByTestId(testId);
  if (!table) return [];
  return within(table)
    .queryAllByTestId("recurring-row")
    .map((r) => r.getAttribute("data-description") ?? "");
}

// Mobile cards of one table, in DOM order.
function cardsOf(testId: string): string[] {
  const table = screen.queryByTestId(testId);
  if (!table) return [];
  return within(table)
    .queryAllByTestId("recurring-card")
    .map((r) => r.getAttribute("data-description") ?? "");
}

const ACTIVE = "recurring-active-table";
const PAUSED = "recurring-paused-table";

function searchBox(): HTMLInputElement {
  return screen.getByRole("textbox", { name: "Search recurring transactions" }) as HTMLInputElement;
}

function typeSearch(value: string) {
  fireEvent.change(searchBox(), { target: { value } });
}

function highlighted(): string[] {
  return [
    ...screen.queryAllByTestId("recurring-row"),
    ...screen.queryAllByTestId("recurring-card"),
  ]
    .filter((el) => el.className.includes("ring-accent"))
    .map((el) => `${el.getAttribute("data-testid")}:${el.getAttribute("data-description")}`);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  window.localStorage.clear();
  searchParamsState.value = new URLSearchParams();
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never, loading: false, needsSetup: false,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  } as never);
});

const MIXED = [
  rec({ id: 1, description: "Rent", account_name: "Checking", category_name: "Housing", amount: "1200.00", next_due_date: "2026-01-01" }),
  rec({ id: 2, description: "Netflix", account_name: "Visa Card", category_name: "Streaming", amount: "15.49", next_due_date: "2026-01-02" }),
  rec({ id: 3, description: "Salary", account_name: "Checking", category_name: "Income", amount: "12.50", type: "income", next_due_date: "2026-01-03" }),
  rec({ id: 4, description: "Gym", account_name: "Savings", category_name: null as never, amount: "12.50", next_due_date: "2026-01-04" }),
  rec({ id: 5, description: "Old phone plan", account_name: "Visa Card", category_name: "Phone", amount: "40.00", is_active: false, next_due_date: "2026-01-05" }),
  rec({ id: 6, description: "Old rent", account_name: "Checking", category_name: "Housing", amount: "900.00", is_active: false, next_due_date: "2026-01-06" }),
];

describe("RecurringPage — search (TBD-761)", { timeout: 20_000 }, () => {
  it("filters by name, case-insensitively and trimmed, in both layouts", async () => {
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));

    typeSearch("  NETFLIX ");
    expect(rowsOf(ACTIVE)).toEqual(["Netflix"]);
    expect(cardsOf(ACTIVE)).toEqual(["Netflix"]);
  });

  it("filters by account name", async () => {
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));

    typeSearch("visa");
    expect(rowsOf(ACTIVE)).toEqual(["Netflix"]);
    expect(rowsOf(PAUSED)).toEqual(["Old phone plan"]);
  });

  it("filters by category name, and a row with no category never matches it", async () => {
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));

    typeSearch("stream");
    expect(rowsOf(ACTIVE)).toEqual(["Netflix"]);
    typeSearch("housing");
    expect(rowsOf(ACTIVE)).toEqual(["Rent"]);
    expect(rowsOf(PAUSED)).toEqual(["Old rent"]);
  });

  // FENCE: amounts are Decimal strings ("12.50"), so a plain substring test
  // misses "12.5", and the formatted "1,200.00" misses "1200".
  it.each([
    ["12.5", ["Salary", "Gym"]],
    ["12.50", ["Salary", "Gym"]],
    ["$12.5", ["Salary", "Gym"]],
    ["1200", ["Rent"]],
    ["1,200", ["Rent"]],
    ["1,200.00", ["Rent"]],
    ["15.49", ["Netflix"]],
  ])("matches amount %s", async (query, expected) => {
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));

    typeSearch(query);
    expect(rowsOf(ACTIVE)).toEqual(expected);
  });

  // FENCE: the rows SHOW a sign ("-12.50" / "+12.50"); a typed sign narrows
  // to that side, an unsigned amount matches both.
  it.each([
    ["-12.5", ["Gym"]],
    ["+12.50", ["Salary"]],
  ])("a signed amount %s matches only that side", async (query, expected) => {
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));

    typeSearch(query);
    expect(rowsOf(ACTIVE)).toEqual(expected);
  });

  it("applies to both tables and shows 'x of y' in each title while active", async () => {
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));
    expect(within(screen.getByTestId(ACTIVE)).getByRole("heading")).toHaveTextContent("Active (4)");
    expect(within(screen.getByTestId(PAUSED)).getByRole("heading")).toHaveTextContent("Paused (2)");

    typeSearch("rent");
    expect(rowsOf(ACTIVE)).toEqual(["Rent"]);
    expect(rowsOf(PAUSED)).toEqual(["Old rent"]);
    expect(within(screen.getByTestId(ACTIVE)).getByRole("heading")).toHaveTextContent("Active (1 of 4)");
    expect(within(screen.getByTestId(PAUSED)).getByRole("heading")).toHaveTextContent("Paused (1 of 2)");

    // Whitespace alone is no search.
    typeSearch("   ");
    expect(within(screen.getByTestId(ACTIVE)).getByRole("heading")).toHaveTextContent("Active (4)");
  });

  it("shows a no-match state per table, distinct from the empty-list label", async () => {
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));

    typeSearch("netflix");
    const paused = screen.getByTestId(PAUSED);
    // The Paused table stays: its templates exist, they just don't match.
    expect(within(paused).getByRole("heading")).toHaveTextContent("Paused (0 of 2)");
    expect(within(paused).getAllByText("No recurring transactions match your search.").length).toBeGreaterThan(0);
    expect(within(paused).queryByText("No paused recurring transactions.")).toBeNull();

    typeSearch("zzz");
    const active = screen.getByTestId(ACTIVE);
    expect(within(active).getAllByText("No recurring transactions match your search.").length).toBeGreaterThan(0);
    expect(within(active).queryByText("No active recurring transactions.")).toBeNull();
  });

  // FENCE: filter BEFORE paginate. 30 rows, 2 match, both past page 1.
  it("filters before paginating and resets the table to page 1", async () => {
    const rows = Array.from({ length: 30 }, (_, i) =>
      rec({
        id: i + 1,
        description: i >= 26 ? `Target ${i + 1}` : `Row ${String(i + 1).padStart(2, "0")}`,
        next_due_date: `2026-01-${String(i + 1).padStart(2, "0")}`,
      }),
    );
    mockApiWith(rows);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(25));

    const table = screen.getByTestId(ACTIVE);
    fireEvent.click(within(table).getByRole("button", { name: /Next page/ }));
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(5));

    typeSearch("row");
    // Back on page 1 of the 26 matches, not page 2 of them (1 row).
    expect(rowsOf(ACTIVE)).toHaveLength(25);
    expect(rowsOf(ACTIVE)[0]).toBe("Row 01");
    expect(within(table).getByText(/Page 1 of 2/)).toBeInTheDocument();
    expect(within(table).getByText(/26 total/)).toBeInTheDocument();

    typeSearch("target");
    expect(rowsOf(ACTIVE)).toEqual(["Target 27", "Target 28", "Target 29", "Target 30"]);
    expect(within(table).queryByText(/Page 1 of/)).toBeNull();
  });

  it("persists the search across a remount", async () => {
    mockApiWith(MIXED);
    const { unmount } = render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));
    typeSearch("gym");
    expect(rowsOf(ACTIVE)).toEqual(["Gym"]);
    expect(JSON.parse(window.localStorage.getItem(FILTERS_KEY_RECURRING) ?? "{}")).toEqual({ search: "gym" });
    unmount();

    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toEqual(["Gym"]));
    expect(searchBox().value).toBe("gym");
  });

  it("the clear button empties the search", async () => {
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));
    expect(screen.queryByRole("button", { name: "Clear search" })).toBeNull();

    typeSearch("gym");
    fireEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(searchBox().value).toBe("");
    expect(rowsOf(ACTIVE)).toHaveLength(4);
  });

  it("Reset filters and sort clears the search and both tables' sort", async () => {
    window.localStorage.setItem(
      `${SORT_KEY_RECURRING}:stopped`,
      JSON.stringify({ sortField: "description", sortDir: "desc", pageSize: 25 }),
    );
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));
    // Paused sorted by name desc from storage.
    expect(rowsOf(PAUSED)).toEqual(["Old rent", "Old phone plan"]);
    // Visible: the paused table's sort is not the default.
    expect(screen.getByRole("button", { name: "Reset filters and sort" })).toBeInTheDocument();

    fireEvent.click(within(screen.getByTestId(ACTIVE)).getByRole("button", { name: /^Name/ }));
    expect(rowsOf(ACTIVE)).toEqual(["Gym", "Netflix", "Rent", "Salary"]);
    typeSearch("e");

    fireEvent.click(screen.getByRole("button", { name: "Reset filters and sort" }));
    expect(searchBox().value).toBe("");
    // Both back to next due date ascending.
    expect(rowsOf(ACTIVE)).toEqual(["Rent", "Netflix", "Salary", "Gym"]);
    expect(rowsOf(PAUSED)).toEqual(["Old phone plan", "Old rent"]);
    expect(window.localStorage.getItem(FILTERS_KEY_RECURRING)).toBeNull();
    // Nothing left to reset: the button hides.
    expect(screen.queryByRole("button", { name: "Reset filters and sort" })).toBeNull();
  });

  it("hides the reset button while sort and search are at their defaults", async () => {
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toHaveLength(4));
    expect(screen.queryByRole("button", { name: "Reset filters and sort" })).toBeNull();
    typeSearch("gym");
    expect(screen.getByRole("button", { name: "Reset filters and sort" })).toBeInTheDocument();
  });
});

describe("RecurringPage — search and the ?recurring_id deep link (TBD-761)", { timeout: 20_000 }, () => {
  // A search that already shows the target is kept.
  it("keeps a persisted search that matches the target, and highlights it", async () => {
    window.localStorage.setItem(FILTERS_KEY_RECURRING, JSON.stringify({ search: "e" }));
    searchParamsState.value = new URLSearchParams("recurring_id=2");
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(highlighted()).toEqual(["recurring-row:Netflix", "recurring-card:Netflix"]));
    expect(searchBox().value).toBe("e");
  });

  // FENCE: the link addresses one template; a persisted search hiding it
  // would land the user on a page with nothing highlighted.
  it("clears a persisted search that hides the target, then highlights it", async () => {
    window.localStorage.setItem(FILTERS_KEY_RECURRING, JSON.stringify({ search: "rent" }));
    searchParamsState.value = new URLSearchParams("recurring_id=2");
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(highlighted()).toEqual(["recurring-row:Netflix", "recurring-card:Netflix"]));
    expect(searchBox().value).toBe("");
    expect(rowsOf(ACTIVE)).toHaveLength(4);
  });

  // The page holding the target is still found once the search clears.
  it("lands on the target's page after clearing the search", async () => {
    window.localStorage.setItem(FILTERS_KEY_RECURRING, JSON.stringify({ search: "nomatch" }));
    const rows = Array.from({ length: 30 }, (_, i) =>
      rec({
        id: i + 1,
        description: `Row ${String(i + 1).padStart(2, "0")}`,
        next_due_date: `2026-01-${String(i + 1).padStart(2, "0")}`,
      }),
    );
    searchParamsState.value = new URLSearchParams("recurring_id=28");
    mockApiWith(rows);
    render(<RecurringPage />);
    await waitFor(() => expect(highlighted()).toEqual(["recurring-row:Row 28", "recurring-card:Row 28"]));
  });

  // GUARD: the clear is once, on landing. A search typed afterwards that
  // hides the target is the user's to keep.
  it("does not fight a search typed after landing", async () => {
    searchParamsState.value = new URLSearchParams("recurring_id=2");
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(highlighted()).toContain("recurring-row:Netflix"));

    typeSearch("rent");
    expect(searchBox().value).toBe("rent");
    expect(rowsOf(ACTIVE)).toEqual(["Rent"]);
  });

  // GUARD: an unknown id leaves the persisted search alone.
  it("an unknown id keeps the persisted search", async () => {
    window.localStorage.setItem(FILTERS_KEY_RECURRING, JSON.stringify({ search: "rent" }));
    searchParamsState.value = new URLSearchParams("recurring_id=999");
    mockApiWith(MIXED);
    render(<RecurringPage />);
    await waitFor(() => expect(rowsOf(ACTIVE)).toEqual(["Rent"]));
    expect(searchBox().value).toBe("rent");
  });
});
