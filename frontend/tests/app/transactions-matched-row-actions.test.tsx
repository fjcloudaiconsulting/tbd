/**
 * TBD-292 / 294 / 295 — what a matched row offers, and what a delete says.
 *
 * A reconcile-matched row carries a NON-NULL `linked_transaction_id` with a
 * NULL `linked_account_name` (`_apply_match` writes the link ONE-WAY, so the
 * server never populates the partner's account name). Three things follow:
 *
 *  - "Make recurring" must NOT be offered. `promote_to_recurring` refuses ANY
 *    linked row, so the checkbox is an action the server always rejects. It
 *    was masked until now by the edit itself returning 409 (TBD-292); that
 *    409 is gone, so the hole is live.
 *  - A `?transaction_id=` deep link that lands off-page must SAY so. The
 *    matched badge now sends users through that mechanism from a filtered
 *    list, where a miss is the common case, and the effect used to `return`
 *    silently — indistinguishable from a dead link.
 *  - Deleting a canonical row demotes its matched duplicate to REJECTED
 *    (TBD-294). That is irreversible through the API, so the UI must say it.
 *
 * ⚠ jsdom renders BOTH the desktop grid and the mobile card layout — the `md:`
 * breakpoint is CSS-only. Every assertion here therefore counts across both,
 * because the mobile slot is the one this repo has historically missed.
 */
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

const searchParamsState = vi.hoisted(() => ({ value: new URLSearchParams() }));

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

// Distinct ids everywhere: a fixture set where every id is 1 cannot tell a
// correct lookup from a wrong one.
const ACCT_CHECKING = {
  id: 301, name: "Checking A", account_type_id: 4,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: true,
};

const ACCT_SAVINGS = {
  id: 302, name: "Savings B", account_type_id: 5,
  account_type_name: "Savings", account_type_slug: "savings",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: false,
};

const CATEGORY = {
  id: 211, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};

type Tx = {
  id: number;
  account_id: number;
  account_name: string;
  category_id: number;
  category_name: string;
  description: string;
  amount: number;
  type: "income" | "expense";
  status: "settled" | "pending";
  linked_transaction_id: number | null;
  linked_account_name: string | null;
  recurring_id: number | null;
  date: string;
  settled_date: string | null;
  is_imported: boolean;
  tags: { id: number; name: string }[];
};

function makeTx(over: Partial<Tx> & { id: number; description: string }): Tx {
  return {
    account_id: ACCT_CHECKING.id,
    account_name: ACCT_CHECKING.name,
    category_id: CATEGORY.id,
    category_name: CATEGORY.name,
    amount: 41.25,
    type: "expense",
    status: "settled",
    linked_transaction_id: null,
    linked_account_name: null,
    recurring_id: null,
    date: "2026-05-04",
    settled_date: "2026-05-04",
    is_imported: false,
    tags: [],
    ...over,
  } as Tx;
}

const MATCHED = makeTx({
  id: 9001,
  description: "Matched supermarket run",
  linked_transaction_id: 9500,
  linked_account_name: null,
});

const ORDINARY = makeTx({
  id: 9002,
  description: "Ordinary bakery run",
});

// A genuine transfer leg: BOTH fields set.
const TRANSFER_LEG = makeTx({
  id: 9003,
  description: "Genuine transfer leg",
  linked_transaction_id: 9004,
  linked_account_name: ACCT_SAVINGS.name,
});

function setupApiFetch(txs: Tx[], overrides: Record<string, unknown> = {}) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string, opts?: RequestInit) => {
    for (const [prefix, value] of Object.entries(overrides)) {
      if (url.startsWith(prefix)) return value as never;
    }
    if (url.startsWith("/api/v1/accounts")) return [ACCT_CHECKING, ACCT_SAVINGS] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (opts?.method === "DELETE") return { deleted: true, demoted_ids: [] } as never;
    if (url.startsWith("/api/v1/transactions"))
      return { items: txs, total: txs.length, limit: 25, offset: 0 } as never;
    return null as never;
  });
  return apiFetchMock;
}

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

afterEach(() => cleanup());

function recurringToggles(): HTMLElement[] {
  return screen.queryAllByLabelText("Make recurring");
}

describe("TBD-295 — 'Make recurring' on a linked row", () => {
  it("is NOT offered on a matched row, in EITHER slot", async () => {
    // RED against `main`: the checkbox is gated on `!editPartner`, which
    // startEdit leaves null for a matched row (it only hydrates a MUTUAL
    // pair), so both slots rendered it.
    setupApiFetch([MATCHED]);
    render(<TransactionsPage />);
    await screen.findAllByText(MATCHED.description);

    fireEvent.click(screen.getAllByLabelText(`Edit: ${MATCHED.description}`)[0]);
    // The edit form is open in both layouts before we assert an absence.
    await screen.findByTestId(`edit-row-desktop-${MATCHED.id}`);

    expect(recurringToggles()).toHaveLength(0);
    expect(screen.queryByTestId(`edit-recurring-row-${MATCHED.id}`)).toBeNull();
    expect(
      screen.queryByTestId(`edit-recurring-row-mobile-${MATCHED.id}`),
    ).toBeNull();
  });

  it("IS still offered on an ordinary row, in BOTH slots (over-reach fence)", async () => {
    // Without this, "never render the checkbox" passes the fence above.
    setupApiFetch([ORDINARY]);
    render(<TransactionsPage />);
    await screen.findAllByText(ORDINARY.description);

    fireEvent.click(screen.getAllByLabelText(`Edit: ${ORDINARY.description}`)[0]);
    await screen.findByTestId(`edit-row-desktop-${ORDINARY.id}`);

    expect(recurringToggles()).toHaveLength(2);
    expect(screen.getByTestId(`edit-recurring-row-${ORDINARY.id}`)).toBeTruthy();
    expect(
      screen.getByTestId(`edit-recurring-row-mobile-${ORDINARY.id}`),
    ).toBeTruthy();
  });

  it("is NOT offered on a real transfer leg either (unchanged)", async () => {
    setupApiFetch([TRANSFER_LEG]);
    render(<TransactionsPage />);
    await screen.findAllByText(TRANSFER_LEG.description);

    fireEvent.click(
      screen.getAllByLabelText(`Edit: ${TRANSFER_LEG.description}`)[0],
    );
    await screen.findByTestId(`edit-row-desktop-${TRANSFER_LEG.id}`);

    expect(recurringToggles()).toHaveLength(0);
  });
});

