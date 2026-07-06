import React from "react";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithSWR } from "../utils/render-with-swr";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import {
  FILTERS_KEY_TRANSACTIONS,
  SORT_KEY_TRANSACTIONS,
} from "@/lib/hooks/persisted-keys";

// Item 6 (sort + filter persistence) regression coverage. Verifies the
// transactions page hydrates from localStorage on mount, writes through on
// sort/filter changes, and resets both via the reset affordance.

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/transactions",
  useSearchParams: () => ({ get: () => null }),
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

const ACCT = {
  id: 100, name: "Checking", account_type_id: 1,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: true,
};

const CATEGORY = {
  id: 11, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};

function setupApiFetch() {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url.startsWith("/api/v1/transactions"))
      return { items: [], total: 0, limit: 25, offset: 0 } as never;
    return null as never;
  });
  return apiFetchMock;
}

function getHeader(name: string): HTMLElement {
  const buttons = screen
    .getAllByRole("button")
    .filter((b) => b.textContent?.startsWith(name));
  if (buttons.length === 0) {
    throw new Error(`No header button starting with "${name}"`);
  }
  return buttons[0];
}

async function awaitReady(
  mock: ReturnType<typeof vi.mocked<typeof apiFetch>>,
) {
  const required = ["Date", "Description", "Account", "Category", "Status", "Amount"];
  await waitFor(
    () => {
      const txCalls = mock.mock.calls.filter(
        (c) => typeof c[0] === "string" && (c[0] as string).startsWith("/api/v1/transactions"),
      ).length;
      if (txCalls < 1) {
        throw new Error("waiting for the /transactions fetch");
      }
      for (const name of required) {
        const buttons = screen
          .queryAllByRole("button")
          .filter((b) => b.textContent?.startsWith(name));
        if (buttons.length === 0) {
          throw new Error(`header not yet visible: ${name}`);
        }
      }
    },
    { timeout: 4000 },
  );
}

describe("TransactionsPage - persisted sort and filters (item 6)", () => {
  const useAuthMock = vi.mocked(useAuth);

  beforeEach(() => {
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

  it("writes the sort selection to localStorage when a column header is clicked", async () => {
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await awaitReady(mock);

    fireEvent.click(getHeader("Description"));
    await waitFor(() => {
      const stored = window.localStorage.getItem(SORT_KEY_TRANSACTIONS);
      expect(stored).not.toBeNull();
      expect(JSON.parse(stored!)).toEqual({ field: "description", dir: "asc" });
    });
  });

  it("rehydrates sort from localStorage on mount (survives navigate-away)", async () => {
    window.localStorage.setItem(
      SORT_KEY_TRANSACTIONS,
      JSON.stringify({ field: "amount", dir: "asc" }),
    );
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await awaitReady(mock);

    // Active sort renders an indicator suffix on the matching header. Default
    // (amount-asc here) means the Amount column is decorated and Date is not.
    const amountText = getHeader("Amount").textContent ?? "";
    const dateText = getHeader("Date").textContent ?? "";
    expect(amountText.length).toBeGreaterThan("Amount".length);
    expect(dateText).toBe("Date");
  });

  it("Reset filters and sort button is hidden when defaults are active", async () => {
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await awaitReady(mock);

    expect(screen.queryByTestId("reset-sort-filters")).toBeNull();
  });

  it("Reset filters and sort button appears when sort differs from default and clears persistence on click", async () => {
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await awaitReady(mock);

    fireEvent.click(getHeader("Description"));
    await waitFor(() => {
      const stored = window.localStorage.getItem(SORT_KEY_TRANSACTIONS);
      expect(stored).not.toBeNull();
      expect(JSON.parse(stored!)).toEqual({ field: "description", dir: "asc" });
    });

    const resetBtn = await screen.findByTestId("reset-sort-filters");
    expect(resetBtn).toBeInTheDocument();

    fireEvent.click(resetBtn);
    await waitFor(() => {
      expect(window.localStorage.getItem(SORT_KEY_TRANSACTIONS)).toBeNull();
    });
    // After reset, the Date header is the active default sort and renders
    // its indicator suffix; Description is plain again.
    const dateText = getHeader("Date").textContent ?? "";
    const descText = getHeader("Description").textContent ?? "";
    expect(dateText.length).toBeGreaterThan("Date".length);
    expect(descText).toBe("Description");
    // Reset button is hidden again now that defaults are restored.
    expect(screen.queryByTestId("reset-sort-filters")).toBeNull();
  });

  it("rehydrates filterSearch from localStorage on mount", async () => {
    window.localStorage.setItem(
      FILTERS_KEY_TRANSACTIONS,
      JSON.stringify({
        filterAccount: "",
        filterCategory: "",
        filterType: "",
        filterStatus: "",
        filterDateFrom: "",
        filterDateTo: "",
        filterSearch: "rent",
        filterPeriod: "",
      }),
    );
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await awaitReady(mock);

    const search = screen.getByLabelText("Search transactions") as HTMLInputElement;
    expect(search.value).toBe("rent");
    // Reset button visible because filters differ from defaults.
    expect(screen.getByTestId("reset-sort-filters")).toBeInTheDocument();
  });

  it("Reset clears persisted filters too", async () => {
    window.localStorage.setItem(
      FILTERS_KEY_TRANSACTIONS,
      JSON.stringify({
        filterAccount: "",
        filterCategory: "",
        filterType: "expense",
        filterStatus: "",
        filterDateFrom: "",
        filterDateTo: "",
        filterSearch: "",
        filterPeriod: "",
      }),
    );
    const mock = setupApiFetch();
    renderWithSWR(<TransactionsPage />);
    await awaitReady(mock);

    const resetBtn = screen.getByTestId("reset-sort-filters");
    fireEvent.click(resetBtn);

    await waitFor(() => {
      expect(window.localStorage.getItem(FILTERS_KEY_TRANSACTIONS)).toBeNull();
    });
    const typeSelect = screen.getByLabelText("Filter by type") as HTMLSelectElement;
    expect(typeSelect.value).toBe("");
  });
});
