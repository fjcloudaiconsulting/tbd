import React from "react";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithSWR } from "../utils/render-with-swr";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
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
};

function makeTx(over: Partial<Tx> = {}): Tx {
  return {
    id: 1,
    account_id: ACCT_A.id,
    account_name: ACCT_A.name,
    category_id: CATEGORY_GROCERIES.id,
    category_name: CATEGORY_GROCERIES.name,
    description: "Coffee",
    amount: 12.5,
    type: "expense",
    status: "settled",
    linked_transaction_id: null,
    linked_account_name: null,
    recurring_id: null,
    date: "2026-05-01",
    settled_date: null,
    is_imported: false,
    ...over,
  };
}

type Series = {
  id: number;
  is_active: boolean;
  occurrence_count: number | null;
  occurrences_elapsed: number;
};

// An open-ended, active series: the only shape that existed before TBD-275.
function makeSeries(id: number, over: Partial<Series> = {}): Series {
  return { id, is_active: true, occurrence_count: null, occurrences_elapsed: 0, ...over };
}

// `series` defaults to one running series per recurring row, so the TBD-277
// tests keep describing a series that genuinely is running.
function setupApiFetch(txs: Tx[], series?: Series[]) {
  const templates =
    series ??
    txs.flatMap((t) => (t.recurring_id === null ? [] : [makeSeries(t.recurring_id)]));
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/v1/recurring" && method === "GET") return templates as never;
    if (url.startsWith("/api/v1/accounts")) return [ACCT_A] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url.startsWith("/api/v1/transactions") && method === "GET")
      return { items: txs, total: txs.length, limit: 25, offset: 0 } as never;
    return null as never;
  });
}

beforeEach(() => {
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

/**
 * The whole recurring block renders TWICE, once per layout, and the two copies
 * are maintained by hand. TBD-301 shipped a fix into one of them and missed the
 * other, so every assertion here is made in BOTH trees.
 *
 * ⚠ Do NOT reach for either tree positionally. `getAllBy*` groups matches by
 * matching STRATEGY, not document order (this repo's TBD-313 lesson), so which
 * index means which tree is control-dependent. Scope by the container testid,
 * which cannot drift.
 */
function tree(id: number, mobile: boolean) {
  return within(
    screen.getByTestId(
      mobile ? `edit-recurring-row-mobile-${id}` : `edit-recurring-row-${id}`,
    ),
  );
}

const HINT_COPY =
  "Editing or deleting this occurrence leaves the series running. Stop the whole series on the Recurring page.";

async function openEdit(tx: Tx, series?: Series[]) {
  setupApiFetch([tx], series);
  renderWithSWR(<TransactionsPage />);
  await waitForStableTxList();
  fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);
  await screen.findByTestId(`edit-recurring-row-${tx.id}`);
}

describe("TransactionsPage — recurring series pointer (TBD-277)", () => {
  it("recurring row edit form: points at the series, in BOTH render trees", async () => {
    const tx = makeTx({ id: 201, description: "Netflix", recurring_id: 7 });
    await openEdit(tx);

    for (const mobile of [false, true]) {
      const suffix = mobile ? "mobile-201" : "201";
      const hint = await tree(201, mobile).findByTestId(
        `edit-recurring-series-hint-${suffix}`,
      );
      expect(hint, mobile ? "mobile" : "desktop").toHaveTextContent(HINT_COPY);
    }
  });

  it("the pointer is a link to the recurring page, in BOTH render trees", async () => {
    const tx = makeTx({ id: 202, description: "Spotify", recurring_id: 9 });
    await openEdit(tx);

    for (const mobile of [false, true]) {
      const link = await tree(202, mobile).findByRole("link", {
        name: "Recurring page",
      });
      expect(link, mobile ? "mobile" : "desktop").toHaveAttribute(
        "href",
        "/recurring",
      );
    }
  });

  it("ORDINARY row (recurring_id === null): no pointer, in BOTH render trees", async () => {
    // ⭐ FENCE. This is the assertion that makes the two above non-vacuous.
    // Without it they only say "the string renders", which stays GREEN against
    // a component that renders the hint unconditionally -- and an unconditional
    // hint is actively wrong here: it would tell someone editing a one-off
    // coffee that they have a series to go stop.
    const tx = makeTx({ id: 203, description: "One-off coffee", recurring_id: null });
    await openEdit(tx);

    for (const mobile of [false, true]) {
      const suffix = mobile ? "mobile-203" : "203";
      const scope = tree(203, mobile);
      const where = mobile ? "mobile" : "desktop";
      expect(
        scope.queryByTestId(`edit-recurring-series-hint-${suffix}`),
        where,
      ).toBeNull();
      // Belt and braces: not the testid, and not the copy or the link either,
      // so renaming the testid cannot quietly retire this fence.
      expect(scope.queryByText(/Stop the whole series/i), where).toBeNull();
      expect(
        scope.queryByRole("link", { name: "Recurring page" }),
        where,
      ).toBeNull();
    }

    // CONTROL. The absence above must be the CONDITION doing the work, not a
    // container that failed to render or a query that can never match. The
    // non-recurring branch of the very same block is present and populated, so
    // both trees really did render and really are observable here.
    for (const mobile of [false, true]) {
      expect(
        tree(203, mobile).getByLabelText("Make recurring"),
        mobile ? "mobile" : "desktop",
      ).toBeInTheDocument();
    }
  });

  it("the pointer belongs to the edit form, not the row: absent until Edit is clicked", async () => {
    const tx = makeTx({ id: 204, description: "Gym", recurring_id: 12 });
    setupApiFetch([tx]);
    renderWithSWR(<TransactionsPage />);
    await waitForStableTxList();

    expect(screen.queryByText(/Stop the whole series/i)).toBeNull();

    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);
    await screen.findByTestId("edit-recurring-row-204");
    expect(
      (await screen.findAllByText(/Stop the whole series/i)).length,
      "one per render tree",
    ).toBe(2);
  });
});

