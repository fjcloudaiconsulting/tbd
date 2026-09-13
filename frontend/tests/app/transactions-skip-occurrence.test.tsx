/**
 * TBD-272 / TBD-273 on the transactions page.
 *
 *  F9  "Skip this occurrence" renders in BOTH edit trees (scoped by testid) for
 *      a pending, recurring, unlinked, non-reverted row; not for a settled,
 *      linked or reverted row; and NOT when a settled row's status select is
 *      switched to pending. After success the form closes and the list
 *      refetches.
 *      Kills: gating on `editStatus` instead of the row, gating on
 *      `is_imported`, and no refetch.
 *  F10 The "Amount differs" badge renders in both row trees exactly when
 *      `differs_from_series` is true.
 *
 * Fixtures are typed as the shared `Transaction`, so `tsc --noEmit` gates the
 * new wire field too.
 */
import React from "react";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithSWR } from "../utils/render-with-swr";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { ApiResponseError, apiFetch } from "@/lib/api";
import { formatMoney } from "@/lib/format";
import type { RecurringTransaction, Transaction } from "@/lib/types";
import { waitForStableTxList } from "../utils/wait-for-stable-tx-list";

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

const USER = {
  id: 7, username: "user", email: "user@example.com",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const, org_id: 3, org_name: "Org",
  billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

const ACCT = {
  id: 301, name: "Checking A", account_type_id: 4,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true, close_day: null, is_default: true,
};

const CAT = {
  id: 211, name: "Streaming", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "streaming", is_system: false, transaction_count: 0,
};

function makeTx(over: Partial<Transaction> & { id: number; description: string }): Transaction {
  return {
    account_id: ACCT.id, account_name: ACCT.name,
    category_id: CAT.id, category_name: CAT.name,
    amount: 15.99, type: "expense", status: "pending",
    linked_transaction_id: null, linked_account_name: null,
    recurring_id: 31, date: "2026-09-05", settled_date: null,
    is_imported: false, is_manual_adjustment: false, is_reverted: false,
    differs_from_series: false, tags: [],
    ...over,
  };
}

function series(over: Partial<RecurringTransaction> = {}): RecurringTransaction {
  return {
    id: 31, account_id: ACCT.id, account_name: ACCT.name, category_id: CAT.id,
    category_name: CAT.name, description: "Netflix", amount: 15.99, type: "expense",
    frequency: "monthly", next_due_date: "2026-10-05", auto_settle: false,
    is_active: true, occurrence_count: null, occurrences_elapsed: 0, ...over,
  };
}

function setupApi(
  txs: Transaction[],
  opts: { series?: RecurringTransaction; skip?: () => unknown } = {},
) {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/v1/recurring" && method === "GET") return [opts.series ?? series()] as never;
    if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
    if (url.startsWith("/api/v1/categories")) return [CAT] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (method === "POST" && /^\/api\/v1\/transactions\/\d+\/skip$/.test(url))
      return (opts.skip ? opts.skip() : { id: 1 }) as never;
    if (url.startsWith("/api/v1/transactions") && method === "GET")
      return { items: txs, total: txs.length, limit: 25, offset: 0 } as never;
    return null as never;
  });
}

beforeEach(() => {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never, loading: false, needsSetup: false,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  });
});

const calls = () =>
  vi.mocked(apiFetch).mock.calls.map(([url, init]) => ({
    key: `${(init as RequestInit | undefined)?.method ?? "GET"} ${url}`,
    init: init as RequestInit | undefined,
  }));

async function openEdit(tx: Transaction) {
  renderWithSWR(<TransactionsPage />);
  await waitForStableTxList();
  fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);
  await waitFor(() =>
    expect(screen.getAllByRole("button", { name: /^Save$/ }).length).toBeGreaterThan(0),
  );
  return `Skip this occurrence: ${tx.description}`;
}

const skipIn = (testid: string, label: string) =>
  within(screen.getByTestId(testid)).queryByRole("button", { name: label });

function clickSkip(testid: string, label: string) {
  const btn = skipIn(testid, label);
  expect(btn, testid).not.toBeNull();
  fireEvent.click(btn!);
}

