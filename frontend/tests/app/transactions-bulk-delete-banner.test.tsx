import React from "react";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import { renderWithSWR } from "../utils/render-with-swr";
import { waitForStableTxList } from "../utils/wait-for-stable-tx-list";
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

const ACCT_B = {
  id: 200, name: "Checking B", account_type_id: 1,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: false,
};

const CATEGORY_GROCERIES = {
  id: 11, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};

function makeTx(over: Partial<{
  id: number;
  account_id: number;
  account_name: string;
  description: string;
  amount: number;
  type: "income" | "expense";
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
    linked_account_name: null,
    recurring_id: null,
    date: "2026-05-01",
    settled_date: null,
    is_imported: false,
    ...over,
  };
}

// The four rows the user ticks. They are ordinary un-linked rows on purpose:
// the list request sends collapse_transfers=true, so a transfer shows up here
// as ONE row and the client CANNOT tell which selected row happens to be a
// transfer leg. Everything the banner knows about the cascade arrives as
// numbers in the bulk-delete response, which is exactly what these tests vary.
const ROWS = [
  makeTx({ id: 1, description: "Row one" }),
  makeTx({ id: 2, description: "Row two" }),
  makeTx({ id: 3, description: "Row three" }),
  makeTx({ id: 4, description: "Row four" }),
];

interface BulkDeleteResponse {
  requested_count: number;
  deleted_count: number;
  skipped_ids: number[];
  demoted_ids: number[];
}

// Pass an Error to make the bulk-delete request itself fail.
function setupApiFetch(bulkDeleteResponse: BulkDeleteResponse | Error) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url === "/api/v1/transactions/bulk-delete") {
      if (bulkDeleteResponse instanceof Error) throw bulkDeleteResponse;
      return bulkDeleteResponse as never;
    }
    if (url.startsWith("/api/v1/accounts")) return [ACCT_A, ACCT_B] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
    const detail = /^\/api\/v1\/transactions\/(\d+)$/.exec(url);
    if (detail) {
      const id = Number(detail[1]);
      return (ROWS.find((t) => t.id === id) ?? null) as never;
    }
    if (url.startsWith("/api/v1/transactions"))
      return { items: ROWS, total: ROWS.length, limit: 25, offset: 0 } as never;
    return null as never;
  });
}

// Tick `ids`, press "Delete selected", confirm in the modal, and return the
// text of the banner the page renders afterwards. Defaults to all four rows;
// the singular-copy case ticks exactly one.
async function bulkDeleteAndReadBanner(
  ids: number[] = ROWS.map((t) => t.id),
): Promise<string> {
  confirmBulkDelete(await openBulkDeleteDialog(ids));
  const banner = await screen.findByText(/^Deleted /);
  return banner.textContent ?? "";
}

// Tick `ids`, press "Delete selected" and return the confirmation dialog.
//
// Confirming is a separate SYNCHRONOUS step on purpose: the caller must start
// its findBy/waitFor in the same tick as the click. Resolving an async helper
// in between hands the delete handler microtasks in which its state updates
// land outside any act() scope (measured: 3 act() warnings per test).
async function openBulkDeleteDialog(ids: number[] = ROWS.map((t) => t.id)) {
  renderWithSWR(<TransactionsPage />);

  await waitForStableTxList();

  // Desktop and mobile layouts both render in jsdom and share the aria-label,
  // so click the first of each pair.
  ids.forEach((id) => {
    fireEvent.click(screen.getAllByLabelText(`Select transaction ${id}`)[0]);
  });

  const deleteSelected = await screen.findByRole("button", { name: /^Delete selected$/ });
  fireEvent.click(deleteSelected);

  return screen.findByRole("dialog");
}

// Confirm inside the dialog: plain /^Delete$/ would also match the per-row
// action buttons behind the modal.
function confirmBulkDelete(dialog: HTMLElement) {
  fireEvent.click(within(dialog).getByRole("button", { name: /^Delete$/ }));
}

