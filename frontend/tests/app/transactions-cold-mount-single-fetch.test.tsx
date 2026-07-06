/**
 * Transactions page — cold-mount single-fetch guard.
 *
 * Regression test for the double-fetch surfaced in the #519 SWR review:
 * ``loadTransactions`` carried ``periods`` in its dependency array, so it was
 * recreated the moment the billing-periods SWR request resolved. The mount
 * effect (which lists ``loadTransactions`` as a dep) then re-fired, issuing a
 * SECOND identical list request — once against the empty period fallback, then
 * again after periods loaded.
 *
 * A fresh SWR cache (``renderWithSWR``) is required to reproduce it: with the
 * suite's warm module cache periods are already resolved on mount, so the
 * loading -> resolved transition that triggers the second fetch never happens.
 *
 * The fix gates the initial list fetch until periods have SETTLED (resolved or
 * errored), so the list is fetched exactly once, after periods are available.
 */
import React from "react";

import { renderWithSWR, screen, waitFor } from "@/tests/utils/render-with-swr";
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

vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

import TransactionsPage from "@/app/transactions/page";

const USER = {
  id: 1, username: "user", email: "user@example.com",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const, org_id: 1, org_name: "Org",
  billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

// A single CLOSED billing period (end_date set) so the "Billing period" filter
// select renders once periods resolve — a deterministic DOM signal that the
// SWR loading -> resolved transition (the second-fetch trigger) has fired.
const CLOSED_PERIOD = { id: 5, start_date: "2026-04-01", end_date: "2026-04-30" };

const TX = {
  id: 1, account_id: 100, account_name: "Checking", category_id: 11,
  category_name: "Groceries", description: "Tx", amount: 100,
  type: "expense" as const, status: "settled" as const,
  linked_transaction_id: null, recurring_id: null,
  date: "2026-05-01", settled_date: null, is_imported: false,
};

function txListCallCount(): number {
  return vi.mocked(apiFetch).mock.calls.filter(([url]) =>
    typeof url === "string" && url.startsWith("/api/v1/transactions"),
  ).length;
}

beforeEach(() => {
  vi.mocked(useAuth).mockReturnValue({ user: USER, loading: false } as never);
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [] as never;
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [CLOSED_PERIOD] as never;
    if (url.startsWith("/api/v1/transactions"))
      return { items: [TX], total: 1, limit: 25, offset: 0 } as never;
    return null as never;
  });
});

describe("TransactionsPage cold mount", () => {
  it("fetches the transactions list exactly once after periods settle", async () => {
    renderWithSWR(<TransactionsPage />);

    // Periods resolving is what used to trigger the redundant second fetch.
    // The billing-period select only mounts once closed periods are present,
    // so awaiting it guarantees the loading -> resolved transition has run and
    // any second fetch has been dispatched by the mount effect.
    await screen.findByLabelText("Billing period");

    // Flush any trailing effect-driven fetch before asserting the final count.
    await waitFor(() => expect(txListCallCount()).toBeGreaterThan(0));

    expect(txListCallCount()).toBe(1);
  });
});
