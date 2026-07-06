import React from "react";
import { render, screen, within } from "@testing-library/react";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

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

const ACCT_A = {
  id: 100, name: "Checking A", account_type_id: 1,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: true,
};

const CATEGORY_GROCERIES = {
  id: 11, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};

function makeTx(over: Partial<{
  id: number;
  date: string;
  settled_date: string | null;
  description: string;
  status: "settled" | "pending";
}> = {}) {
  return {
    id: 1,
    account_id: ACCT_A.id,
    account_name: ACCT_A.name,
    category_id: CATEGORY_GROCERIES.id,
    category_name: CATEGORY_GROCERIES.name,
    description: "Tx",
    amount: 100,
    type: "expense" as const,
    status: "settled" as const,
    linked_transaction_id: null,
    recurring_id: null,
    date: "2026-05-31",
    settled_date: "2026-06-15",
    is_imported: false,
    is_manual_adjustment: false,
    tags: [],
    ...over,
  };
}

function setupApiFetch(txs: ReturnType<typeof makeTx>[]) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT_A] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url.startsWith("/api/v1/transactions"))
      return { items: txs, total: txs.length, limit: 25, offset: 0 } as never;
    return null as never;
  });
}

describe("TransactionsPage — Settled date column (Task 8)", () => {
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

  it("renders a Settled header and shows both the original date and settled date for a GBLT row", async () => {
    setupApiFetch([makeTx({ id: 7, description: "GBLT", date: "2026-05-31", settled_date: "2026-06-15" })]);

    render(<TransactionsPage />);

    const row = await screen.findByTestId("tx-row-desktop-7");

    // Both the original date and the settled date are visible on the row.
    expect(within(row).getByTestId("settled-date-7")).toHaveTextContent("2026-06-15");
    expect(within(row).getAllByText("2026-05-31").length).toBeGreaterThan(0);
  });

  it("shows an em-dash placeholder in the Settled cell when settled_date is null", async () => {
    setupApiFetch([makeTx({ id: 8, description: "Pending", status: "pending", date: "2026-06-10", settled_date: null })]);

    render(<TransactionsPage />);

    const row = await screen.findByTestId("tx-row-desktop-8");
    const settledCell = within(row).getByTestId("settled-date-8");
    expect(settledCell).toHaveTextContent("—");
  });
});
