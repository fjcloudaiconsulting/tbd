import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";

import AccountsPage from "@/app/accounts/page";
import BudgetsPage from "@/app/budgets/page";
import ForecastPlansPage from "@/app/forecast-plans/page";
import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

// AppShell is heavy and depends on auth/permissions; the listener
// behaviour we're asserting lives in the page's own useEffect, not in
// the shell, so a dumb passthrough keeps the test focused.
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

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/transactions",
  useSearchParams: () => ({ get: () => null }),
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
  role: "owner" as const,
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
  allow_manual_balance_adjustment: false,
};

const ACCT = {
  id: 1,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Checking",
  account_type_slug: "checking",
  balance: 100,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const CAT = {
  id: 10,
  name: "Groceries",
  type: "expense" as const,
  parent_id: null,
  parent_name: null,
  description: null,
  slug: "groceries",
  is_system: false,
  transaction_count: 0,
};

const PERIOD_OPEN = {
  id: 1,
  start_date: "2026-05-01",
  end_date: null,
};

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
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

function dispatchTransactionAdded() {
  act(() => {
    window.dispatchEvent(new Event("pfv:transaction-added"));
  });
}

function countCalls(prefix: string): number {
  return vi
    .mocked(apiFetch)
    .mock.calls.filter((c) => typeof c[0] === "string" && (c[0] as string).startsWith(prefix))
    .length;
}

describe("Transactions page subscribes to pfv:transaction-added", () => {
  it("re-fetches transactions and refs after the event fires", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
      if (url.startsWith("/api/v1/categories")) return [CAT] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD_OPEN] as never;
      if (url.startsWith("/api/v1/transactions")) return [] as never;
      return null as never;
    });

    render(<TransactionsPage />);

    await waitFor(() => {
      expect(countCalls("/api/v1/transactions")).toBeGreaterThanOrEqual(1);
    });
    const initialTx = countCalls("/api/v1/transactions");
    const initialAccts = countCalls("/api/v1/accounts");

    dispatchTransactionAdded();

    await waitFor(() => {
      expect(countCalls("/api/v1/transactions")).toBeGreaterThan(initialTx);
      expect(countCalls("/api/v1/accounts")).toBeGreaterThan(initialAccts);
    });
  });

  it("shows the inline retry banner when a refresh call rejects", async () => {
    let txCalls = 0;
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
      if (url.startsWith("/api/v1/categories")) return [CAT] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD_OPEN] as never;
      if (url.startsWith("/api/v1/transactions")) {
        txCalls += 1;
        // First call (initial load) resolves; the second (post-event
        // refresh) rejects so we exercise the Promise.allSettled
        // surfacing path. The other allSettled member (refs) still runs
        // and resolves, asserting "the rest still ran" semantics.
        if (txCalls >= 2) throw new Error("backend hiccup");
        return [] as never;
      }
      return null as never;
    });

    render(<TransactionsPage />);

    await waitFor(() => {
      expect(countCalls("/api/v1/transactions")).toBe(1);
    });
    const acctsBefore = countCalls("/api/v1/accounts");

    dispatchTransactionAdded();

    await waitFor(() => {
      expect(screen.getByTestId("transactions-refresh-error")).toBeTruthy();
    });
    // Refs reload still ran, proving Promise.allSettled didn't short-circuit.
    expect(countCalls("/api/v1/accounts")).toBeGreaterThan(acctsBefore);
  });
});

describe("Accounts page subscribes to pfv:transaction-added", () => {
  it("calls reload after the event fires", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/account-types")) return [] as never;
      if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
      if (url.startsWith("/api/v1/transactions")) return [] as never;
      return null as never;
    });

    render(<AccountsPage />);

    await waitFor(() => {
      expect(countCalls("/api/v1/accounts")).toBeGreaterThanOrEqual(1);
    });
    const before = countCalls("/api/v1/accounts");

    dispatchTransactionAdded();

    await waitFor(() => {
      expect(countCalls("/api/v1/accounts")).toBeGreaterThan(before);
    });
  });

  it("surfaces the inline retry banner when reload rejects", async () => {
    let acctCalls = 0;
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/account-types")) return [] as never;
      if (url.startsWith("/api/v1/accounts")) {
        acctCalls += 1;
        if (acctCalls >= 2) throw new Error("nope");
        return [ACCT] as never;
      }
      if (url.startsWith("/api/v1/transactions")) return [] as never;
      return null as never;
    });

    render(<AccountsPage />);

    await waitFor(() => {
      expect(countCalls("/api/v1/accounts")).toBe(1);
    });

    dispatchTransactionAdded();

    await waitFor(() => {
      expect(screen.getByTestId("accounts-refresh-error")).toBeTruthy();
    });
  });
});

describe("Forecast Plans page subscribes to pfv:transaction-added", () => {
  it("re-fetches the plan after the event fires", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/settings/billing-periods/ensure-future")) return null as never;
      if (url.startsWith("/api/v1/categories")) return [CAT] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD_OPEN] as never;
      if (url.startsWith("/api/v1/forecast-plans")) {
        return {
          id: 1,
          billing_period_id: 1,
          period_start: "2026-05-01",
          period_end: null,
          status: "draft",
          total_planned_income: "0",
          total_planned_expense: "0",
          total_actual_income: "0",
          total_actual_expense: "0",
          items: [],
        } as never;
      }
      return null as never;
    });

    render(<ForecastPlansPage />);

    await waitFor(() => {
      expect(countCalls("/api/v1/forecast-plans")).toBeGreaterThanOrEqual(1);
    });
    const before = countCalls("/api/v1/forecast-plans");

    dispatchTransactionAdded();

    await waitFor(() => {
      expect(countCalls("/api/v1/forecast-plans")).toBeGreaterThan(before);
    });
  });
});

describe("Budgets page subscribes to pfv:transaction-added", () => {
  it("re-fetches budgets after the event fires", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/categories")) return [CAT] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD_OPEN] as never;
      if (url.startsWith("/api/v1/budgets")) return [] as never;
      return null as never;
    });

    render(<BudgetsPage />);

    await waitFor(() => {
      expect(countCalls("/api/v1/budgets")).toBeGreaterThanOrEqual(1);
    });
    const before = countCalls("/api/v1/budgets");

    dispatchTransactionAdded();

    await waitFor(() => {
      expect(countCalls("/api/v1/budgets")).toBeGreaterThan(before);
    });
  });

  it("surfaces the inline retry banner when reload rejects", async () => {
    let calls = 0;
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/categories")) return [CAT] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD_OPEN] as never;
      if (url.startsWith("/api/v1/budgets")) {
        calls += 1;
        if (calls >= 2) throw new Error("err");
        return [] as never;
      }
      return null as never;
    });

    render(<BudgetsPage />);

    await waitFor(() => {
      expect(countCalls("/api/v1/budgets")).toBe(1);
    });

    dispatchTransactionAdded();

    await waitFor(() => {
      expect(screen.getByTestId("budgets-refresh-error")).toBeTruthy();
    });
  });
});