// The defect this file fences: a banner that reads "Deleted 6 of 4
// transactions." Rather than pin one literal string, assert the PROPERTY:
// no "N of M" anywhere in the banner may have N greater than M. A copy change
// that keeps the inversion in different words still fails here.
function expectNoInvertedCount(text: string) {
  const matches = [...text.matchAll(/(\d+)\s+of\s+(?:the\s+)?(\d+)/g)];
  matches.forEach((m) => {
    expect(
      Number(m[1]),
      `"${m[0]}" reports a larger count "of" a smaller one in: ${text}`,
    ).toBeLessThanOrEqual(Number(m[2]));
  });
  return matches;
}

describe("TransactionsPage — bulk-delete banner counts (TBD-290)", () => {
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

  it("transfer cascade: never reports more deleted than the user selected", async () => {
    // 4 selected, 1 already gone, so 3 of the user's rows went. All 3 were
    // transfer legs, so the server also removed 3 partners: deleted_count 6.
    setupApiFetch({
      requested_count: 4,
      deleted_count: 6,
      skipped_ids: [4],
      demoted_ids: [],
    });

    const text = await bulkDeleteAndReadBanner();

    expect(text).not.toMatch(/6 of (?:the )?4/);
    const counts = expectNoInvertedCount(text);
    // Guard the guard: if the banner stops phrasing counts as "N of M" the
    // loop above passes vacuously, so require the user-facing pair to be there.
    expect(counts.length).toBeGreaterThan(0);

    // The count the user can verify: 3 of the 4 rows they ticked.
    expect(text).toMatch(/\b3 of (?:the )?4\b/);
    expect(text).toMatch(/1 was already gone/);
    // The cascade is disclosed, but the 6 DB rows are NOT named: the list is
    // fetched with collapse_transfers=true, so the user saw 3 rows go, never 6.
    expect(text).toMatch(/[Tt]ransfers come in pairs/);
    expect(text).not.toMatch(/\b6\b/);
    expect(text).not.toMatch(/rows/);
  });

  it("control, no cascade: reports the plain count and says nothing about transfers", async () => {
    // 4 selected, 1 already gone, 3 deleted, none of them a transfer leg.
    setupApiFetch({
      requested_count: 4,
      deleted_count: 3,
      skipped_ids: [4],
      demoted_ids: [],
    });

    const text = await bulkDeleteAndReadBanner();

    expect(text).toMatch(/\b3 of (?:the )?4\b/);
    expect(text).toMatch(/1 was already gone/);
    expectNoInvertedCount(text);
    // No cascade happened, so the transfer explanation must not appear.
    expect(text).not.toMatch(/[Tt]ransfers come in pairs/);
    expect(text).not.toMatch(/halves/);
  });

  it("control, cascade masked by a skip: deleted_count equal to requested_count still explains the extra row", async () => {
    // 4 selected, 1 already gone, 3 deleted, ONE of them a transfer leg whose
    // partner also went: deleted_count 4 == requested_count 4. An
    // implementation that only explains itself when deleted_count exceeds
    // requested_count reports "4" as if all four ticked rows were deleted.
    setupApiFetch({
      requested_count: 4,
      deleted_count: 4,
      skipped_ids: [4],
      demoted_ids: [],
    });

    const text = await bulkDeleteAndReadBanner();

    expect(text).toMatch(/\b3 of (?:the )?4\b/);
    expect(text).toMatch(/1 was already gone/);
    expect(text).toMatch(/[Tt]ransfers come in pairs/);
    expectNoInvertedCount(text);
  });

  it("two already gone: the skipped sentence pluralises", async () => {
    // The only variation that kills a hardcoded "1 was already gone." or a
    // dropped was/were ternary: every other case in this file skips exactly one.
    setupApiFetch({
      requested_count: 4,
      deleted_count: 2,
      skipped_ids: [3, 4],
      demoted_ids: [],
    });

    const text = await bulkDeleteAndReadBanner();

    expect(text).toMatch(/\b2 of (?:the )?4\b/);
    expect(text).toMatch(/2 were already gone/);
    expect(text).not.toMatch(/\bwas already gone/);
    expectNoInvertedCount(text);
  });

  it("a single selected row, already gone: the sentence reads in the singular", async () => {
    // The only case with requested_count 1, which is what kills a dropped
    // transaction/transactions ternary. A lone selection that was deleted
    // outright shows no banner, so the one reaching it here is the row that
    // had already gone.
    setupApiFetch({
      requested_count: 1,
      deleted_count: 0,
      skipped_ids: [4],
      demoted_ids: [],
    });

    const text = await bulkDeleteAndReadBanner([4]);

    expect(text).toBe("Deleted 0 of 1 transaction you selected. 1 was already gone.");
    // Singular noun, and no "the 1 transaction" / "the 1 transactions" wording.
    expect(text).not.toMatch(/transactions/);
    expect(text).not.toMatch(/of the 1\b/);
    expectNoInvertedCount(text);
  });
});