/**
 * TBD-318. The pointer says the series is still RUNNING, so it must not render
 * for a series that is not. The transaction row carries only `recurring_id`;
 * the state lives on the template.
 *
 * Only two non-running states are reachable with the row still linked:
 * - an instalment series whose count is spent (TBD-275 keeps `is_active` true
 *   and keeps `recurring_id` on every row, deliberately), and
 * - `is_active: false` written through `PUT /recurring/{id}`. The UI's own Stop
 *   cannot produce it: `stop_recurring` NULLs `recurring_id` on every surviving
 *   row in the same commit, so the pointer never renders there at all.
 */
describe("TransactionsPage — series pointer only for a running series (TBD-318)", () => {
  // Absence is only meaningful once the series has been fetched and applied;
  // before that the pointer is absent for every row, running or not.
  async function seriesSettled() {
    await waitFor(() =>
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith("/api/v1/recurring"),
    );
    await new Promise((r) => setTimeout(r, 0));
  }

  function expectNoPointer(id: number) {
    for (const mobile of [false, true]) {
      const suffix = mobile ? `mobile-${id}` : `${id}`;
      const scope = tree(id, mobile);
      const where = mobile ? "mobile" : "desktop";
      expect(scope.queryByTestId(`edit-recurring-series-hint-${suffix}`), where).toBeNull();
      expect(scope.queryByText(/Stop the whole series/i), where).toBeNull();
      // CONTROL: the recurring branch itself rendered, so the absence is the
      // new condition and not the whole block being missing.
      expect(scope.getByTestId(`edit-recurring-chip-${suffix}`), where).toBeInTheDocument();
    }
  }

  // FENCE, two rows: spent exactly (3 of 3) and over-delivered (4 of 3, a plan
  // shortened below what it already delivered). The second kills an
  // `elapsed === count` spelling, which is green on the first.
  it.each([
    [3, 3],
    [4, 3],
  ])("instalment series %i of %i: no pointer, in BOTH render trees", async (elapsed, count) => {
    const tx = makeTx({ id: 301, description: "Sofa", recurring_id: 21 });
    await openEdit(tx, [makeSeries(21, { occurrence_count: count, occurrences_elapsed: elapsed })]);
    await seriesSettled();
    expectNoPointer(301);
  });

  // FENCE: kills a condition that reads only instalment exhaustion.
  it("inactive series: no pointer, in BOTH render trees", async () => {
    const tx = makeTx({ id: 302, description: "Paused gym", recurring_id: 22 });
    await openEdit(tx, [makeSeries(22, { is_active: false })]);
    await seriesSettled();
    expectNoPointer(302);
  });

  // FENCE: kills "hide every instalment series" (`occurrence_count != null`),
  // which passes both fences above.
  it("instalment series with occurrences left (2 of 3): pointer shown, in BOTH render trees", async () => {
    const tx = makeTx({ id: 303, description: "Laptop", recurring_id: 23 });
    await openEdit(tx, [makeSeries(23, { occurrence_count: 3, occurrences_elapsed: 2 })]);
    for (const mobile of [false, true]) {
      const suffix = mobile ? "mobile-303" : "303";
      expect(
        await tree(303, mobile).findByTestId(`edit-recurring-series-hint-${suffix}`),
        mobile ? "mobile" : "desktop",
      ).toHaveTextContent(HINT_COPY);
    }
  });

  // FENCE: a series the client cannot see (the list failed, or the template is
  // not in it) must not be claimed as running. Kills `!series || running`.
  it("series not found: no pointer, in BOTH render trees", async () => {
    const tx = makeTx({ id: 304, description: "Unknown", recurring_id: 24 });
    await openEdit(tx, [makeSeries(99)]);
    await seriesSettled();
    expectNoPointer(304);
  });
});
