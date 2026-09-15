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

function cat(id: number, name: string, parent_id: number | null) {
  return {
    id, name, type: "expense" as const,
    parent_id, parent_name: null, description: null,
    slug: name.toLowerCase(), is_system: false, transaction_count: 0,
  };
}

const ACCOUNTS = [acct(100, "Checking A"), acct(200, "Checking B"), acct(300, "Old Savings", false)];
// Food is a master with two subs; Transport is a master with none.
const CATEGORIES = [
  cat(10, "Food", null),
  cat(11, "Groceries", 10),
  cat(12, "Dining", 10),
  cat(20, "Transport", null),
];
const TAGS = [
  { id: 1, name: "trip", name_normalized: "trip", usage_count: 2 },
  { id: 2, name: "work", name_normalized: "work", usage_count: 1 },
];

function setupApiFetch(total = 3) {
  const mock = vi.mocked(apiFetch);
  mock.mockReset();
  mock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return ACCOUNTS as never;
    if (url.startsWith("/api/v1/categories")) return CATEGORIES as never;
    if (url.startsWith("/api/v1/tags")) return TAGS as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url.startsWith("/api/v1/transactions"))
      return { items: [], total, limit: 25, offset: 0 } as never;
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

function panel() {
  return screen.getByTestId("transactions-filter-panel");
}

async function ready() {
  await screen.findByRole("button", { name: "Account Checking A" });
  await screen.findByRole("checkbox", { name: "Category Food" });
  await waitFor(() => expect(screen.queryByRole("status", { name: "Loading" })).toBeNull());
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

  it("sends one account_id per account picked in the panel, inactive accounts included", async () => {
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    const group = within(panel()).getByRole("group", { name: "Accounts" });
    fireEvent.click(within(group).getByRole("button", { name: "Account Checking A" }));
    fireEvent.click(within(group).getByRole("button", { name: "Account Old Savings" }));

    await waitFor(() => expect(lastParams(mock).getAll("account_id")).toEqual(["100", "300"]));
  });

  it("checking a master sends the master; unchecking one sub sends only the remaining subs", async () => {
    // FENCE. Kills: sending a partially selected master's id, which with the
    // default subtree match pulls the unchecked sub straight back in.
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    fireEvent.click(screen.getByRole("checkbox", { name: "Category Food" }));
    await waitFor(() =>
      expect(lastParams(mock).getAll("category_id").sort()).toEqual(["10", "11", "12"]),
    );
    expect(screen.getByRole("checkbox", { name: "Category Dining" })).toBeChecked();

    fireEvent.click(screen.getByRole("checkbox", { name: "Category Dining" }));
    await waitFor(() => expect(lastParams(mock).getAll("category_id")).toEqual(["11"]));
    expect(screen.getByRole("checkbox", { name: "Category Food" })).not.toBeChecked();

    // Unchecking the last sub clears the master too, not a lone master that
    // would read as "the whole subtree".
    fireEvent.click(screen.getByRole("checkbox", { name: "Category Groceries" }));
    await waitFor(() => expect(lastParams(mock).getAll("category_id")).toEqual([]));
    expect(screen.getByRole("checkbox", { name: "Category Food" })).not.toBeChecked();
  });

  it("a master with no subs is sent as itself", async () => {
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    fireEvent.click(screen.getByRole("checkbox", { name: "Category Transport" }));
    await waitFor(() => expect(lastParams(mock).getAll("category_id")).toEqual(["20"]));
  });

  it("a ?category_id= deep link of a master shows it fully checked and still sends the master", async () => {
    searchParamsState.value = new URLSearchParams("category_id=10");
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await waitFor(() => expect(screen.getByRole("checkbox", { name: "Category Food" })).toBeChecked());
    expect(screen.getByRole("checkbox", { name: "Category Groceries" })).toBeChecked();
    expect(lastParams(mock).getAll("category_id")).toContain("10");
  });

  it("a stored single-select master (pre-panel) keeps filtering its subtree", async () => {
    // FENCE. Kills: applying the partial-master rule to a lone stored master,
    // which silently widens a saved filter to every category.
    window.localStorage.setItem(FILTERS_KEY_TRANSACTIONS, JSON.stringify({ filterCategory: 10 }));
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await waitFor(() => expect(lastParams(mock).getAll("category_id")).toContain("10"));
    expect(screen.getByRole("checkbox", { name: "Category Food" })).toBeChecked();
  });

  it("keeps a category_match=exact drilldown on the master alone", async () => {
    // FENCE. Kills: expanding or omitting the master under exact match, which
    // either widens the list past the slice that opened it or unfilters it.
    searchParamsState.value = new URLSearchParams("category_id=10&category_match=exact");
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    await waitFor(() => {
      const params = lastParams(mock);
      expect(params.getAll("category_id")).toEqual(["10"]);
      expect(params.get("category_match")).toBe("exact");
    });

    // Picking in the tree drops exact and applies the tree's rules.
    const from = mock.mock.calls.length;
    fireEvent.click(screen.getByRole("checkbox", { name: "Category Transport" }));
    await waitFor(() => {
      const params = lastParams(mock, from);
      expect(params.getAll("category_id")).toContain("20");
      expect(params.get("category_match")).toBeNull();
    });
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

  it("below xl, Filters opens a focus-trapped dialog; Escape closes it and returns focus", async () => {
    // FENCE. Kills: no focus trap (focus never enters the drawer) and no
    // focus return (focus is stranded when the drawer closes).
    setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

    const aside = panel();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(aside.className.split(/\s+/)).toContain("invisible");
    expect(aside.className.split(/\s+/)).toContain("xl:visible");

    const open = screen.getByRole("button", { name: /^Filters/ });
    expect(open).toHaveAttribute("aria-expanded", "false");
    open.focus();
    fireEvent.click(open);

    const dialog = await screen.findByRole("dialog", { name: "Filters" });
    expect(dialog).toBe(aside);
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(open).toHaveAttribute("aria-expanded", "true");
    expect(dialog.className.split(/\s+/)).not.toContain("invisible");
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(open);
  });

  it("the drawer footer names the result count and closes the drawer", async () => {
    setupApiFetch(3);
    renderWithSWR(<TransactionsPage />);
    await ready();

    const open = screen.getByRole("button", { name: /^Filters/ });
    open.focus();
    fireEvent.click(open);
    const dialog = await screen.findByRole("dialog", { name: "Filters" });

    fireEvent.click(within(dialog).getByRole("button", { name: "Show 3 transactions" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(open);
  });

  it("announces the result count in an always-mounted live region", async () => {
    setupApiFetch(3);
    renderWithSWR(<TransactionsPage />);
    await ready();

    const count = await screen.findByText("3 transactions");
    expect(count.closest('[aria-live="polite"]')).not.toBeNull();
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
    // There is still exactly one reset on the page.
    expect(screen.queryAllByTestId("reset-sort-filters")).toHaveLength(0);
  });

  it("the section with selections is open, and its summary carries the count", async () => {
    window.localStorage.setItem(FILTERS_KEY_TRANSACTIONS, JSON.stringify({ filterTags: ["trip", "work"] }));
    setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await ready();

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
