/**
 * Punch-list ITEM 2: pending pill on the Transactions page reads gray instead
 * of vivid amber because the row applies `opacity-60` to the entire grid.
 *
 * The naive fix (add `opacity-100` to the pill span) does NOT work — CSS
 * `opacity` composites with ancestor opacity, so 60%×100% still paints at
 * 60%. The applied fix moves the dim from the row container onto specific
 * non-pill cells (desktop: `[&>*:not(.tx-status-cell)]:opacity-60` on the
 * row; mobile: per-segment opacity-60 with the pill cell untouched).
 *
 * These tests pin the structural invariant: the pill cell on a pending row
 * must NOT inherit any ancestor opacity-60 class.
 */
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

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

function makePendingTx() {
  return {
    id: 42,
    account_id: ACCT_A.id,
    account_name: ACCT_A.name,
    category_id: CATEGORY_GROCERIES.id,
    category_name: CATEGORY_GROCERIES.name,
    description: "Pending coffee",
    amount: 12.5,
    type: "expense" as const,
    status: "pending" as const,
    linked_transaction_id: null,
    recurring_id: null,
    date: "2026-05-01",
    settled_date: null,
    is_imported: false,
  };
}

function setupApiFetch(txs: ReturnType<typeof makePendingTx>[]) {
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
 * Walks ancestors of `el` up to (and including) `boundary`. Used to assert
 * none of them carry an `opacity-60` class — that's the load-bearing
 * structural property: the pill must not inherit ancestor dimming.
 */
function ancestorsToBoundary(
  el: Element,
  boundary: Element | null,
): Element[] {
  const chain: Element[] = [];
  let cur: Element | null = el;
  while (cur && cur !== boundary) {
    chain.push(cur);
    cur = cur.parentElement;
  }
  if (boundary) chain.push(boundary);
  return chain;
}

describe("Transactions page — pending pill opacity (punch ITEM 2)", () => {
  it("desktop: pending row dims via [&>*:not(.tx-status-cell)]:opacity-60, pill cell stays full opacity", async () => {
    setupApiFetch([makePendingTx()]);
    render(<TransactionsPage />);

    // Wait for the row to render.
    await screen.findAllByText("Pending coffee");

    // Mark-as-pending button has aria-label="Mark as settled" on a pending row.
    // Pick the first match (jsdom renders both desktop+mobile).
    const buttons = await screen.findAllByLabelText(/^Mark as settled$/);
    expect(buttons.length).toBeGreaterThan(0);

    // The desktop row uses a 12-column grid; the button is wrapped in a
    // <span class="tx-status-cell col-span-1 ...">. Find that ancestor.
    const desktopButton = buttons.find((b) =>
      b.className.includes("inline-flex"),
    );
    expect(desktopButton).toBeDefined();

    const pillCell = desktopButton!.closest(".tx-status-cell");
    expect(pillCell).toBeTruthy();

    // The pill cell itself must not carry opacity-60.
    expect(pillCell!.className).not.toMatch(/\bopacity-60\b/);

    // The PARENT row carries the arbitrary variant
    // `[&>*:not(.tx-status-cell)]:opacity-60`, NOT a bare `opacity-60`.
    const desktopRow = pillCell!.parentElement!;
    expect(desktopRow.className).toContain(
      "[&>*:not(.tx-status-cell)]:opacity-60",
    );
    expect(desktopRow.className).not.toMatch(/(^|\s)opacity-60(\s|$)/);
  });

  it("mobile: pending row applies opacity-60 per-segment, the pill segment is untouched", async () => {
    setupApiFetch([makePendingTx()]);
    render(<TransactionsPage />);

    await screen.findAllByText("Pending coffee");

    // The mobile pill is the second match — it uses `ml-auto` on the button.
    const pendingButtons = await screen.findAllByLabelText(/^Mark as settled$/);
    const mobileButton = pendingButtons.find((b) =>
      b.className.includes("ml-auto"),
    );
    expect(mobileButton).toBeDefined();

    // The container of the mobile pill is a flex row that holds category +
    // pill. That row must NOT carry opacity-60 (otherwise the pill would dim
    // alongside the category text).
    const pillRow = mobileButton!.parentElement!;
    expect(pillRow.className).not.toMatch(/\bopacity-60\b/);

    // The OUTER article must also not carry opacity-60 (the bug from
    // PR #183 + earlier commits had it on the article itself).
    const article = mobileButton!.closest("article");
    expect(article).toBeTruthy();
    expect(article!.className).not.toMatch(/\bopacity-60\b/);

    // No ancestor between the pill button and the article carries
    // opacity-60 — the pill is fully outside any dimming container.
    const chain = ancestorsToBoundary(mobileButton!, article);
    for (const node of chain) {
      expect(node.className).not.toMatch(/\bopacity-60\b/);
    }
  });

  it("settled row: no opacity-60 anywhere on the row regardless of layout", async () => {
    const settled = { ...makePendingTx(), status: "settled" as const };
    // The pending fixture is `as const` for status, which narrows it to the
    // string literal "pending"; the spread above plus a fresh literal lets
    // tsc accept the override without a wider Tx-shaped helper type.
    setupApiFetch([settled as unknown as ReturnType<typeof makePendingTx>]);
    render(<TransactionsPage />);

    await screen.findAllByText("Pending coffee");
    // On a settled row the toggle aria flips to 'Mark as pending'.
    const buttons = await screen.findAllByLabelText(/^Mark as pending$/);
    expect(buttons.length).toBeGreaterThan(0);

    for (const btn of buttons) {
      // Walk up to the article (mobile) or to the grid row (desktop).
      // Either way, no ancestor should carry opacity-60.
      let node: Element | null = btn;
      while (node) {
        expect(node.className).not.toMatch(/\bopacity-60\b/);
        if (node.tagName === "ARTICLE" || node.className.includes("grid-cols-12")) {
          break;
        }
        node = node.parentElement;
      }
    }
  });
});

// Touch fireEvent so eslint/no-unused-imports keeps it; reserved for future
// interactive variants (e.g. flipping pending<->settled and re-checking).
void fireEvent;
void waitFor;
