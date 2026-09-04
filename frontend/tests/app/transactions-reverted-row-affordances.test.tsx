/**
 * TBD-309 — a REVERTED row must say it counts toward nothing, and must not be
 * offered an action the server always refuses.
 *
 * A row in `skipped` or `rejected` had its amount pulled back out of
 * `accounts.balance` at a reconciliation transition and sits outside every
 * reportable aggregate. It is in the ledger counting toward nothing. Until this
 * ticket the transactions surface could not tell: the wire carried no signal,
 * so such a row rendered identically to an ordinary transaction AND was offered
 * a promote-to-recurring checkbox that ticks and then 400s — a live violation
 * of the standing TBD-289 rule, "no affordance is offered that the server will
 * refuse".
 *
 * ⚠ THE FIXTURES HERE ARE TYPED AS THE SHARED `Transaction`, NOT A LOCAL
 * `type Tx`. This is load-bearing and is the difference between a fence and a
 * decoration. Most sibling files in this directory declare their own local row
 * type, which means they compile — and pass — even if `lib/types.ts` never
 * receives the new field at all. Typing against the shared interface is what
 * makes `tsc --noEmit` a gate on the contract half of this ticket.
 *
 * Each fence names the wrong implementation it kills:
 *
 *  F1  A reverted row is NOT offered the promote checkbox, in BOTH slots.
 *      Kills: fixing the desktop render gate and forgetting the mobile twin —
 *      the slot this file's neighbours have repeatedly missed.
 *
 *  F2  An ordinary row IS still offered it, and so are the non-reverted
 *      reconciliation states. The over-reach fence. Without it, "never render
 *      the checkbox" passes F1.
 *      Kills: widening the predicate to `reconciliation_state !== "accepted"`,
 *      which would withhold on `edited` / `matched` / `unmatched` /
 *      `pending_review` — four states the server happily promotes.
 *
 *  F3  The submit path STILL issues the promote request for a promotable row.
 *      The over-reach control on the submit half: without it, making the
 *      submit guard unconditionally false passes every other fence here.
 *
 *  F3b The submit path REFUSES when the row turns reverted underneath an open
 *      edit form.
 *      Kills: leaving the submit path on the pre-TBD-309 predicate while
 *      fixing the two render gates — measured, that mutant leaves every other
 *      fence in this file green.
 *
 *      ⚠ An earlier revision of this file claimed this direction was NOT
 *      fenceable through the DOM, on the reasoning that the render gate makes
 *      its input unreachable. That reasoning was WRONG and the claim is
 *      withdrawn. `refreshAfterTransactionAdded` replaces the row set on the
 *      global add event WITHOUT closing the open form or clearing the tick, so
 *      a row can flip to reverted underneath a ticked checkbox — which is
 *      exactly the mid-edit revalidation the guard was described as defending
 *      against. A fence file that talks itself out of a reachable path is
 *      worse than one that never mentions it.
 *
 *  F4  A manual balance adjustment cannot REACH the promote checkbox at all.
 *      ⚠ REWRITTEN BY TBD-387, and the change of shape is the point. This
 *      fence used to open the edit form on an adjustment row and assert the
 *      checkbox was withheld inside it. TBD-387 closed the entry point — such
 *      a row is now offered no Edit affordance — so that form is unreachable
 *      and `openEditFor` has no button to click. The assertion below is
 *      strictly stronger and is the only form it can now take.
 *      Kills: re-opening the Edit affordance on an adjustment row (which is
 *      what would make the old body runnable again).
 *      ⚠ HONEST COVERAGE NOTE, so nobody cites this fence for more than it
 *      holds: `canPromoteToRecurring`'s `!tx.is_manual_adjustment` term is now
 *      UNREACHABLE through the UI — `is_manual_adjustment` is set at creation
 *      and never flips, so unlike the reverted case (F3b) no row can turn into
 *      an adjustment underneath an open form. That term survives as defence in
 *      depth for a future call site, NOT because this fence still exercises
 *      it. The server-side guard (transaction_service.py:921) is the real
 *      backstop. The affordance half is fenced in
 *      `transactions-manual-adjustment-affordances.test.tsx`.
 *
 *  F5  A COLLAPSED transfer pair renders NO excluded indicator, even when the
 *      surviving leg is reverted; an uncollapsed reverted row renders one.
 *      Kills: badging a row that stands in for two transactions using a flag
 *      that describes only one of them.
 *
 * Plus the copy and a11y fences inherited verbatim from TBD-289: the copy never
 * asserts a cause, and the explanation reaches the accessibility tree by TEXT
 * rather than only through `title`.
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { Transaction } from "@/lib/types";

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

const CATEGORY_GROCERIES = {
  id: 211, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};

function makeTx(
  over: Partial<Transaction> & { id: number; description: string },
): Transaction {
  return {
    account_id: ACCT_CHECKING.id,
    account_name: ACCT_CHECKING.name,
    category_id: CATEGORY_GROCERIES.id,
    category_name: CATEGORY_GROCERIES.name,
    amount: 41.25,
    type: "expense",
    status: "settled",
    linked_transaction_id: null,
    linked_account_name: null,
    recurring_id: null,
    date: "2026-05-04",
    settled_date: "2026-05-04",
    is_imported: false,
    is_manual_adjustment: false,
    is_reverted: false,
    tags: [],
    ...over,
  };
}

const REVERTED = makeTx({
  id: 9101,
  description: "Reverted duplicate charge",
  is_reverted: true,
});

const ORDINARY = makeTx({ id: 9102, description: "Ordinary bakery run" });

const ADJUSTMENT = makeTx({
  id: 9103,
  description: "Balance correction",
  is_manual_adjustment: true,
});

// A collapsed transfer pair whose SURVIVING leg is reverted. `linked_account_name`
// non-null is the mutuality-verified transfer signal (TBD-268).
const REVERTED_TRANSFER_LEG = makeTx({
  id: 9104,
  description: "Collapsed transfer leg",
  linked_transaction_id: 9500,
  linked_account_name: "Savings B",
  is_reverted: true,
});

/** Returns a setter so a test can change what the NEXT refetch returns, which
 * is what the mid-edit revalidation fence needs. */
