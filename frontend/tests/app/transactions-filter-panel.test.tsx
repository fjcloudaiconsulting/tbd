import React from "react";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithSWR } from "../utils/render-with-swr";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { FILTERS_KEY_TRANSACTIONS } from "@/lib/hooks/persisted-keys";

// TBD-464 PR 2: the filter side panel. Every control is driven through the
// panel the user sees, and every assertion reads the list request it sends.

const searchParamsState = vi.hoisted(() => ({
  value: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/transactions",
  useSearchParams: () => searchParamsState.value,
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

const USER = {
  id: 1, username: "user", email: "user@example.com",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const, org_id: 1, org_name: "Org",
  billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

function acct(id: number, name: string, is_active = true) {
  return {
    id, name, account_type_id: 1,
    account_type_name: "Checking", account_type_slug: "checking",
    balance: 0, currency: "EUR", is_active,
    close_day: null, is_default: id === 100,
  };
}

function cat(id: number, name: string, parent_id: number | null, transaction_count = 0) {
  return {
    id, name, type: "expense" as const,
    parent_id, parent_name: null, description: null,
    slug: name.toLowerCase(), is_system: false, transaction_count,
  };
}

function makeTx(id: number, description: string) {
  return {
    id, account_id: 100, account_name: "Checking A",
    category_id: 11, category_name: "Groceries",
    description, amount: 10, type: "expense" as const, status: "settled" as const,
    linked_transaction_id: null, recurring_id: null,
    date: "2026-05-01", settled_date: null, is_imported: false,
  };
}

const ACCOUNTS = [acct(100, "Checking A"), acct(200, "Checking B"), acct(300, "Old Savings", false)];
// Two masters, each with two subs. Food holds transactions of its own, so it
// gets a "Food (other)" row; Transport holds none.
const CATEGORIES = [
  cat(10, "Food", null, 4),
  cat(11, "Groceries", 10),
  cat(12, "Dining", 10),
  cat(20, "Transport", null),
  cat(21, "Fuel", 20),
  cat(22, "Parking", 20),
];
const TAGS = [
  { id: 1, name: "trip", name_normalized: "trip", usage_count: 2 },
  { id: 2, name: "work", name_normalized: "work", usage_count: 1 },
];

function setupApiFetch(
  total: number | ((params: URLSearchParams) => number) = 3,
  items: unknown[] = [],
) {
  const mock = vi.mocked(apiFetch);
  mock.mockReset();
  mock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return ACCOUNTS as never;
    if (url.startsWith("/api/v1/categories")) return CATEGORIES as never;
    if (url.startsWith("/api/v1/tags")) return TAGS as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url.startsWith("/api/v1/transactions")) {
      const params = new URL(url, "http://x").searchParams;
      const t = typeof total === "function" ? total(params) : total;
      return { items, total: t, limit: 25, offset: 0 } as never;
    }
    return null as never;
  });
  return mock;
}

type Mock = ReturnType<typeof setupApiFetch>;

function listUrls(mock: Mock, from = 0): string[] {
  return mock.mock.calls
    .slice(from)
    .map((c) => c[0])
    .filter((u): u is string => typeof u === "string" && u.startsWith("/api/v1/transactions?"));
}

function lastParams(mock: Mock, from = 0) {
  const urls = listUrls(mock, from);
  if (urls.length === 0) throw new Error("no list request yet");
  return new URL(urls[urls.length - 1], "http://x").searchParams;
}

function sortedCategoryIds(params: URLSearchParams) {
  return params.getAll("category_id").sort();
}

function panel() {
  return screen.getByTestId("transactions-filter-panel");
}

function checkbox(name: string) {
  return screen.getByRole("checkbox", { name: `Category ${name}` }) as HTMLInputElement;
}

async function ready() {
  await screen.findByRole("button", { name: "Account Checking A" });
  await screen.findByRole("checkbox", { name: "Category Food" });
  await waitFor(() => expect(screen.queryByRole("status", { name: "Loading" })).toBeNull());
}

async function openDrawer() {
  const open = screen.getByRole("button", { name: /^Filters/ });
  open.focus();
  fireEvent.click(open);
  const dialog = await screen.findByRole("dialog", { name: "Filters" });
  return { open, dialog };
}

describe("TransactionsPage — filter side panel (TBD-464)", () => {
  beforeEach(() => {
    searchParamsState.value = new URLSearchParams();
    window.localStorage.clear();
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    });
  });

  afterEach(() => {
    cleanup();
    // @ts-expect-error -- remove a matchMedia stub a test installed
    delete window.matchMedia;
  });

  it("keeps the panel mounted and focus on the control while the list refetches", async () => {
    // FENCE. Kills: the panel rendered inside the `fetching ? <Spinner/>`
    // ternary, which unmounts every control on each filter click.
    const mock = setupApiFetch();
    const base = mock.getMockImplementation()!;
    renderWithSWR(<TransactionsPage />);
    await ready();

    mock.mockImplementation(async (url: string) =>
      url.startsWith("/api/v1/transactions?") ? (new Promise(() => {}) as never) : base(url),
    );
    const chip = screen.getByRole("button", { name: "Account Checking A" });
    chip.focus();
    fireEvent.click(chip);

    await waitFor(() => expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument());
    expect(chip.isConnected).toBe(true);
    expect(document.activeElement).toBe(chip);
    expect(chip).toHaveAttribute("aria-pressed", "true");
  });

  it("the result count stays in its live region, unblanked, while a load is in flight", async () => {
    // FENCE (N3). Kills: blanking the count during `fetching`, which makes
    // every load re-announce an unchanged count.
    const mock = setupApiFetch(3);
    const base = mock.getMockImplementation()!;
    renderWithSWR(<TransactionsPage />);
    await ready();

    const region = screen.getByTestId("transactions-result-count");
    await waitFor(() => expect(region).toHaveTextContent("3 transactions"));
    expect(region).toHaveAttribute("aria-live", "polite");

    mock.mockImplementation(async (url: string) =>
      url.startsWith("/api/v1/transactions?") ? (new Promise(() => {}) as never) : base(url),
    );
    fireEvent.click(screen.getByRole("button", { name: "Account Checking A" }));
    await waitFor(() => expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument());

    expect(region.isConnected).toBe(true);
    expect(region.textContent).toBe("3 transactions");
  });

  it("sends one account_id per account picked in the panel, inactive accounts included", async () => {
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    const group = within(panel()).getByRole("group", { name: "Accounts" });
    fireEvent.click(within(group).getByRole("button", { name: "Account Checking A" }));
    fireEvent.click(within(group).getByRole("button", { name: "Account Old Savings" }));

    await waitFor(() => expect(lastParams(mock).getAll("account_id")).toEqual(["100", "300"]));
  });

  // ── Option C: tri-state groups plus an (other) row ─────────────────────

  it("the group toggle checks and clears the whole group; a missing sub leaves it partial; the request is exact", async () => {
    // FENCE (option C). Kills: option B (unchecking the master unchecks only
    // itself), and sending ids without category_match=exact.
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    fireEvent.click(checkbox("Food"));
    await waitFor(() => expect(sortedCategoryIds(lastParams(mock))).toEqual(["10", "11", "12"]));
    expect(lastParams(mock).get("category_match")).toBe("exact");
    expect(checkbox("Food (other)")).toBeChecked();

    fireEvent.click(checkbox("Dining"));
    await waitFor(() => expect(sortedCategoryIds(lastParams(mock))).toEqual(["10", "11"]));
    expect(lastParams(mock).get("category_match")).toBe("exact");
    expect(checkbox("Food")).not.toBeChecked();
    expect(checkbox("Food").indeterminate).toBe(true);

    fireEvent.click(checkbox("Food"));
    await waitFor(() => expect(sortedCategoryIds(lastParams(mock))).toEqual(["10", "11", "12"]));
    fireEvent.click(checkbox("Food"));
    await waitFor(() => expect(lastParams(mock).getAll("category_id")).toEqual([]));
    expect(checkbox("Groceries")).not.toBeChecked();
    expect(checkbox("Food (other)")).not.toBeChecked();
  });

  it("the (other) row alone sends only the master, exact, and leaves the group partial", async () => {
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    fireEvent.click(checkbox("Food (other)"));
    await waitFor(() => {
      const params = lastParams(mock);
      expect(params.getAll("category_id")).toEqual(["10"]);
      expect(params.get("category_match")).toBe("exact");
    });
    expect(checkbox("Food")).not.toBeChecked();
    expect(checkbox("Food").indeterminate).toBe(true);
    expect(checkbox("Groceries")).not.toBeChecked();
  });

  it("two masters and a partial pick send exactly the checked ids", async () => {
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    // Transport holds no transactions of its own: no (other) row, but the
    // group toggle still sends its id.
    expect(screen.queryByRole("checkbox", { name: "Category Transport (other)" })).toBeNull();
    fireEvent.click(checkbox("Food"));
    await waitFor(() => expect(checkbox("Dining")).toBeChecked());
    fireEvent.click(checkbox("Transport"));
    await waitFor(() =>
      expect(sortedCategoryIds(lastParams(mock))).toEqual(["10", "11", "12", "20", "21", "22"]),
    );

    fireEvent.click(checkbox("Fuel"));
    await waitFor(() =>
      expect(sortedCategoryIds(lastParams(mock))).toEqual(["10", "11", "12", "20", "22"]),
    );
    expect(checkbox("Transport")).not.toBeChecked();
    expect(checkbox("Transport").indeterminate).toBe(true);
    expect(checkbox("Food")).toBeChecked();
    expect(lastParams(mock).get("category_match")).toBe("exact");
  });

  it("a subtree ?category_id= deep link seeds the whole group, (other) included", async () => {
    // FENCE. Kills: seeding only the master, which under exact match drops
    // every sub a budget or forecast link meant to include.
    searchParamsState.value = new URLSearchParams("category_id=10");
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await waitFor(() => expect(sortedCategoryIds(lastParams(mock))).toEqual(["10", "11", "12"]));
    expect(lastParams(mock).get("category_match")).toBe("exact");
    // The first list request already carries the seed.
    expect(sortedCategoryIds(new URL(listUrls(mock)[0], "http://x").searchParams)).toEqual(["10", "11", "12"]);
    expect(checkbox("Food")).toBeChecked();
    expect(checkbox("Food (other)")).toBeChecked();
    expect(checkbox("Groceries")).toBeChecked();
  });

  it("a legacy ?category=<name> bookmark seeds the same way", async () => {
    searchParamsState.value = new URLSearchParams("category=food");
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await waitFor(() => expect(sortedCategoryIds(lastParams(mock))).toEqual(["10", "11", "12"]));
  });

  it("an exact drilldown shows only (other) checked and keeps its category when the user adds another", async () => {
    // FENCE (vacuity review BLOCKING). Kills: a pick replacing the linked
    // category, or dropping exact once the user touches the tree.
    searchParamsState.value = new URLSearchParams("category_id=10&category_match=exact");
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await waitFor(() => {
      const params = lastParams(mock);
      expect(params.getAll("category_id")).toEqual(["10"]);
      expect(params.get("category_match")).toBe("exact");
    });
    expect(checkbox("Food (other)")).toBeChecked();
    expect(checkbox("Groceries")).not.toBeChecked();
    await waitFor(() => expect(checkbox("Food").indeterminate).toBe(true));

    const from = mock.mock.calls.length;
    fireEvent.click(checkbox("Transport"));
    await waitFor(() => {
      const params = lastParams(mock, from);
      expect(sortedCategoryIds(params)).toEqual(["10", "20", "21", "22"]);
      expect(params.get("category_match")).toBe("exact");
    });
  });

  it("a saved master from before option C is read as that master alone: a partial group (accepted)", async () => {
    window.localStorage.setItem(FILTERS_KEY_TRANSACTIONS, JSON.stringify({ filterCategory: 10 }));
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await waitFor(() => {
      const params = lastParams(mock);
      expect(params.getAll("category_id")).toEqual(["10"]);
      expect(params.get("category_match")).toBe("exact");
    });
    expect(checkbox("Food (other)")).toBeChecked();
    await waitFor(() => expect(checkbox("Food").indeterminate).toBe(true));
  });

  it("the Categories badge leaves out a master the tree cannot show", async () => {
    // FENCE. Kills: counting the raw selection, where a master with no own
    // transactions (no (other) row) adds an invisible 1.
    window.localStorage.setItem(FILTERS_KEY_TRANSACTIONS, JSON.stringify({ filterCategory: [20, 21] }));
    setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    const summary = screen.getByTestId("filter-section-categories").querySelector("summary")!;
    expect(within(summary).getByText("1")).toBeInTheDocument();
    expect(within(summary).queryByText("2")).toBeNull();
  });

  it("drops saved category ids that no longer exist once categories load", async () => {
    // FENCE (re-review). Kills: a deleted category's id staying in the saved
    // selection, sent on every request and counted in the badge while
    // nothing in the tree shows it, so only Reset could clear it.
    window.localStorage.setItem(FILTERS_KEY_TRANSACTIONS, JSON.stringify({ filterCategory: [10, 999] }));
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await waitFor(() => expect(lastParams(mock).getAll("category_id")).toEqual(["10"]));
    for (const url of listUrls(mock)) {
      expect(new URL(url, "http://x").searchParams.getAll("category_id")).not.toContain("999");
    }
    expect(screen.getByRole("button", { name: /^Filters\s*,\s*1 active$/ })).toBeInTheDocument();
    expect(JSON.parse(window.localStorage.getItem(FILTERS_KEY_TRANSACTIONS)!).filterCategory).toEqual([10]);
  });

  // ── R2: a deep link clears saved filters ───────────────────────────────

  it("a deep link starts from default filters, then applies its own", async () => {
    // FENCE (R2). Kills: keeping saved filters under a deep link, which can
    // hide the very row it points at.
    window.localStorage.setItem(
      FILTERS_KEY_TRANSACTIONS,
      JSON.stringify({ filterTags: ["work"], filterType: "expense" }),
    );
    searchParamsState.value = new URLSearchParams("account_id=100&transaction_id=5");
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await waitFor(() => expect(lastParams(mock).getAll("account_id")).toEqual(["100"]));
    for (const url of listUrls(mock)) {
      const params = new URL(url, "http://x").searchParams;
      expect(params.get("tags")).toBeNull();
      expect(params.get("type")).toBeNull();
    }
  });

  it("sends picked tags with tag_match=any and shows no match radios", async () => {
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    const trip = await within(panel()).findByRole("button", { name: "Tag trip" });
    fireEvent.click(trip);
    fireEvent.click(within(panel()).getByRole("button", { name: "Tag work" }));

    await waitFor(() => {
      const params = lastParams(mock);
      expect(params.get("tags")).toBe("trip,work");
      expect(params.get("tag_match")).toBe("any");
    });
    expect(within(panel()).queryByRole("radio")).toBeNull();
  });

  it("the category search narrows the tree", async () => {
    setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    fireEvent.change(within(panel()).getByRole("textbox", { name: "Search categories" }), {
      target: { value: "din" },
    });

    await waitFor(() => expect(screen.queryByRole("checkbox", { name: "Category Groceries" })).toBeNull());
    expect(screen.getByRole("checkbox", { name: "Category Dining" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Category Transport" })).toBeNull();
  });

  // ── The drawer (below xl) ──────────────────────────────────────────────

  it("below xl, Filters opens a focus-trapped dialog; Escape closes it and returns focus", async () => {
    // FENCE. Kills: no focus trap (focus never enters the drawer), no focus
    // return, and a closed drawer that stays reachable (no `invisible`).
    setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    const aside = panel();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(aside.className.split(/\s+/)).toContain("invisible");
    expect(aside.className.split(/\s+/)).toContain("xl:visible");

    expect(screen.getByRole("button", { name: /^Filters/ })).toHaveAttribute("aria-expanded", "false");
    const { open, dialog } = await openDrawer();
    expect(dialog).toBe(aside);
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(open).toHaveAttribute("aria-expanded", "true");
    expect(dialog.className.split(/\s+/)).not.toContain("invisible");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(open);
    expect(aside.className.split(/\s+/)).toContain("invisible");
  });

  it("the scrim and the Close button each close the drawer", async () => {
    setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await openDrawer();
    fireEvent.click(screen.getByTestId("transactions-filter-scrim"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByTestId("transactions-filter-scrim")).toBeNull();

    const { open, dialog } = await openDrawer();
    fireEvent.click(within(dialog).getByRole("button", { name: "Close filters" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(open);
  });

  it("the footer names the current result count and closes the drawer", async () => {
    // Kills: a footer count frozen at the first load.
    const mock = setupApiFetch((params) => (params.getAll("account_id").includes("100") ? 7 : 3));
    renderWithSWR(<TransactionsPage />);
    await ready();

    const { open, dialog } = await openDrawer();
    within(dialog).getByRole("button", { name: "Show 3 transactions" });

    fireEvent.click(within(dialog).getByRole("button", { name: "Account Checking A" }));
    await waitFor(() => expect(lastParams(mock).getAll("account_id")).toEqual(["100"]));
    const show = await within(dialog).findByRole("button", { name: "Show 7 transactions" });

    fireEvent.click(show);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(open);
  });

  it("the Filters button names how many filters are active", async () => {
    window.localStorage.setItem(
      FILTERS_KEY_TRANSACTIONS,
      JSON.stringify({ filterAccount: [100], filterCategory: [20], filterTags: ["trip"], filterType: "expense" }),
    );
    setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    // jsdom's name computation pads element boundaries, hence the \s*.
    expect(screen.getByRole("button", { name: /^Filters\s*,\s*4 active$/ })).toBeInTheDocument();
  });

  it("crossing into xl closes the drawer so the side panel is not left modal", async () => {
    // FENCE (N2). Kills: no matchMedia listener (the panel stays a modal
    // dialog trapping focus after a resize or rotate).
    const listeners: (() => void)[] = [];
    const xl = { matches: false };
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      get matches() {
        return query === "(min-width: 80rem)" ? xl.matches : false;
      },
      media: query,
      addEventListener: (_: string, cb: () => void) => {
        if (query === "(min-width: 80rem)") listeners.push(cb);
      },
      removeEventListener: () => {},
    }));
    setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await openDrawer();
    expect(listeners.length).toBeGreaterThan(0);
    act(() => {
      xl.matches = true;
      listeners.forEach((cb) => cb());
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(panel()).not.toHaveAttribute("aria-modal");
  });

  it("Escape that closes the drawer does not clear selected rows", async () => {
    // FENCE. Kills: the page-level Escape handler also dropping the selection.
    setupApiFetch(1, [makeTx(1, "Coffee")]);
    renderWithSWR(<TransactionsPage />);
    await ready();

    fireEvent.click((await screen.findAllByRole("checkbox", { name: "Select transaction 1" }))[0]);
    await screen.findByText("1 selected");

    await openDrawer();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("Reset in the panel header clears every filter", async () => {
    window.localStorage.setItem(
      FILTERS_KEY_TRANSACTIONS,
      JSON.stringify({ filterAccount: [100], filterCategory: [20], filterTags: ["trip"], filterType: "expense" }),
    );
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();
    expect(lastParams(mock).getAll("account_id")).toEqual(["100"]);
    // There is exactly one Reset on the page, and it is in the panel.
    expect(screen.getAllByTestId("reset-sort-filters")).toHaveLength(1);

    const from = mock.mock.calls.length;
    fireEvent.click(within(panel()).getByRole("button", { name: "Reset filters and sort" }));

    await waitFor(() => {
      const params = lastParams(mock, from);
      expect(params.getAll("account_id")).toEqual([]);
      expect(params.getAll("category_id")).toEqual([]);
      expect(params.get("tags")).toBeNull();
      expect(params.get("type")).toBeNull();
    });
    expect(window.localStorage.getItem(FILTERS_KEY_TRANSACTIONS)).toBeNull();
    expect(screen.getByRole("button", { name: "Account Checking A" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.queryAllByTestId("reset-sort-filters")).toHaveLength(0);
  });

  it("the section with selections is open, and its summary carries the count", async () => {
    window.localStorage.setItem(FILTERS_KEY_TRANSACTIONS, JSON.stringify({ filterTags: ["trip", "work"] }));
    setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    // Operator request: Date sits before Categories and starts open, like
    // Accounts and Categories.
    const order = Array.from(panel().querySelectorAll("details")).map((d) => d.dataset.testid);
    expect(order).toEqual([
      "filter-section-accounts",
      "filter-section-date",
      "filter-section-categories",
      "filter-section-tags",
      "filter-section-type",
    ]);
    expect((screen.getByTestId("filter-section-date") as HTMLDetailsElement).open).toBe(true);

    const tags = screen.getByTestId("filter-section-tags") as HTMLDetailsElement;
    expect(tags.open).toBe(true);
    expect(within(tags.querySelector("summary")!).getByText("2")).toBeInTheDocument();
    expect((screen.getByTestId("filter-section-type") as HTMLDetailsElement).open).toBe(false);

    // Clearing the last tag must not collapse the section under the pointer.
    fireEvent.click(within(tags).getByRole("button", { name: "Tag trip" }));
    await waitFor(() =>
      expect(within(tags).getByRole("button", { name: "Tag trip" })).toHaveAttribute("aria-pressed", "false"),
    );
    fireEvent.click(within(tags).getByRole("button", { name: "Tag work" }));
    await waitFor(() =>
      expect(within(tags).getByRole("button", { name: "Tag work" })).toHaveAttribute("aria-pressed", "false"),
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(within(tags.querySelector("summary")!).queryByText(/^\d+$/)).toBeNull();
    expect(tags.open).toBe(true);
  });
});