describe("TransactionsPage — bulk-delete banner tone, announcement and gate (TBD-317)", () => {
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

  const PARTIAL: BulkDeleteResponse = {
    requested_count: 4,
    deleted_count: 3,
    skipped_ids: [4],
    demoted_ids: [],
  };

  it("a partial success renders in the warning family, not the danger family", async () => {
    // Kills leaving the banner on setError (errorCls, danger/coral): 3 of 4
    // deleted is a caution, not a failure.
    setupApiFetch(PARTIAL);
    confirmBulkDelete(await openBulkDeleteDialog());

    const banner = await screen.findByText(/^Deleted /);
    expect(banner.className).toMatch(/\bbg-warning-dim\b/);
    expect(banner.className).not.toMatch(/danger/);
  });

  it("the banner is announced through the transactions live region", async () => {
    // Kills a warning box mounted outside `transactions-live-region` (or a
    // second, conditionally mounted announcer): the region is the one that
    // exists before the message does.
    setupApiFetch(PARTIAL);
    confirmBulkDelete(await openBulkDeleteDialog());

    const banner = await screen.findByText(/^Deleted /);
    const region = screen.getByTestId("transactions-live-region");
    expect(region.contains(banner)).toBe(true);
    // Kills stripping the announcer attributes off the region itself.
    expect(region.getAttribute("role")).toBe("status");
    expect(region.getAttribute("aria-live")).toBe("polite");
  });

  it("the demotion warning survives next to a partial success, in the same interaction", async () => {
    // Kills the naive setError -> setNotice swap: `notice` already holds the
    // demotion sentence one line earlier, and overwriting it drops a warning
    // about an irreversible change.
    setupApiFetch({ ...PARTIAL, deleted_count: 5, demoted_ids: [9001] });
    confirmBulkDelete(await openBulkDeleteDialog());

    const region = screen.getByTestId("transactions-live-region");
    await waitFor(() => expect(region.textContent).toContain("Deleted "));
    expect(region.textContent).toContain(
      "Deleted 3 of the 4 transactions you selected. 1 was already gone. Transfers come in pairs, so the matching halves went too.",
    );
    expect(region.textContent).toContain(
      "1 matched duplicate was marked rejected. It no longer counts toward balances or reports.",
    );
  });

  it("a pure transfer cascade with nothing skipped still explains itself", async () => {
    // skipped == 0, deleted_count > requested_count: 4 selected, 2 of them
    // transfer legs, 6 rows gone. Kills keeping the `skipped_ids.length > 0`
    // gate, under which the user is never told rows they did not pick went.
    // The lead-in is not "4 of the 4": nothing the user picked was missed, and
    // "N of N" frames a complete delete as a partial one.
    setupApiFetch({ requested_count: 4, deleted_count: 6, skipped_ids: [], demoted_ids: [] });
    confirmBulkDelete(await openBulkDeleteDialog());

    const region = screen.getByTestId("transactions-live-region");
    await waitFor(() =>
      expect(region.textContent).toContain(
        "Deleted the 4 transactions you selected. Transfers come in pairs, so the matching halves went too.",
      ),
    );
    expect(region.textContent).not.toMatch(/already gone/);
    expect(region.textContent).not.toMatch(/\d+ of/);
  });

  it("a pure cascade from a single selected row reads in the singular", async () => {
    // One leg selected removes exactly one partner (extra === 1). Kills a
    // dropped singular branch in the complete-delete lead-in, and a hard-coded
    // plural "halves" in the cascade sentence. The plural (extra === 2) is
    // pinned by the pure-cascade test above and the demotion test.
    setupApiFetch({ requested_count: 1, deleted_count: 2, skipped_ids: [], demoted_ids: [] });
    confirmBulkDelete(await openBulkDeleteDialog([1]));

    const region = screen.getByTestId("transactions-live-region");
    await waitFor(() =>
      expect(region.textContent).toBe(
        "Deleted the transaction you selected. Transfers come in pairs, so the matching half went too.",
      ),
    );
  });

  it("an ordinary delete (N selected, N deleted, nothing skipped, no cascade) shows no banner", async () => {
    // Kills widening the gate to always-show. The demotion notice is the
    // positive signal that the handler reached the point where the banner
    // would have been set, so the absence below is not vacuous.
    setupApiFetch({ requested_count: 4, deleted_count: 4, skipped_ids: [], demoted_ids: [9001] });
    confirmBulkDelete(await openBulkDeleteDialog());

    const region = screen.getByTestId("transactions-live-region");
    await waitFor(() => expect(region.textContent).toMatch(/matched duplicate was marked rejected/));
    expect(screen.queryByTestId("transactions-bulk-delete-result")).toBeNull();
    expect(document.body.textContent).not.toMatch(/Deleted /);
  });

  it("a failed bulk delete stays a danger-styled error and is not presented as a caution", async () => {
    // Kills routing the catch branch into the warning banner along with the
    // partial-success message.
    setupApiFetch(new Error("Bulk delete exploded"));
    confirmBulkDelete(await openBulkDeleteDialog());

    const err = await screen.findByText("Bulk delete exploded");
    expect(err.className).toMatch(/\bbg-danger-dim\b/);
    expect(err.className).not.toMatch(/warning/);
    expect(screen.getByTestId("transactions-live-region").contains(err)).toBe(false);
  });

  it("the banner clears on the next list load", async () => {
    // Same scope rule as the TBD-294 notice: a caution about a delete must not
    // outlive the list it describes. Kills not clearing it in loadTransactions.
    setupApiFetch(PARTIAL);
    confirmBulkDelete(await openBulkDeleteDialog());
    await screen.findByText(/^Deleted /);

    fireEvent.change(screen.getByLabelText("Search transactions"), {
      target: { value: "bakery" },
    });

    await waitFor(() => expect(screen.queryByText(/^Deleted /)).toBeNull());
  });

  it("a failed follow-up bulk delete does not leave the previous caution on screen", async () => {
    // Kills dropping the clear at the top of handleBulkDelete: a failure does
    // not reload the list, so nothing else would remove the stale caution.
    setupApiFetch(PARTIAL);
    confirmBulkDelete(await openBulkDeleteDialog());
    await screen.findByText(/^Deleted /);
    setupApiFetch(new Error("Bulk delete exploded"));
    fireEvent.click(screen.getAllByLabelText("Select transaction 1")[0]);
    fireEvent.click(await screen.findByRole("button", { name: /^Delete selected$/ }));
    confirmBulkDelete(await screen.findByRole("dialog"));
    await screen.findByText("Bulk delete exploded");
    expect(screen.queryByTestId("transactions-bulk-delete-result")).toBeNull();
  });

  it("a failed single-row delete does not leave a previous bulk caution on screen", async () => {
    // Kills handleDelete clearing `error` and `notice` but not the bulk
    // caution: the failed delete does not reload, so the old caution would sit
    // next to the new error.
    setupApiFetch(PARTIAL);
    confirmBulkDelete(await openBulkDeleteDialog());
    await screen.findByText(/^Deleted /);

    const apiFetchMock = vi.mocked(apiFetch);
    const base = apiFetchMock.getMockImplementation()!;
    apiFetchMock.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (opts?.method === "DELETE") throw new Error("Single delete exploded");
      return base(url, opts);
    });
    fireEvent.click(screen.getAllByLabelText("Delete: Row one")[0]);
    confirmBulkDelete(await screen.findByRole("dialog"));

    await screen.findByText("Single delete exploded");
    expect(screen.queryByTestId("transactions-bulk-delete-result")).toBeNull();
  });

  it("both banners survive the page clamp when a bulk delete empties the last page", async () => {
    // 30 rows at 25 per page, user on page 2, deletes its 5 rows. The reload
    // shrinks total to 25, the clamp moves the page back to 1, and that page
    // change reloads the list again. Kills a loadTransactions that clears the
    // banners on the clamp's reload: the caution and the demotion sentence
    // would be on screen for one frame.
    const page1 = Array.from({ length: 25 }, (_, i) =>
      makeTx({ id: i + 1, description: `Row ${i + 1}` }),
    );
    const page2 = Array.from({ length: 5 }, (_, i) =>
      makeTx({ id: i + 26, description: `Row ${i + 26}` }),
    );
    let deleted = false;
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/v1/transactions/bulk-delete") {
        deleted = true;
        return {
          requested_count: 5,
          deleted_count: 4,
          skipped_ids: [30],
          demoted_ids: [9001],
        } as never;
      }
      if (url.startsWith("/api/v1/accounts")) return [ACCT_A, ACCT_B] as never;
      if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
      if (url.startsWith("/api/v1/transactions?")) {
        const onPage2 = url.includes("offset=25");
        if (deleted) {
          return { items: onPage2 ? [] : page1, total: 25, limit: 25, offset: onPage2 ? 25 : 0 } as never;
        }
        return { items: onPage2 ? page2 : page1, total: 30, limit: 25, offset: onPage2 ? 25 : 0 } as never;
      }
      return null as never;
    });

    renderWithSWR(<TransactionsPage />);
    await waitForStableTxList();
    fireEvent.click(await screen.findByLabelText("Next page", undefined, { timeout: 8000 }));
    await screen.findAllByLabelText("Select transaction 26");
    fireEvent.click(screen.getAllByLabelText("Select all on page")[0]);
    fireEvent.click(await screen.findByRole("button", { name: /^Delete selected$/ }));
    confirmBulkDelete(await screen.findByRole("dialog"));

    // Wait for the clamp's own reload of page 1 to land.
    await waitFor(() => {
      const calls = apiFetchMock.mock.calls.map(([u]) => u as string);
      const del = calls.indexOf("/api/v1/transactions/bulk-delete");
      expect(del).toBeGreaterThanOrEqual(0);
      expect(
        calls.slice(del + 1).some((u) => u.startsWith("/api/v1/transactions?") && u.includes("offset=0")),
      ).toBe(true);
    });
    await screen.findAllByLabelText("Select transaction 1");

    const region = screen.getByTestId("transactions-live-region");
    await waitFor(() => {
      expect(region.textContent).toContain("Deleted 4 of the 5 transactions you selected. 1 was already gone.");
      expect(region.textContent).toContain("1 matched duplicate was marked rejected.");
    });

    // The clamp's keep-once must be spent by that reload: a later filter
    // change still clears both. Kills a keep flag that stays armed.
    fireEvent.change(screen.getByLabelText("Search transactions"), {
      target: { value: "bakery" },
    });
    await waitFor(() => expect(region.textContent).toBe(""));
  });

  it("a failed batch edit after a partial bulk delete does not leave the caution on screen", async () => {
    // Kills a write handler that clears `error` but not the delete banners:
    // the failed request does not reload, so the old caution would sit next
    // to the new error. Representative of handleBatchEdit, handleSkipOccurrence,
    // handleSaveEdit and handleToggleStatus, which share the same clear.
    setupApiFetch(PARTIAL);
    confirmBulkDelete(await openBulkDeleteDialog());
    await screen.findByText(/^Deleted /);

    const apiFetchMock = vi.mocked(apiFetch);
    const base = apiFetchMock.getMockImplementation()!;
    apiFetchMock.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url === "/api/v1/transactions/bulk-update") throw new Error("Batch edit exploded");
      return base(url, opts);
    });
    fireEvent.click(screen.getAllByLabelText("Select transaction 1")[0]);
    fireEvent.click((await screen.findAllByRole("button", { name: /^Batch edit$/ }))[0]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(dialog.querySelector("#batch-edit-status")!, { target: { value: "pending" } });
    fireEvent.click(within(dialog).getByRole("button", { name: /apply/i }));

    await screen.findByText("Batch edit exploded");
    expect(screen.queryByTestId("transactions-bulk-delete-result")).toBeNull();
  });
});
