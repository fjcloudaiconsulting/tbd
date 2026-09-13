/**
 * TBD-272: DashboardDataProvider's pendingByAccount (the custom dashboard's
 * accounts widget) ignores reverted rows. The legacy page has its own copy,
 * fenced in tests/app/dashboard-pending-strip-excludes-reverted.test.tsx.
 *
 * FENCE. Kills a reduce that sums every pending row: -50 + -70 = -120 instead
 * of -50. Amounts are asymmetric on purpose.
 */
import { screen, waitFor } from "@testing-library/react";

import { renderWithSWR } from "@/tests/utils/render-with-swr";
import { DashboardDataProvider, useDashboard } from "@/components/dashboard/DashboardDataProvider";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import * as pagination from "@/lib/pagination";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/lib/pagination", async () => {
  const actual = await vi.importActual<typeof import("@/lib/pagination")>("@/lib/pagination");
  return { ...actual, fetchAll: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return { ...actual, useAuth: vi.fn() };
});

const ACCT = {
  id: 1, name: "Checking", account_type_id: 1, account_type_name: "Checking",
  account_type_slug: "checking", balance: 1000, currency: "EUR", is_active: true,
  close_day: null, is_default: true,
};

function Consumer() {
  const ctx = useDashboard();
  return (
    <div>
      <span data-testid="loading">{String(ctx.loading)}</span>
      <span data-testid="pending-acct-1">{ctx.pendingByAccount[1] ?? 0}</span>
    </div>
  );
}

describe("DashboardDataProvider — pendingByAccount ignores reverted rows (TBD-272)", () => {
  it("totals only the non-reverted pending rows", async () => {
    window.localStorage.clear();
    vi.mocked(useAuth).mockReturnValue({ user: { billing_cycle_day: 1 }, loading: false } as never);
    vi.mocked(apiFetch).mockImplementation((async (url: string) => {
      if (url.startsWith("/api/v1/accounts")) return [ACCT];
      if (url.startsWith("/api/v1/settings/billing-periods"))
        return [{ id: 2, start_date: "2026-05-01", end_date: null }];
      if (url.startsWith("/api/v1/settings/billing-period"))
        return { id: 2, start_date: "2026-05-01", end_date: null };
      if (url.startsWith("/api/v1/settings/billing-cycle")) return { billing_cycle_day: 1 };
      if (url.startsWith("/api/v1/forecast/account-balances"))
        return { period_start: "2026-05-01", period_end: "2026-05-31", totals: [], accounts: [] };
      if (url.startsWith("/api/v1/transactions/spending-by-category"))
        return { period_start: "2026-05-01", period_end: "2026-05-31", executed_expense: "0", categories: [] };
      if (url.startsWith("/api/v1/transactions")) return { items: [], total: 0 };
      if (url.startsWith("/api/v1/budgets")) return [];
      return null;
    }) as never);
    vi.mocked(pagination.fetchAll).mockResolvedValue([
      { id: 10, account_id: 1, type: "expense", amount: "50", status: "pending", is_reverted: false },
      { id: 11, account_id: 1, type: "expense", amount: "70", status: "pending", is_reverted: true },
    ] as never);

    renderWithSWR(
      <DashboardDataProvider>
        <Consumer />
      </DashboardDataProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
    await waitFor(() => expect(screen.getByTestId("pending-acct-1").textContent).toBe("-50"));
  });
});