describe("transactions page: skip this occurrence (TBD-272)", () => {
  it.each([false, true])(
    "F9: shown in BOTH edit trees for a pending recurring row (is_imported=%s)",
    async (is_imported) => {
      const tx = makeTx({ id: 501, description: "Netflix", is_imported });
      setupApi([tx]);
      const label = await openEdit(tx);
      expect(skipIn("edit-recurring-row-501", label), "desktop").not.toBeNull();
      expect(skipIn("edit-recurring-row-mobile-501", label), "mobile").not.toBeNull();
    },
  );

  it("F9: NOT for a settled row, even after its status select is switched to pending", async () => {
    const tx = makeTx({ id: 502, description: "Spotify", status: "settled", settled_date: "2026-09-05" });
    setupApi([tx]);
    const label = await openEdit(tx);
    expect(skipIn("edit-recurring-row-502", label)).toBeNull();
    expect(skipIn("edit-recurring-row-mobile-502", label)).toBeNull();

    fireEvent.change(document.getElementById("edit-status-502")!, {
      target: { value: "pending" },
    });
    expect((document.getElementById("edit-status-502") as HTMLSelectElement).value).toBe("pending");
    expect(skipIn("edit-recurring-row-502", label)).toBeNull();
    expect(skipIn("edit-recurring-row-mobile-502", label)).toBeNull();
  });

  it.each([
    ["linked", { linked_transaction_id: 9500 }],
    ["reverted", { is_reverted: true }],
  ] as const)("F9: NOT for a %s pending recurring row", async (_name, over) => {
    const tx = makeTx({ id: 503, description: "Disney", ...over });
    setupApi([tx]);
    const label = await openEdit(tx);
    expect(screen.queryAllByRole("button", { name: label })).toHaveLength(0);
  });

  it("F9: confirm POSTs skip, closes the form, refetches, and announces it", async () => {
    const tx = makeTx({ id: 504, description: "Netflix" });
    setupApi([tx], { series: series({ occurrence_count: 12, occurrences_elapsed: 4 }) });
    const label = await openEdit(tx);
    clickSkip("edit-recurring-row-504", label);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Skip This Occurrence")).toBeInTheDocument();
    await waitFor(() =>
      expect(dialog.textContent).toContain("It still counts as 1 of the 12 payments."),
    );
    expect(dialog.textContent).toContain(`Skip "Netflix" on 2026-09-05 (-${formatMoney(15.99)})?`);
    expect(dialog.textContent).toContain("This can't be undone.");

    const confirm = within(dialog).getByRole("button", { name: "Skip" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await waitFor(() => expect(screen.getByTestId("transactions-notice")).toHaveTextContent('Skipped "Netflix" on 2026-09-05.'));
    const keys = calls().map((c) => c.key);
    const post = keys.indexOf("POST /api/v1/transactions/504/skip");
    expect(keys.filter((k) => k.endsWith("/skip"))).toHaveLength(1);
    expect(calls()[post].init?.body).toBeUndefined();
    expect(keys.slice(post + 1).some((k) => k.startsWith("GET /api/v1/transactions?"))).toBe(true);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("edit-recurring-row-504")).toBeNull();
  });

  it("F9: open-ended series has no payments line", async () => {
    const tx = makeTx({ id: 505, description: "Netflix" });
    setupApi([tx]);
    const label = await openEdit(tx);
    await waitFor(() => expect(calls().some((c) => c.key === "GET /api/v1/recurring")).toBe(true));
    clickSkip("edit-recurring-row-mobile-505", label);
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).not.toMatch(/payments/);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  });

  it("F9: a 409 shows the server message verbatim", async () => {
    const tx = makeTx({ id: 506, description: "Netflix" });
    setupApi([tx], {
      skip: () => {
        throw new ApiResponseError(409, "Only a pending occurrence can be skipped.");
      },
    });
    const label = await openEdit(tx);
    clickSkip("edit-recurring-row-506", label);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Skip" }));
    expect(await screen.findByText("Only a pending occurrence can be skipped.")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("transactions page: amount differs badge (TBD-273)", () => {
  it("F10: renders in BOTH row trees exactly when differs_from_series is true", async () => {
    const differs = makeTx({ id: 601, description: "Edited rent", differs_from_series: true });
    const same = makeTx({ id: 602, description: "Plain rent" });
    setupApi([differs, same]);
    renderWithSWR(<TransactionsPage />);
    await waitForStableTxList();

    for (const testid of ["amount-differs-badge-601", "amount-differs-badge-mobile-601"]) {
      const badge = screen.getByTestId(testid);
      expect(badge).toHaveTextContent("Amount differs");
      fireEvent.focus(badge);
      await waitFor(() => expect(badge.getAttribute("aria-describedby")).toBeTruthy());
      expect(document.getElementById(badge.getAttribute("aria-describedby")!)?.textContent).toContain(
        "This occurrence's amount is different from its recurring series. Later occurrences use the series amount.",
      );
      fireEvent.blur(badge);
    }
    expect(screen.queryByTestId("amount-differs-badge-602")).toBeNull();
    expect(screen.queryByTestId("amount-differs-badge-mobile-602")).toBeNull();
  });
});
