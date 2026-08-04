/**
 * TBD-289 — a reconcile-matched row must not be offered an action the server
 * always refuses, and must say that it is matched.
 *
 * `reconciliation_service._apply_match` writes `linked_transaction_id` ONE-WAY,
 * so a reconcile-matched row carries a non-null `linked_transaction_id` with a
 * NULL `linked_account_name`. The page's transfer signal is
 * `linked_account_name != null` (TBD-268), so such a row used to land in the
 * `!isPairedTransfer` branch and get offered "Mark transfer" — which
 * `transaction_service._link_pair` invariant 7 rejects with "Expense leg is
 * already linked".
 *
 * Three fences, and each one names the wrong implementation it kills:
 *
 *  1. MATCHED row is NOT offered "Mark transfer".
 *     Kills: gating the offer on `!isPairedTransfer` alone (i.e. reverting to
 *     `linked_account_name != null`), in EITHER the desktop or the mobile slot.
 *
 *  2. ORDINARY unlinked row IS still offered "Mark transfer".
 *     The over-reach fence. Without it "never render the button" passes fence 1.
 *     Kills: `!isPairedTransfer && !isLinked` widened to a bare `false`, or a
 *     flag mis-derived so that an unlinked row reads as matched.
 *
 *  3. GENUINE transfer pair is unchanged: no "Mark transfer", still "Unlink",
 *     still the arrow subline, and NO "Matched" badge (it is a transfer, and it
 *     already says so).
 *     Kills: over-broadening the new flag to `linked_transaction_id != null`
 *     without excluding paired transfers, which would stamp "Matched" onto both
 *     legs of a real transfer.
 *
 * Plus the legibility fence: the matched row renders a non-interactive
 * "Matched" indicator (kills shipping only the button-hiding half), and that
 * indicator is NOT a button or a link (kills "make it clickable", which is the
 * deferred TBD-292/295 question).
 *
 * And two fences on the WORDS, not the mechanism:
 *
 *  - The copy never says "reconciliation". `isReconcileMatched` is shorthand
 *    for "linked but not reciprocally", which is equally true of a self-linked
 *    row, a cross-org link and a chain A->B->C. Kills copy that asserts a cause
 *    the flag cannot know.
 *  - The explanation is in the accessibility tree, not only in `title`. A bare
 *    <span>'s `title` is not an accessible name: screen readers largely skip
 *    it, touch never shows it, and the span is not focusable. Kills shipping
 *    the explanation where the users who most need it cannot reach it
 *    (PRODUCT.md WCAG 2.2 AA).
 */
import React from "react";
import { render, screen } from "@testing-library/react";

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
  balance: 0, currency: "EUR", is_active: false,
  close_day: null, is_default: false,
};

const CATEGORY_GROCERIES = {
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
};

function makeTx(over: Partial<Tx> & { id: number; description: string }): Tx {
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
    ...over,
  } as Tx;
}

// A reconcile-matched row: linked_transaction_id SET, linked_account_name NULL.
// That exact combination is what `_apply_match` produces and is the whole
// point of this ticket.
const MATCHED = makeTx({
  id: 9001,
  description: "Matched supermarket run",
  linked_transaction_id: 9500,
  linked_account_name: null,
});

// A self-linked row: `linked_transaction_id === id`. Corrupt but real; no
// writer produces it, which is exactly why nothing downstream may assume the
// target is a DIFFERENT row.
const SELF_LINKED = makeTx({
  id: 9005,
  description: "Self linked oddity",
  linked_transaction_id: 9005,
  linked_account_name: null,
});

// An ordinary row: neither field set.
const ORDINARY = makeTx({
  id: 9002,
  description: "Ordinary bakery run",
  linked_transaction_id: null,
  linked_account_name: null,
});

// A genuine transfer leg: BOTH fields set (mutually linked pair, collapsed by
// the server so only this leg is in the page).
const TRANSFER_LEG = makeTx({
  id: 9003,
  description: "Genuine transfer leg",
  linked_transaction_id: 9004,
  linked_account_name: ACCT_SAVINGS.name,
});

function setupApiFetch(txs: Tx[]) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT_CHECKING, ACCT_SAVINGS] as never;
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

/**
 * jsdom renders BOTH the desktop grid and the mobile card layout (the md:
 * breakpoint is CSS-only). Every assertion below therefore counts matches
 * across both, so hiding the button in only one slot cannot pass.
 */
function markTransferButtons(description: string): HTMLElement[] {
  return screen.queryAllByLabelText(`Mark as transfer: ${description}`);
}

