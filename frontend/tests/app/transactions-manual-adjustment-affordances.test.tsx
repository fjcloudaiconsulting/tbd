/**
 * TBD-387 — a manual balance adjustment must not be offered an action the
 * server always refuses.
 *
 * `is_manual_adjustment` rows are read-only through the whole standard CRUD
 * surface. Three separate server guards refuse them outright:
 *
 *   update_transaction        transaction_service.py:513  "cannot be edited"
 *   delete_transaction        transaction_service.py:1348 "cannot be deleted"
 *   convert_and_create_leg    transaction_service.py:2050 "cannot ... transfer leg"
 *
 * The transactions page consulted the field for exactly one thing (the
 * promote-to-recurring checkbox, TBD-309) and rendered Edit, Delete and
 * "Mark as transfer" unconditionally — three live violations of the standing
 * TBD-289 rule, "no affordance is offered that the server will refuse". The
 * user fills in the form and the request 400s.
 *
 * ⚠ SCOPE. The ticket names only Edit. Edit is not the only one — Delete and
 * Mark-as-transfer carry identical, independently-written server refusals and
 * identical unguarded render sites. Fixing Edit alone leaves two doors open,
 * which is this repo's most-repeated defect shape.
 *
 * ⚠ THE FIXTURES ARE TYPED AS THE SHARED `Transaction`, NOT A LOCAL `type Tx`,
 * for the reason the TBD-309 sibling file documents: a local row type compiles
 * and passes even if `lib/types.ts` loses the field entirely.
 *
 * ⚠ EVERY FENCE RENDERS AN ADJUSTMENT ROW AND AN ORDINARY ROW IN THE SAME
 * LIST. jsdom renders both the desktop grid and the mobile card layout (the
 * `md:` breakpoint is CSS-only), so each affordance appears TWICE per row when
 * offered. Asserting "0 for the adjustment" alone cannot distinguish "both
 * slots suppressed" from "the query matched nothing for an unrelated reason";
 * asserting "0 for the adjustment AND exactly 2 for the ordinary row in the
 * same render" can. That pairing is what makes these fences rather than
 * decorations, and it is why the mobile twin cannot be missed here — the slot
 * this file's neighbours have repeatedly missed.
 *
 * Each fence names the wrong implementation it kills:
 *
 *  F1  No Edit on an adjustment row; Edit still offered twice on an ordinary
 *      row in the same render.
 *      Kills: gating the desktop tree and forgetting the mobile twin (which
 *      leaves the count at 1 rather than 0), and a blanket "never render Edit".
 *
 *  F2  No Delete on an adjustment row; Delete still offered twice on an
 *      ordinary row.
 *      Kills: fixing only the affordance the ticket names. `delete_transaction`
 *      raises its own ValidationError, so this door is open independently.
 *
 *  F3  No "Mark as transfer" on an adjustment row; still offered twice on an
 *      ordinary row.
 *      Kills: the same, for the third independent refusal
 *      (`convert_and_create_leg`).
 *
 *  F4  A REVERTED row (skipped/rejected) IS still offered Edit and Delete.
 *      The over-reach fence, and the most valuable one here.
 *      Kills: reusing the existing `canPromoteToRecurring` predicate — the
 *      obvious, tempting implementation. That predicate also tests
 *      `is_reverted` and `linked_transaction_id`, but the server refuses
 *      NEITHER for edit or delete: `update_transaction` and
 *      `delete_transaction` guard on `is_manual_adjustment` ONLY (verified at
 *      transaction_service.py:513 and :1348 — no reconciliation_state term
 *      reaches either). Reusing it would silently strip Edit and Delete from
 *      every reverted and every transfer-leg row, converting a
 *      too-permissive UI into a too-restrictive one.
 *
 *  F5  The row says WHY, in both slots, and reaches the accessibility tree by
 *      TEXT rather than only through `title`. An ordinary row says nothing.
 *      Kills: hiding the buttons and leaving a bare gap, and attaching the
 *      explanation to a `title` attribute only (the a11y half of TBD-289).
 *
 * Copy discipline, inherited verbatim from TBD-295/TBD-289: the copy must say
 * what the row IS and what follows from it, never assert a cause the UI cannot
 * know. Here the UI CAN know the cause — `is_manual_adjustment` is the field
 * being gated on — so naming it is accurate rather than speculative.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

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
  id: 401, name: "Checking A", account_type_id: 4,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: true,
};

const CATEGORY_GROCERIES = {
  id: 311, name: "Groceries", type: "expense" as const,
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

const ADJUSTMENT = makeTx({
  id: 9201,
  description: "Balance correction",
  is_manual_adjustment: true,
});

const ORDINARY = makeTx({ id: 9202, description: "Ordinary bakery run" });

// is_reverted WITHOUT is_manual_adjustment. This row is the F4 control: the
// server edits and deletes it happily.
const REVERTED = makeTx({
  id: 9203,
  description: "Reverted duplicate charge",
  is_reverted: true,
});

function setupApiFetch(txs: Transaction[]) {
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT_CHECKING] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    if (url.startsWith("/api/v1/transactions"))
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

/** Renders the page with the given rows and waits for the list to paint.
 *
 * The `findAllByText` is a precondition, asserted rather than assumed: without
 * it every "absence" assertion below could pass against a page that never
 * rendered a single row, which is vacuous in exactly the direction these
 * fences test.
 */
