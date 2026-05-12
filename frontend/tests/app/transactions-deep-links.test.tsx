import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

const searchParamsState = vi.hoisted(() => ({
  value: new URLSearchParams(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/transactions",
  useSearchParams: () => ({
    get: (key: string) => searchParamsState.value.get(key),
  }),
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
    if (url.startsWith("/api/v1/transactions")) return txs as never;
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

    render(<TransactionsPage />);

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

    render(<TransactionsPage />);

    const desktopRow = await screen.findByTestId("tx-row-desktop-42");
    const mobileRow = await screen.findByTestId("tx-row-mobile-42");

    expect(desktopRow.className).toContain("ring-accent");
    expect(mobileRow.className).toContain("ring-accent");
    await waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "auto" });
    });
  });
});