function unlinkButtons(description: string): HTMLElement[] {
  return screen.queryAllByLabelText(`Unlink transfer: ${description}`);
}

describe("TBD-289 — reconcile-matched row affordances", () => {
  it("does NOT offer 'Mark transfer' on a reconcile-matched row, in ANY action slot", async () => {
    setupApiFetch([MATCHED]);
    render(<TransactionsPage />);

    // The row is on screen before we assert an absence — otherwise this
    // passes vacuously against a page that rendered nothing at all.
    await screen.findAllByText(MATCHED.description);
    // Sanity: the page really did render both layouts, so "zero buttons"
    // below means "both slots suppressed", not "only one slot exists".
    expect(screen.getByTestId(`tx-row-desktop-${MATCHED.id}`)).toBeTruthy();
    expect(screen.getAllByText(MATCHED.description).length).toBeGreaterThan(1);

    expect(markTransferButtons(MATCHED.description)).toHaveLength(0);
  });

  /**
   * ⚠ REWRITTEN BY TBD-295, deliberately.
   *
   * TBD-289 shipped this indicator NON-interactive and fenced that choice
   * here, because "what should a matched row let a user DO" was an open
   * ruling. TBD-295 IS that ruling and it answers: link to the canonical
   * twin. So the fence that pinned "not a button, not a link" had to be
   * rewritten, not extended — a prior fence encoding the half of the problem
   * its own ticket did not fix.
   *
   * What survives verbatim from TBD-289 and must not be lost in the rewrite:
   *   - the copy never says "reconciliation";
   *   - the explanation reaches the accessibility tree by TEXT, not `title`;
   *   - the styling is the quiet neutral badge primitive, never the brass
   *     accent and never a raw palette colour;
   *   - both the desktop and the mobile slot are asserted.
   */
  it("renders a 'Matched' indicator that LINKS to the canonical twin, in both slots", async () => {
    setupApiFetch([MATCHED]);
    render(<TransactionsPage />);

    await screen.findAllByText(MATCHED.description);

    // Present in BOTH renderers.
    const desktopBadge = screen.getByTestId(`matched-badge-${MATCHED.id}`);
    const mobileBadge = screen.getByTestId(`matched-badge-mobile-${MATCHED.id}`);

    for (const badge of [desktopBadge, mobileBadge]) {
      expect(badge.textContent).toContain("Matched");
      // TBD-295: the row is a DUPLICATE and is out of balances and reports.
      // The old copy ("Linked to another transaction.") said neither.
      expect(badge.getAttribute("title")).toBe(
        "Marked as a duplicate of another transaction. It is excluded from balances and reports.",
      );
      // The copy must not name reconciliation: the flag is "linked but not
      // reciprocally", which is also true of a self-linked, cross-org or
      // chained row that reconciliation never touched.
      expect(badge.textContent).not.toMatch(/reconcil/i);
      expect(badge.getAttribute("title")).not.toMatch(/reconcil/i);
      // WCAG 2.2 AA: `title` alone has no accessible name on touch and is
      // largely skipped by screen readers. The explanation must reach the
      // accessibility tree by text — still true now the element is focusable.
      const srOnly = badge.querySelector(".sr-only");
      expect(srOnly).not.toBeNull();
      expect(srOnly?.textContent).toMatch(/duplicate of another transaction/i);
      expect(srOnly?.textContent).toMatch(/balances and reports/i);
      // THE REVERSAL: it is a link, and it points at the canonical twin.
      expect(badge.tagName).toBe("A");
      expect(badge.getAttribute("href")).toBe(
        `/transactions?transaction_id=${MATCHED.linked_transaction_id}`,
      );
      // Quiet-by-default + No Off-Token: the neutral badge primitive from
      // lib/styles.ts, not the brass accent and not a raw palette colour.
      expect(badge.className).toContain("bg-surface-raised");
      expect(badge.className).toContain("text-text-secondary");
      expect(badge.className).not.toMatch(/\baccent\b/);
    }
  });

  it("renders NO link on a self-linked row, in either slot", async () => {
    // `linked_transaction_id === id`: the target IS this row, so a link would
    // be a no-op that claims otherwise. Kills rendering the <a> unconditionally.
    setupApiFetch([SELF_LINKED]);
    render(<TransactionsPage />);

    await screen.findAllByText(SELF_LINKED.description);

    for (const testid of [
      `matched-badge-${SELF_LINKED.id}`,
      `matched-badge-mobile-${SELF_LINKED.id}`,
    ]) {
      const badge = screen.getByTestId(testid);
      // Still says it is matched — the row IS linked-but-not-reciprocally.
      expect(badge.textContent).toContain("Matched");
      // But inert: no anchor, and no href anywhere inside it.
      expect(badge.tagName).toBe("SPAN");
      expect(badge.closest("a")).toBeNull();
      expect(badge.querySelector("a")).toBeNull();
    }
  });

  it("still offers Edit and the status pill on a matched row (TBD-292 regression fence)", async () => {
    // TBD-292 made the matched row editable server-side. This stops a future
    // agent from "fixing" a matched-row bug by hiding Edit instead — the
    // affordances must stay, in BOTH slots.
    setupApiFetch([MATCHED]);
    render(<TransactionsPage />);

    await screen.findAllByText(MATCHED.description);

    // ByROLE, not ByLabelText: an element carrying `hidden` (or aria-hidden)
    // is still returned by ByLabelText, so a "hide it instead of fixing it"
    // change would slip past a label query. The role query reads the
    // accessibility tree, which is what a user actually gets.
    expect(
      screen.getAllByRole("button", { name: `Edit: ${MATCHED.description}` }),
    ).toHaveLength(2);
    // The status pill is the interactive toggle, not the inert transfer chip:
    // a matched row is NOT a transfer, so it keeps the button.
    expect(
      screen.getAllByRole("button", { name: "Mark as pending" }).length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByRole("button", { name: `Delete: ${MATCHED.description}` }),
    ).toHaveLength(2);
  });

  it("STILL offers 'Mark transfer' on an ordinary unlinked row (over-reach fence)", async () => {
    setupApiFetch([ORDINARY]);
    render(<TransactionsPage />);

    await screen.findAllByText(ORDINARY.description);

    // Both slots must still offer it: desktop grid + mobile card.
    expect(markTransferButtons(ORDINARY.description)).toHaveLength(2);
    // And an unlinked row must NOT claim to be matched.
    expect(screen.queryByTestId(`matched-badge-${ORDINARY.id}`)).toBeNull();
    expect(screen.queryByTestId(`matched-badge-mobile-${ORDINARY.id}`)).toBeNull();
  });

  it("leaves a genuine transfer pair exactly as it was", async () => {
    setupApiFetch([TRANSFER_LEG]);
    render(<TransactionsPage />);

    await screen.findAllByText(TRANSFER_LEG.description);

    // Unchanged transfer behaviour: no "Mark transfer", Unlink in both slots.
    expect(markTransferButtons(TRANSFER_LEG.description)).toHaveLength(0);
    expect(unlinkButtons(TRANSFER_LEG.description)).toHaveLength(2);
    // Still renders the transfer subline (source -> destination), i.e. the
    // transfer signal was not collateral damage of the new flag.
    //
    // Match the COMBINED string, not each name on its own: every account is
    // rendered unconditionally as an <option> in the account filter, so
    // /Checking A/ and /Savings B/ both pass with the subline deleted. Only the
    // arrow form is unique to the subline (same shape as the sibling fence in
    // transactions-server-pagination.test.tsx).
    expect(
      screen.getAllByText(/Checking A\s*→\s*Savings B/).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText(/Savings B\s*→\s*Checking A/)).toBeNull();
    // A transfer is NOT "matched" — it already says what it is. This kills
    // over-broadening the new flag to a bare `linked_transaction_id != null`.
    expect(screen.queryByTestId(`matched-badge-${TRANSFER_LEG.id}`)).toBeNull();
    expect(screen.queryByTestId(`matched-badge-mobile-${TRANSFER_LEG.id}`)).toBeNull();
  });

  it("separates the three row kinds in ONE page render", async () => {
    // The per-row tests above each render a single-row page, so a bug that
    // keys off "the page has any linked row" would slip through. This renders
    // all three together and pins each independently.
    setupApiFetch([MATCHED, ORDINARY, TRANSFER_LEG]);
    render(<TransactionsPage />);

    await screen.findAllByText(ORDINARY.description);

    expect(markTransferButtons(MATCHED.description)).toHaveLength(0);
    expect(markTransferButtons(ORDINARY.description)).toHaveLength(2);
    expect(markTransferButtons(TRANSFER_LEG.description)).toHaveLength(0);

    expect(screen.getByTestId(`matched-badge-${MATCHED.id}`)).toBeTruthy();
    expect(screen.queryByTestId(`matched-badge-${ORDINARY.id}`)).toBeNull();
    expect(screen.queryByTestId(`matched-badge-${TRANSFER_LEG.id}`)).toBeNull();
  });
});
