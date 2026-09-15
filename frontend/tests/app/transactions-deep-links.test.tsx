import React from "react";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithSWR } from "../utils/render-with-swr";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { FILTERS_KEY_TRANSACTIONS } from "@/lib/hooks/persisted-keys";

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

const ACCT_A = {
  id: 100, name: "Checking A", account_type_id: 1,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: true,
};

const ACCT_B = {
  id: 200, name: "Checking B", account_type_id: 1,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: false,
};

const CATEGORY = {
  id: 11, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};

function makeTx(over: Partial<{
  id: number;
  account_id: number;
  account_name: string;
  description: string;
  amount: number;
  date: string;
}> = {}) {
  return {
    id: 1,
    account_id: ACCT_A.id,
    account_name: ACCT_A.name,
    category_id: CATEGORY.id,
    category_name: CATEGORY.name,
    description: "Tx",
    amount: 100,
    type: "expense" as const,
    status: "settled" as const,
    linked_transaction_id: null,
    recurring_id: null,
    date: "2026-05-01",
    settled_date: null,
    is_imported: false,
    ...over,
  };
}

function setupApiFetch(txs: ReturnType<typeof makeTx>[]) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT_A, ACCT_B] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) {
      return [{ id: 9, start_date: "2026-05-01", end_date: null }] as never;
    }
    if (url.startsWith("/api/v1/transactions"))
      return { items: txs, total: txs.length, limit: 25, offset: 0 } as never;
    return null as never;
  });
  return apiFetchMock;
}

function listUrls(mock: ReturnType<typeof vi.mocked<typeof apiFetch>>): string[] {
  return mock.mock.calls
    .map((call) => call[0])
    .filter(
      (url): url is string =>
        typeof url === "string" && url.startsWith("/api/v1/transactions?"),
    );
}

function listUrlsAfter(
  mock: ReturnType<typeof vi.mocked<typeof apiFetch>>,
  startIndex: number,
): string[] {
  return mock.mock.calls
    .slice(startIndex)
    .map((call) => call[0])
    .filter(
      (url): url is string =>
        typeof url === "string" && url.startsWith("/api/v1/transactions?"),
    );
}

