import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import RecurringPage from "@/app/recurring/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import type { RecurringTransaction } from "@/lib/types";

/**
 * TBD-316. The series pointer on a recurring occurrence links to
 * `/recurring?recurring_id=<id>`, and the page lands the user on THAT template:
 * the page of its table that holds it, highlighted and scrolled to. An unknown
 * or malformed id degrades to the plain list.
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

// Both layouts render in jsdom; the highlight must land in each.
function highlighted(): string[] {
  return [
    ...screen.queryAllByTestId("recurring-row"),
    ...screen.queryAllByTestId("recurring-card"),
  ]
    .filter((el) => el.className.includes("ring-accent"))
    .map((el) => `${el.getAttribute("data-testid")}:${el.getAttribute("data-description")}`);
}

let scrollIntoView: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  window.localStorage.clear();
  searchParamsState.value = new URLSearchParams();
  scrollIntoView = vi.fn();
  window.HTMLElement.prototype.scrollIntoView = scrollIntoView;
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never, loading: false, needsSetup: false,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  } as never);
});

const THREE = [
  rec({ id: 1, description: "Alpha", next_due_date: "2026-01-05" }),
  rec({ id: 2, description: "Beta", next_due_date: "2026-02-10" }),
  rec({ id: 3, description: "Gamma", next_due_date: "2026-03-15" }),
];

describe("RecurringPage — ?recurring_id deep link (TBD-316)", () => {
  // FENCE: kills a highlight that ignores the id (first row, or every row).
  // Beta is neither first nor last.
  it("highlights exactly the target template, in both layouts, and scrolls to it", async () => {
    searchParamsState.value = new URLSearchParams("recurring_id=2");
    mockApiWith(THREE);
    render(<RecurringPage />);
    await waitFor(() => expect(highlighted()).toEqual(["recurring-row:Beta", "recurring-card:Beta"]));
    await waitFor(() =>
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "auto" }),
    );
  });

  // FENCE: kills "highlight only what is already on page 1". 30 rows at the
  // default page size of 25; the target sorts 28th.
  it("opens the page of the table that holds the target", async () => {
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

  // FENCE: the jump honours the PERSISTED sort and page size. Rows arrive in
  // reverse, sort by name at 10 per page puts Row 28 on page 3. Kills a
  // hard-coded page size of 25 (page 2) and an index taken from the unsorted
  // list (page 1). Also the only scroll assertion for a target off page 1, so
  // it kills a scroll effect that runs on mount only.
  it("uses the persisted sort and page size, and scrolls once the page is open", async () => {
    window.localStorage.setItem(
      "pfv:sort:recurring:active",
      JSON.stringify({ sortField: "description", sortDir: "asc", pageSize: 10 }),
    );
    const rows = Array.from({ length: 30 }, (_, i) =>
      rec({
        id: i + 1,
        description: `Row ${String(i + 1).padStart(2, "0")}`,
        next_due_date: `2026-01-${String(30 - i).padStart(2, "0")}`,
      }),
    ).reverse();
    searchParamsState.value = new URLSearchParams("recurring_id=28");
    mockApiWith(rows);
    render(<RecurringPage />);
    await waitFor(() => expect(highlighted()).toEqual(["recurring-row:Row 28", "recurring-card:Row 28"]));
    await waitFor(() =>
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "auto" }),
    );
  });

  // FENCE: kills dropping the once-guard. At 10 per page, re-sorting resets to
  // page 1; without the guard the target's new position (16th by amount, page
  // 2) re-jumps.
  it("re-sorting after the jump does not jump again", async () => {
    window.localStorage.setItem(
      "pfv:sort:recurring:active",
      JSON.stringify({ sortField: "next_due_date", sortDir: "asc", pageSize: 10 }),
    );
    const rows = Array.from({ length: 30 }, (_, i) => {
      const n = i + 1;
      // Decimal amounts arrive on the wire as strings (TBD-272).
      const amount = (n === 28 ? 16 : n === 16 ? 28 : n).toFixed(2);
      return rec({
        id: n,
        amount,
        description: `Row ${String(n).padStart(2, "0")}`,
        next_due_date: `2026-01-${String(n).padStart(2, "0")}`,
      });
    });
    searchParamsState.value = new URLSearchParams("recurring_id=28");
    mockApiWith(rows);
    render(<RecurringPage />);
    await waitFor(() => expect(highlighted()).toContain("recurring-row:Row 28"));

    const table = screen.getByTestId("recurring-active-table");
    fireEvent.click(within(table).getByRole("button", { name: /^Amount/ }));
    await waitFor(() =>
      expect(within(table).getAllByTestId("recurring-row")[0]).toHaveAttribute("data-description", "Row 01"),
    );
    expect(highlighted()).toEqual([]);
  });

  // FENCE: kills deriving the page from the target on every render, which
  // would trap the user on the target's page.
  it("the jump happens once: the user can page away afterwards", async () => {
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
    await waitFor(() => expect(highlighted()).toContain("recurring-row:Row 28"));

    const table = screen.getByTestId("recurring-active-table");
    fireEvent.click(within(table).getByRole("button", { name: /previous/i }));
    await waitFor(() =>
      expect(within(table).getAllByTestId("recurring-row")[0]).toHaveAttribute("data-description", "Row 01"),
    );
    expect(highlighted()).toEqual([]);
  });

  // FENCE: kills a lookup that only searches the active table.
  it("finds a paused template in the paused table", async () => {
    searchParamsState.value = new URLSearchParams("recurring_id=9");
    mockApiWith([...THREE, rec({ id: 9, description: "Paused gym", is_active: false })]);
    render(<RecurringPage />);
    await waitFor(() =>
      expect(highlighted()).toEqual(["recurring-row:Paused gym", "recurring-card:Paused gym"]),
    );
    expect(
      within(screen.getByTestId("recurring-paused-table")).getAllByTestId("recurring-row")[0].className,
    ).toContain("ring-accent");
  });

  // GUARD: an unknown or malformed id is the plain list, with no error. Most of
  // these match no row whatever the parse; only "2.5" discriminates (a
  // `parseInt` would highlight Beta).
  it.each(["999", "abc", "0", "-2", "2.5"])("recurring_id=%s: plain list, nothing highlighted", async (raw) => {
    searchParamsState.value = new URLSearchParams(`recurring_id=${raw}`);
    mockApiWith(THREE);
    render(<RecurringPage />);
    await waitFor(() => expect(screen.getAllByTestId("recurring-row")).toHaveLength(3));
    expect(highlighted()).toEqual([]);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});