function setupApiFetch(txs: Transaction[]) {
  let rows = txs;
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT_CHECKING] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url.startsWith("/api/v1/transactions"))
      return { items: rows, total: rows.length, limit: 25, offset: 0 } as never;
    return null as never;
  });
  return (next: Transaction[]) => {
    rows = next;
  };
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
 * jsdom renders BOTH the desktop grid and the mobile card layout (the `md:`
 * breakpoint is CSS-only), so every assertion names the slot explicitly.
 * A bare `queryAllByLabelText(...).toHaveLength(0)` cannot distinguish "both
 * slots suppressed" from "the query matched nothing for an unrelated reason".
 */
async function openEditFor(tx: Transaction) {
  await screen.findAllByText(tx.description);
  const editButtons = screen.getAllByRole("button", { name: /^Edit:/ });
  expect(editButtons.length).toBeGreaterThan(0);
  fireEvent.click(editButtons[0]);
  // The edit form mounts asynchronously; without this the assertions below
  // could pass against a form that never opened, which is vacuous in the
  // "absence" direction.
  await waitFor(() =>
    expect(screen.getAllByRole("button", { name: /^Save$/ }).length).toBeGreaterThan(0),
  );
}

describe("TBD-309 — reverted row affordances", () => {
  it("F1: withholds the promote checkbox on a reverted row, in BOTH slots", async () => {
    setupApiFetch([REVERTED]);
    render(<TransactionsPage />);
    await openEditFor(REVERTED);

    expect(screen.queryByTestId(`edit-recurring-row-${REVERTED.id}`)).toBeNull();
    expect(screen.queryByTestId(`edit-recurring-row-mobile-${REVERTED.id}`)).toBeNull();
  });

  it("F2: an ordinary row is STILL offered the promote checkbox in both slots", async () => {
    setupApiFetch([ORDINARY]);
    render(<TransactionsPage />);
    await openEditFor(ORDINARY);

    expect(screen.queryByTestId(`edit-recurring-row-${ORDINARY.id}`)).toBeTruthy();
    expect(screen.queryByTestId(`edit-recurring-row-mobile-${ORDINARY.id}`)).toBeTruthy();
  });

  it("F3: the submit path still issues the promote request for a promotable row", async () => {
    setupApiFetch([ORDINARY]);
    render(<TransactionsPage />);
    await openEditFor(ORDINARY);

    fireEvent.click(screen.getAllByLabelText("Make recurring")[0]);
    const nextDue = screen.getAllByLabelText(/next due/i)[0] as HTMLInputElement;
    fireEvent.change(nextDue, { target: { value: "2026-06-04" } });
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/ })[0]);

    // The promote POST must actually fire. Without this control, making the
    // submit guard unconditionally false would pass every other fence in this
    // file while silently deleting the feature.
    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(
        calls.some((u) => u.includes(`/transactions/${ORDINARY.id}/promote-to-recurring`)),
      ).toBe(true);
    });
  });

  it("F3b: the submit path refuses when the row turns reverted mid-edit", async () => {
    const setRows = setupApiFetch([ORDINARY]);
    render(<TransactionsPage />);
    await openEditFor(ORDINARY);

    fireEvent.click(screen.getAllByLabelText("Make recurring")[0]);
    const nextDue = screen.getAllByLabelText(/next due/i)[0] as HTMLInputElement;
    fireEvent.change(nextDue, { target: { value: "2026-06-04" } });

    // The row is reconciled elsewhere. The page revalidates on the global add
    // event WITHOUT closing the open form or clearing the tick, so the ticked
    // checkbox now sits over a row the server would refuse.
    setRows([{ ...ORDINARY, is_reverted: true }]);
    await act(async () => {
      window.dispatchEvent(new Event("pfv:transaction-added"));
    });

    // Precondition, asserted rather than assumed: the fresh row really landed
    // under the form (the render gate reacted) and the form is still open with
    // the tick still set. Without this the test could pass because nothing
    // happened at all.
    await waitFor(() =>
      expect(screen.queryByTestId(`edit-recurring-row-${ORDINARY.id}`)).toBeNull(),
    );
    expect(screen.getAllByRole("button", { name: /^Save$/ }).length).toBeGreaterThan(0);

    vi.mocked(apiFetch).mockClear();
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/ })[0]);

    await waitFor(() => {
      const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
      expect(calls.some((u) => u.includes(`/transactions/${ORDINARY.id}`))).toBe(true);
    });
    const calls = vi.mocked(apiFetch).mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u.includes("promote-to-recurring"))).toBe(false);
  });

  it("F6: a reverted row that is ALSO reconcile-matched shows only one chip", async () => {
    // `!isReconcileMatched` in the badge gate had zero coverage: dropping it
    // left the whole suite green. This row shape is reachable -- reopen a
    // matched row (ACCEPTED -> PENDING_REVIEW keeps the one-way link), then
    // skip it -- and without the term it renders BOTH "Matched" and
    // "Excluded", the duplication the term exists to prevent.
    const MATCHED_AND_REVERTED = makeTx({
      id: 9105,
      description: "Reopened then skipped",
      linked_transaction_id: 9500,
      linked_account_name: null,
      is_reverted: true,
    });
    setupApiFetch([MATCHED_AND_REVERTED]);
    render(<TransactionsPage />);
    await screen.findAllByText(MATCHED_AND_REVERTED.description);

    // Matched wins: it is strictly more informative (it names the duplicate
    // relationship and links to the twin) and its own copy already says the
    // row is excluded from balances and reports.
    expect(screen.getByTestId(`matched-badge-${MATCHED_AND_REVERTED.id}`)).toBeTruthy();
    expect(screen.queryByTestId(`excluded-badge-${MATCHED_AND_REVERTED.id}`)).toBeNull();
    expect(
      screen.queryByTestId(`excluded-badge-mobile-${MATCHED_AND_REVERTED.id}`),
    ).toBeNull();
  });

  it("F4: a manual balance adjustment cannot reach the promote checkbox at all", async () => {
    setupApiFetch([ADJUSTMENT, ORDINARY]);
    render(<TransactionsPage />);
    await screen.findAllByText(ADJUSTMENT.description);
    await screen.findAllByText(ORDINARY.description);

    // The entry point, not the checkbox: TBD-387 withholds Edit on this row in
    // BOTH render trees, so the form the checkbox lives in never opens.
    expect(
      screen.queryAllByRole("button", {
        name: new RegExp(`^Edit: ${ADJUSTMENT.description}$`),
      }),
    ).toHaveLength(0);

    // The ordinary row in the SAME render still offers Edit, twice (desktop +
    // mobile). Without this the assertion above passes against a page that
    // rendered no rows, or against Edit being deleted outright.
    expect(
      screen.queryAllByRole("button", {
        name: new RegExp(`^Edit: ${ORDINARY.description}$`),
      }),
    ).toHaveLength(2);
  });

  it("renders an 'Excluded' indicator on a reverted row, in BOTH slots", async () => {
    setupApiFetch([REVERTED]);
    render(<TransactionsPage />);
    await screen.findAllByText(REVERTED.description);

    const desktop = screen.getByTestId(`excluded-badge-${REVERTED.id}`);
    const mobile = screen.getByTestId(`excluded-badge-mobile-${REVERTED.id}`);

    for (const badge of [desktop, mobile]) {
      expect(badge.textContent).toContain("Excluded");

      // It is an indicator, not a navigation action: nowhere to go.
      expect(badge.closest("a")).toBeNull();

      // ⚠ Deliberately NOT asserting `tabindex === "0"`. That pins the
      // MECHANISM rather than the requirement: a correct implementation using
      // a <button> trigger would be focusable and correctly wired yet RED on
      // such an assertion. It is also redundant -- measured, removing
      // `tabIndex` reddens the `aria-describedby` assertion below on its own,
      // because Tooltip wires it onto the first FOCUSABLE descendant and a
      // non-focusable span gets nothing. The requirement is "the explanation
      // is reachable", and that is what is asserted.

      // A11Y FENCE. Opening it must wire `aria-describedby` to the bubble.
      // `title` alone is not acceptable here: it never appears on touch and is
      // largely skipped by screen readers, and this page has a full mobile
      // tree, so the explanation would be missing exactly where it is most
      // needed (docs/product/PRODUCT.md WCAG 2.2 AA).
      fireEvent.focus(badge);
      await waitFor(() => expect(badge.getAttribute("aria-describedby")).toBeTruthy());
      const bubbleId = badge.getAttribute("aria-describedby")!;
      const bubble = document.getElementById(bubbleId);
      expect(bubble).toBeTruthy();
      expect(bubble?.getAttribute("role")).toBe("tooltip");
      expect(bubble?.textContent).toContain("not counted in balances or reports");
      fireEvent.blur(badge);
    }

    // COPY FENCE (inherited from TBD-289). The indicator must not assert a
    // cause. A row can be reverted by a route the user never chose -- deleting
    // some OTHER row demotes its matched duplicate -- so naming "skipped",
    // "rejected" or "reconciliation" would claim an action they may never have
    // taken.
    //
    // ⚠ Scoped to the BADGE and its BUBBLE, not to `document.body`. A
    // page-wide negative would redden for any unrelated future copy on this
    // page (an import banner, a reconciliation-state filter) -- a false-RED
    // surface against a perfectly correct implementation.
    fireEvent.focus(desktop);
    await waitFor(() => expect(desktop.getAttribute("aria-describedby")).toBeTruthy());
    const bubble = document.getElementById(desktop.getAttribute("aria-describedby")!);
    const words = `${desktop.textContent} ${bubble?.textContent ?? ""}`.toLowerCase();

    expect(words).toContain("not counted in balances or reports");
    expect(words).toContain("its amount is not in your account balance");
    expect(words).not.toContain("reconcil");
    expect(words).not.toContain("skipped");
    expect(words).not.toContain("rejected");
  });

  it("does NOT render the indicator on an ordinary row (over-reach)", async () => {
    setupApiFetch([ORDINARY]);
    render(<TransactionsPage />);
    await screen.findAllByText(ORDINARY.description);

    expect(screen.queryByTestId(`excluded-badge-${ORDINARY.id}`)).toBeNull();
    expect(screen.queryByTestId(`excluded-badge-mobile-${ORDINARY.id}`)).toBeNull();
  });

  it("F5: a COLLAPSED transfer pair renders no indicator, even when reverted", async () => {
    setupApiFetch([REVERTED_TRANSFER_LEG]);
    render(<TransactionsPage />);
    await screen.findAllByText(REVERTED_TRANSFER_LEG.description);

    // The row stands in for TWO transactions while the wire describes only the
    // surviving leg, and which leg survives collapse is chosen by id, not by
    // state. Badging it would assert something about a pair from the state of
    // one half. Silence here is a scope boundary, not a gap.
    expect(screen.queryByTestId(`excluded-badge-${REVERTED_TRANSFER_LEG.id}`)).toBeNull();
    expect(
      screen.queryByTestId(`excluded-badge-mobile-${REVERTED_TRANSFER_LEG.id}`),
    ).toBeNull();
  });
});