describe("TransactionsPage — dashboard deep links", () => {
  const useAuthMock = vi.mocked(useAuth);
  let scrollIntoView: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    searchParamsState.value = new URLSearchParams();
    window.localStorage.clear();
    scrollIntoView = vi.fn();
    window.HTMLElement.prototype.scrollIntoView = scrollIntoView;
    useAuthMock.mockReturnValue({
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

  it("applies account_id and explicit date URL filters once", async () => {
    searchParamsState.value = new URLSearchParams(
      "account_id=200&date_from=2026-05-01&date_to=2026-05-31",
    );
    const mock = setupApiFetch([]);

    renderWithSWR(<TransactionsPage />);

    await waitFor(() => {
      const urls = listUrls(mock);
      expect(urls.length).toBeGreaterThan(0);
      const last = urls[urls.length - 1];
      expect(last).toContain("account_id=200");
      expect(last).toContain("date_from=2026-05-01");
      expect(last).toContain("date_to=2026-05-31");
    });

    expect(screen.getByLabelText("Filter by account")).toHaveValue("200");
    expect(screen.getByLabelText("From date")).toHaveValue("2026-05-01");
    expect(screen.getByLabelText("To date")).toHaveValue("2026-05-31");

    const startCount = mock.mock.calls.length;
    fireEvent.change(screen.getByLabelText("Filter by account"), {
      target: { value: "100" },
    });

    await waitFor(() => {
      const after = listUrlsAfter(mock, startCount);
      expect(after.at(-1)).toContain("account_id=100");
    });
  });

  it("highlights and scrolls a visible transaction_id target row", async () => {
    searchParamsState.value = new URLSearchParams("transaction_id=42");
    setupApiFetch([
      makeTx({ id: 41, description: "Other tx" }),
      makeTx({ id: 42, description: "Target tx" }),
    ]);

    renderWithSWR(<TransactionsPage />);

    const desktopRow = await screen.findByTestId("tx-row-desktop-42");
    const mobileRow = await screen.findByTestId("tx-row-mobile-42");

    expect(desktopRow.className).toContain("ring-accent");
    expect(mobileRow.className).toContain("ring-accent");
    await waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "auto" });
    });
  });

  // ── TBD-464: multi-valued filter state and the list URL contract ─────────

  function lastParams(mock: ReturnType<typeof vi.mocked<typeof apiFetch>>) {
    const urls = listUrls(mock);
    expect(urls.length).toBeGreaterThan(0);
    return new URL(urls[urls.length - 1], "http://x").searchParams;
  }

  function storeFilters(patch: Record<string, unknown>) {
    window.localStorage.setItem(FILTERS_KEY_TRANSACTIONS, JSON.stringify(patch));
  }

  it("migrates an old scalar stored filterAccount into account_id", async () => {
    // FENCE. Kills: renaming the field (the stored value is dropped), passing
    // the scalar through to array code, and the hook dropping arrays.
    storeFilters({ filterAccount: 5 });
    const mock = setupApiFetch([]);

    renderWithSWR(<TransactionsPage />);

    await waitFor(() => {
      expect(lastParams(mock).getAll("account_id")).toEqual(["5"]);
    });
  });

  it("sends one account_id per selected account", async () => {
    // FENCE. Kills: comma-joining the ids, and last-wins.
    storeFilters({ filterAccount: [100, 200] });
    const mock = setupApiFetch([]);

    renderWithSWR(<TransactionsPage />);

    await waitFor(() => {
      expect(lastParams(mock).getAll("account_id")).toEqual(["100", "200"]);
    });
  });

  it("reads every account_id from the URL and keeps the transaction highlight", async () => {
    // FENCE. Kills: `searchParams.get` into a scalar (only the first id lands).
    searchParamsState.value = new URLSearchParams(
      "account_id=100&account_id=200&transaction_id=42",
    );
    const mock = setupApiFetch([makeTx({ id: 42, description: "Target tx" })]);

    renderWithSWR(<TransactionsPage />);

    await waitFor(() => {
      expect(lastParams(mock).getAll("account_id")).toEqual(["100", "200"]);
    });
    const desktopRow = await screen.findByTestId("tx-row-desktop-42");
    expect(desktopRow.className).toContain("ring-accent");
  });

  it("passes category_id and category_match=exact through from a deep link", async () => {
    // FENCE. Kills: dropping category_match (the list silently widens to the
    // subtree and no longer sums to the slice that opened it).
    searchParamsState.value = new URLSearchParams("category_id=7&category_match=exact");
    const mock = setupApiFetch([]);

    renderWithSWR(<TransactionsPage />);

    await waitFor(() => {
      const params = lastParams(mock);
      expect(params.getAll("category_id")).toEqual(["7"]);
      expect(params.get("category_match")).toBe("exact");
    });
  });

  it("sends selected tags with tag_match=any", async () => {
    // FENCE. Kills: relying on the API default `all` (operator ruling
    // 2026-09-14: the panel's tag filter is OR).
    storeFilters({ filterTags: ["a", "b"] });
    const mock = setupApiFetch([]);

    renderWithSWR(<TransactionsPage />);

    await waitFor(() => {
      const params = lastParams(mock);
      expect(params.get("tags")).toBe("a,b");
      expect(params.get("tag_match")).toBe("any");
    });
  });

  it("shows the latest filter's rows when responses resolve in reverse order", async () => {
    // FENCE (TBD-535). Kills: a missing sequence guard in loadTransactions.
    const apiFetchMock = setupApiFetch([]);
    const pending = new Map<string, (v: unknown) => void>();
    const base = apiFetchMock.getMockImplementation()!;
    apiFetchMock.mockImplementation(async (url: string) => {
      const acct = url.startsWith("/api/v1/transactions?")
        ? new URL(url, "http://x").searchParams.get("account_id")
        : null;
      if (acct) return new Promise((resolve) => pending.set(acct, resolve)) as never;
      return base(url);
    });

    renderWithSWR(<TransactionsPage />);
    const select = await screen.findByLabelText("Filter by account");
    await waitFor(() => expect(select).toHaveTextContent("Checking B"));

    fireEvent.change(select, { target: { value: "100" } });
    await waitFor(() => expect(pending.has("100")).toBe(true));
    fireEvent.change(select, { target: { value: "200" } });
    await waitFor(() => expect(pending.has("200")).toBe(true));

    pending.get("200")!({ items: [makeTx({ id: 2, description: "Latest rows" })], total: 1 });
    await screen.findAllByText("Latest rows");
    pending.get("100")!({ items: [makeTx({ id: 1, description: "Stale rows" })], total: 1 });

    // Let the stale resolution flush before asserting it changed nothing.
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryAllByText("Stale rows")).toHaveLength(0);
    expect(screen.getAllByText("Latest rows").length).toBeGreaterThan(0);
  });

  it("clears the spinner when a stale load is superseded by a load that rejects", async () => {
    // FENCE (TBD-535 review). Filter change starts load A (spinner up); a
    // post-write refresh starts load B. A resolves stale and must not write,
    // B rejects. Kills: a seq guard that skips `setFetching(false)` on the
    // stale path with no rejection handling for the newest load, which
    // strands the spinner until the next filter or page change.
    const apiFetchMock = setupApiFetch([]);
    const base = apiFetchMock.getMockImplementation()!;
    const deferred: { resolve: (v: unknown) => void; reject: (e: unknown) => void }[] = [];
    let gate = false;
    apiFetchMock.mockImplementation(async (url: string) => {
      if (gate && url.startsWith("/api/v1/transactions?")) {
        return new Promise((resolve, reject) => deferred.push({ resolve, reject })) as never;
      }
      return base(url);
    });

    renderWithSWR(<TransactionsPage />);
    const select = await screen.findByLabelText("Filter by account");
    await waitFor(() => expect(select).toHaveTextContent("Checking B"));
    await waitFor(() => expect(screen.queryByRole("status", { name: "Loading" })).toBeNull());

    gate = true;
    fireEvent.change(select, { target: { value: "100" } });
    await waitFor(() => expect(deferred).toHaveLength(1));
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();

    window.dispatchEvent(new Event("pfv:transaction-added"));
    await waitFor(() => expect(deferred).toHaveLength(2));

    deferred[0].resolve({ items: [makeTx({ id: 1, description: "Stale rows" })], total: 1 });
    await new Promise((r) => setTimeout(r, 20));
    deferred[1].reject(new Error("boom"));

    await screen.findByTestId("transactions-refresh-error");
    await waitFor(() => expect(screen.queryByRole("status", { name: "Loading" })).toBeNull());
    expect(screen.queryAllByText("Stale rows")).toHaveLength(0);
  });

  it("hides the Reset button once the account filter is cleared back to All", async () => {
    // FENCE. Kills: `[] !== []` in isDefault (a cleared select holds a fresh
    // empty array, never the default's reference).
    const mock = setupApiFetch([]);
    renderWithSWR(<TransactionsPage />);
    const select = await screen.findByLabelText("Filter by account");
    await waitFor(() => expect(select).toHaveTextContent("Checking B"));
    expect(screen.queryByTestId("reset-sort-filters")).toBeNull();

    fireEvent.change(select, { target: { value: "100" } });
    await screen.findByTestId("reset-sort-filters");
    fireEvent.change(select, { target: { value: "" } });

    await waitFor(() => {
      expect(lastParams(mock).getAll("account_id")).toEqual([]);
      expect(screen.queryByTestId("reset-sort-filters")).toBeNull();
    });
  });

  describe("legacy ?category=<name> bookmarks", () => {
    const MASTER = { ...CATEGORY, id: 11, name: "Food", parent_id: null };
    const SUB = { ...CATEGORY, id: 12, name: "Food", parent_id: 99, parent_name: "Home" };
    const OTHER_MASTER = { ...CATEGORY, id: 13, name: "Food", parent_id: null };

    function withCategories(
      mock: ReturnType<typeof vi.mocked<typeof apiFetch>>,
      cats: unknown[],
    ) {
      const base = mock.getMockImplementation()!;
      mock.mockImplementation(async (url: string) =>
        url.startsWith("/api/v1/categories") ? (cats as never) : base(url),
      );
    }

    it("prefers the master when a sub shares its name", async () => {
      searchParamsState.value = new URLSearchParams("category=food");
      const mock = setupApiFetch([]);
      withCategories(mock, [SUB, MASTER]);

      renderWithSWR(<TransactionsPage />);

      await waitFor(() => {
        expect(lastParams(mock).getAll("category_id")).toEqual(["11"]);
      });
    });

    it("seeds nothing when the name is still ambiguous", async () => {
      // FENCE. Kills: first-match-by-name (it would silently pick one of two
      // unrelated categories).
      searchParamsState.value = new URLSearchParams("category=Food");
      const mock = setupApiFetch([]);
      withCategories(mock, [MASTER, OTHER_MASTER]);

      renderWithSWR(<TransactionsPage />);

      await screen.findByLabelText("Filter by category");
      await waitFor(() =>
        expect(screen.getByLabelText("Filter by category")).toHaveTextContent("Food"),
      );
      // Give the name-resolution effect a chance to (wrongly) seed a filter.
      await new Promise((r) => setTimeout(r, 50));
      expect(lastParams(mock).getAll("category_id")).toEqual([]);
    });
  });
});