async function renderRows(txs: Transaction[]) {
  setupApiFetch(txs);
  render(<TransactionsPage />);
  for (const tx of txs) {
    await screen.findAllByText(tx.description);
  }
}

/** Counts the affordance buttons for one row across BOTH render trees. */
function affordanceCount(action: string, tx: Transaction): number {
  return screen.queryAllByRole("button", {
    name: new RegExp(`^${action}: ${tx.description}$`),
  }).length;
}

describe("TBD-387 — manual balance adjustment affordances", () => {
  it("F1: offers no Edit on an adjustment row, while an ordinary row keeps both", async () => {
    await renderRows([ADJUSTMENT, ORDINARY]);

    expect(affordanceCount("Edit", ADJUSTMENT)).toBe(0);
    // Exactly 2 = desktop grid + mobile card. A gate applied to one tree only
    // leaves this at 1 for the adjustment row, which `toBe(0)` catches.
    expect(affordanceCount("Edit", ORDINARY)).toBe(2);
  });

  it("F2: offers no Delete on an adjustment row, while an ordinary row keeps both", async () => {
    await renderRows([ADJUSTMENT, ORDINARY]);

    expect(affordanceCount("Delete", ADJUSTMENT)).toBe(0);
    expect(affordanceCount("Delete", ORDINARY)).toBe(2);
  });

  it("F3: offers no Mark-as-transfer on an adjustment row, while an ordinary row keeps both", async () => {
    await renderRows([ADJUSTMENT, ORDINARY]);

    expect(affordanceCount("Mark as transfer", ADJUSTMENT)).toBe(0);
    expect(affordanceCount("Mark as transfer", ORDINARY)).toBe(2);
  });

  it("F4: a REVERTED row still keeps Edit and Delete in both slots", async () => {
    await renderRows([REVERTED, ADJUSTMENT]);

    // The server refuses NEITHER for a reverted row. Reusing
    // `canPromoteToRecurring` here — the obvious implementation — would drive
    // both of these to 0 while leaving F1..F3 green.
    expect(affordanceCount("Edit", REVERTED)).toBe(2);
    expect(affordanceCount("Delete", REVERTED)).toBe(2);
    // ...and the adjustment row in the same render is still suppressed, so
    // this cannot pass by the gate having been deleted outright.
    expect(affordanceCount("Edit", ADJUSTMENT)).toBe(0);
  });

  it("F5: says why, in both slots, in the accessibility tree by text", async () => {
    await renderRows([ADJUSTMENT, ORDINARY]);

    // By TEXT, not by `title`: a tooltip-only explanation is invisible to a
    // screen reader and to keyboard-only users, which is the a11y half of the
    // TBD-289 rule this ticket restores.
    expect(screen.queryAllByText(ADJUSTMENT_READ_ONLY_NOTE)).toHaveLength(2);
  });

  it("F5b: an ordinary row carries no such note", async () => {
    // The over-reach control on F5. Without it the note could be rendered
    // unconditionally on every row and F5 would still pass.
    //
    // ⚠ A SEPARATE `it` ON PURPOSE. Asserting both halves inside one test
    // requires a second `render()` into the same document, and RTL's auto
    // cleanup runs BETWEEN tests, not between renders -- so the adjustment row
    // from the first render is still mounted and the count is 2, not 0. That
    // is a test red against correct code, which is as expensive as the
    // opposite kind.
    await renderRows([ORDINARY]);
    expect(screen.queryAllByText(ADJUSTMENT_READ_ONLY_NOTE)).toHaveLength(0);
  });
});

/** The exact user-facing sentence, asserted here so a re-word has to be
 * deliberate. Kept in sync with the constant the page exports. */
const ADJUSTMENT_READ_ONLY_NOTE =
  "Balance adjustments are read-only. Add a new adjustment to correct this one.";