describe("TBD-295 — off-page deep link", () => {
  it("surfaces a message when the target is not on the page", async () => {
    // RED against `main`: the effect returns silently, so following the
    // matched badge from a filtered list looks like a dead link.
    searchParamsState.value = new URLSearchParams("transaction_id=9500");
    setupApiFetch([MATCHED]);
    render(<TransactionsPage />);
    await screen.findAllByText(MATCHED.description);

    const notice = await screen.findByTestId("deep-link-miss");
    expect(notice.textContent).toMatch(/isn't on this page/i);
    expect(notice.textContent).toMatch(/clear your filters/i);
  });

  it("does NOT surface it when the target IS on the page (over-reach fence)", async () => {
    searchParamsState.value = new URLSearchParams(`transaction_id=${MATCHED.id}`);
    setupApiFetch([MATCHED]);
    render(<TransactionsPage />);
    await screen.findByTestId(`tx-row-desktop-${MATCHED.id}`);

    await waitFor(() => {
      expect(screen.queryByTestId("deep-link-miss")).toBeNull();
    });
  });

  it("does NOT flash the message while the list request is still in flight", async () => {
    // The OTHER side of the boundary, and a boundary pinned from one side is
    // not pinned. Before the list lands, `transactions` is legitimately empty
    // and EVERY deep link looks like a miss. Kills dropping the `!fetching`
    // term from the derived flag.
    searchParamsState.value = new URLSearchParams("transaction_id=9500");
    let releaseList: (v: unknown) => void = () => {};
    const listPending = new Promise((resolve) => {
      releaseList = resolve;
    });
    const mock = vi.mocked(apiFetch);
    mock.mockReset();
    mock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/accounts")) return [ACCT_CHECKING, ACCT_SAVINGS] as never;
      if (url.startsWith("/api/v1/categories")) return [CATEGORY] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
      if (url.startsWith("/api/v1/transactions")) {
        await listPending;
        return { items: [MATCHED], total: 1, limit: 25, offset: 0 } as never;
      }
      return null as never;
    });

    render(<TransactionsPage />);
    // Request issued, not yet resolved: silence, not a false "not on this page".
    await waitFor(() => {
      expect(
        mock.mock.calls.some(
          ([u]) => typeof u === "string" && u.startsWith("/api/v1/transactions?"),
        ),
      ).toBe(true);
    });
    expect(screen.queryByTestId("deep-link-miss")).toBeNull();

    releaseList(null);
    // Once the page has landed and really does not contain 9500, it speaks.
    expect(await screen.findByTestId("deep-link-miss")).toBeTruthy();
  });

  it("does NOT surface it when there is no deep link at all", async () => {
    setupApiFetch([MATCHED]);
    render(<TransactionsPage />);
    await screen.findAllByText(MATCHED.description);

    expect(screen.queryByTestId("deep-link-miss")).toBeNull();
  });
});

describe("TBD-294 — the delete says what it did", () => {
  it("reports a demoted duplicate after a single delete", async () => {
    const mock = setupApiFetch([ORDINARY]);
    mock.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url.startsWith("/api/v1/accounts")) return [ACCT_CHECKING, ACCT_SAVINGS] as never;
      if (url.startsWith("/api/v1/categories")) return [CATEGORY] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
      if (opts?.method === "DELETE")
        return { deleted: true, demoted_ids: [9001] } as never;
      if (url.startsWith("/api/v1/transactions"))
        return { items: [ORDINARY], total: 1, limit: 25, offset: 0 } as never;
      return null as never;
    });
    render(<TransactionsPage />);
    await screen.findAllByText(ORDINARY.description);

    fireEvent.click(screen.getAllByLabelText(`Delete: ${ORDINARY.description}`)[0]);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    const notice = await screen.findByTestId("transactions-notice");
    expect(notice.textContent).toMatch(/1 matched duplicate was marked rejected/i);
    expect(notice.textContent).toMatch(/balances or reports/i);
  });

  it("says nothing when nothing was demoted (over-reach fence)", async () => {
    // Without this, hard-coding the banner passes the fence above.
    setupApiFetch([ORDINARY]);
    render(<TransactionsPage />);
    await screen.findAllByText(ORDINARY.description);

    fireEvent.click(screen.getAllByLabelText(`Delete: ${ORDINARY.description}`)[0]);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(vi.mocked(apiFetch).mock.calls.some(
        ([, o]) => (o as RequestInit | undefined)?.method === "DELETE",
      )).toBe(true);
    });
    expect(screen.queryByTestId("transactions-notice")).toBeNull();
  });
});
