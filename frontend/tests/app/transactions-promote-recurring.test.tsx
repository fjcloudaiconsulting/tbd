import React from "react";
import { fireEvent, screen, waitFor } from "@testing-library/react";
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

function setupApiFetch(txs: Tx[], extras: Record<string, unknown> = {}) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (extras[`${method} ${url}`] !== undefined) {
      return extras[`${method} ${url}`] as never;
    }
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

describe("TransactionsPage — promote to recurring (L3.12)", () => {
  it("non-recurring row: toggle reveals frequency + next-due-date inputs", async () => {
    const tx = makeTx({ id: 70, description: "Promo me" });
    setupApiFetch([tx]);
    renderWithSWR(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    // Toggle present, frequency + date hidden by default.
    const toggles = await screen.findAllByLabelText("Make recurring");
    expect(toggles.length).toBeGreaterThan(0);
    expect(screen.queryAllByLabelText("Frequency").length).toBe(0);
    expect(screen.queryAllByLabelText("Next due date").length).toBe(0);

    // Tick the box -> frequency + next due date appear.
    fireEvent.click(toggles[0]);
    await waitFor(() => {
      expect(screen.queryAllByLabelText("Frequency").length).toBeGreaterThan(0);
      expect(screen.queryAllByLabelText("Next due date").length).toBeGreaterThan(0);
    });
  });

  it("save fires PUT then POST /promote-to-recurring in order with the picked schedule", async () => {
    const tx = makeTx({ id: 71, description: "Save me", recurring_id: null });
    const promotedResponse: Tx = { ...tx, recurring_id: 999 };
    setupApiFetch([tx], {
      [`PUT /api/v1/transactions/71`]: { ...tx, description: "Save me edited" },
      [`POST /api/v1/transactions/71/promote-to-recurring`]: promotedResponse,
    });
    renderWithSWR(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    // Tick the recurring toggle on the desktop layout (first match).
    fireEvent.click(screen.getAllByLabelText("Make recurring")[0]);

    // Pick a frequency other than the default.
    const freq = screen.getAllByLabelText("Frequency")[0];
    fireEvent.change(freq, { target: { value: "weekly" } });

    // Save.
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    const apiFetchMock = vi.mocked(apiFetch);
    await waitFor(() => {
      const putCall = apiFetchMock.mock.calls.find(
        (c) =>
          c[0] === "/api/v1/transactions/71" &&
          (c[1] as RequestInit | undefined)?.method === "PUT",
      );
      const promoteCall = apiFetchMock.mock.calls.find(
        (c) =>
          c[0] === "/api/v1/transactions/71/promote-to-recurring" &&
          (c[1] as RequestInit | undefined)?.method === "POST",
      );
      expect(putCall).toBeTruthy();
      expect(promoteCall).toBeTruthy();
    });

    // Confirm ordering: PUT comes before POST in the call log.
    const calls = apiFetchMock.mock.calls;
    const putIdx = calls.findIndex(
      (c) =>
        c[0] === "/api/v1/transactions/71" &&
        (c[1] as RequestInit | undefined)?.method === "PUT",
    );
    const promoteIdx = calls.findIndex(
      (c) =>
        c[0] === "/api/v1/transactions/71/promote-to-recurring" &&
        (c[1] as RequestInit | undefined)?.method === "POST",
    );
    expect(putIdx).toBeLessThan(promoteIdx);

    // Promote payload carries the chosen frequency + a date.
    const promoteCall = calls.find(
      (c) =>
        c[0] === "/api/v1/transactions/71/promote-to-recurring" &&
        (c[1] as RequestInit | undefined)?.method === "POST",
    )!;
    const body = JSON.parse((promoteCall[1] as RequestInit).body as string);
    expect(body.frequency).toBe("weekly");
    expect(body.next_due_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  // ── TBD-275: instalment count on the edit-time promote ───────────────────

  /** Open the edit row for `id`, tick "Make recurring", optionally type a
   *  payments count, Save, and return the parsed promote body (null if the
   *  promote never fired). */
  async function promoteWithCount(
    id: number,
    count: string | null,
    { mobile = false }: { mobile?: boolean } = {},
  ): Promise<Record<string, unknown> | null> {
    const tx = makeTx({ id, description: "Klarna", recurring_id: null });
    setupApiFetch([tx], {
      [`PUT /api/v1/transactions/${id}`]: tx,
      [`POST /api/v1/transactions/${id}/promote-to-recurring`]: {
        ...tx,
        recurring_id: 999,
      },
    });
    renderWithSWR(<TransactionsPage />);
    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    // ⚠ The promote block renders TWICE — once in the desktop row, once in
    // the mobile card — and `getAllBy*` returns both. Index 0 is the desktop
    // one, index 1 the mobile one; a field wired into only ONE of them is the
    // exact regression this indexing exists to catch.
    const idx = mobile ? 1 : 0;
    const toggles = await screen.findAllByLabelText("Make recurring");
    expect(toggles.length).toBe(2);
    fireEvent.click(toggles[idx]);

    if (count !== null) {
      const inputs = await screen.findAllByLabelText("Number of payments");
      expect(inputs.length).toBe(2);
      fireEvent.change(inputs[idx], { target: { value: count } });
    }

    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[idx]);

    const apiFetchMock = vi.mocked(apiFetch);
    await waitFor(() => {
      expect(
        apiFetchMock.mock.calls.some(
          (c) => c[0] === `/api/v1/transactions/${id}`,
        ),
      ).toBe(true);
    });
    const promoteCall = apiFetchMock.mock.calls.find(
      (c) =>
        c[0] === `/api/v1/transactions/${id}/promote-to-recurring` &&
        (c[1] as RequestInit | undefined)?.method === "POST",
    );
    if (!promoteCall) return null;
    return JSON.parse(
      (promoteCall[1] as RequestInit).body as string,
    ) as Record<string, unknown>;
  }

  it("OMITS occurrence_count when the payments field is left blank", async () => {
    // FENCE. Blank means open-ended. `null` and `0` are both wrong on the wire
    // (`Optional[int] = Field(gt=0)`), and a `toBeUndefined()` assertion would
    // be GREEN against a body that sends null, so the key-presence check is
    // the discriminating one.
    const body = await promoteWithCount(80, null);
    expect(body).not.toBeNull();
    expect(Object.keys(body as object)).not.toContain("occurrence_count");
  });

  it("threads the payments count through from the DESKTOP promote block", async () => {
    const body = await promoteWithCount(81, "12");
    expect(body!.occurrence_count).toBe(12);
    expect(typeof body!.occurrence_count).toBe("number");
  });

  it("threads the payments count through from the MOBILE promote block", async () => {
    // FENCE. The block renders twice and the two copies are maintained by
    // hand. A field added to only the desktop one is invisible to every test
    // that reaches for index 0.
    const body = await promoteWithCount(82, "4", { mobile: true });
    expect(body!.occurrence_count).toBe(4);
  });

  it("REJECTS a zero or non-numeric count and does not PUT the edit at all", async () => {
    // FENCE. The edit-row Save is a plain onClick, NOT a form submit, so there
    // is no native constraint validation here at all — the guard in
    // handleSaveEdit is the only thing standing between a bad count and a
    // committed edit followed by a 422 the user reads as "the save failed".
    for (const [id, bad] of [[83, "0"], [84, "abc"], [85, "2.5"]] as const) {
      const tx = makeTx({ id, description: "Klarna", recurring_id: null });
      setupApiFetch([tx]);
      const { unmount } = renderWithSWR(<TransactionsPage />);
      await waitForStableTxList();
      fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);
      fireEvent.click((await screen.findAllByLabelText("Make recurring"))[0]);
      fireEvent.change(
        (await screen.findAllByLabelText("Number of payments"))[0],
        { target: { value: bad } },
      );
      fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

      await waitFor(() => {
        expect(
          screen.getAllByText(/Number of payments must be a whole number/i)
            .length,
          `value=${bad}`,
        ).toBeGreaterThan(0);
      });
      // ⭐ Nothing was written. Not the edit, not the series.
      const apiFetchMock = vi.mocked(apiFetch);
      expect(
        apiFetchMock.mock.calls.filter(
          (c) =>
            c[0] === `/api/v1/transactions/${id}` &&
            (c[1] as RequestInit | undefined)?.method === "PUT",
        ),
        `value=${bad}`,
      ).toHaveLength(0);
      unmount();
    }
  });

  it("partial success: PUT succeeds + POST promote fails surfaces partial-success message and exits edit", async () => {
    const tx = makeTx({ id: 75, description: "Partial save", recurring_id: null });
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.startsWith("/api/v1/accounts")) return [ACCT_A] as never;
      if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
      if (url === "/api/v1/transactions/75" && method === "PUT") {
        return { ...tx, description: "Partial save edited" } as never;
      }
      if (url === "/api/v1/transactions/75/promote-to-recurring" && method === "POST") {
        throw new Error("recurring quota exceeded");
      }
      if (url.startsWith("/api/v1/transactions") && method === "GET")
        return { items: [tx], total: 1, limit: 25, offset: 0 } as never;
      return null as never;
    });

    renderWithSWR(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    fireEvent.click(screen.getAllByLabelText("Make recurring")[0]);
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    // Partial-success banner explicitly tells the user what stuck and what failed.
    await waitFor(() => {
      expect(
        screen.getByText(/Transaction updated, but promote-to-recurring failed/i),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByText(/recurring quota exceeded/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/still reflects your edits/i),
    ).toBeInTheDocument();

    // Edit mode should have exited (the PUT did persist), so no Save button visible.
    await waitFor(() => {
      expect(screen.queryAllByRole("button", { name: /^Save$/i }).length).toBe(0);
    });
  });

  it("save without ticking recurring does NOT call promote-to-recurring", async () => {
    const tx = makeTx({ id: 72, description: "No promote" });
    setupApiFetch([tx], {
      [`PUT /api/v1/transactions/72`]: tx,
    });
    renderWithSWR(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    const apiFetchMock = vi.mocked(apiFetch);
    await waitFor(() => {
      const putCall = apiFetchMock.mock.calls.find(
        (c) =>
          c[0] === "/api/v1/transactions/72" &&
          (c[1] as RequestInit | undefined)?.method === "PUT",
      );
      expect(putCall).toBeTruthy();
    });

    const promoteCall = apiFetchMock.mock.calls.find(
      (c) =>
        c[0] === "/api/v1/transactions/72/promote-to-recurring" &&
        (c[1] as RequestInit | undefined)?.method === "POST",
    );
    expect(promoteCall).toBeUndefined();
  });

  it("already-recurring row: shows static 'Recurring' chip, no toggle", async () => {
    const tx = makeTx({ id: 73, description: "Already promo", recurring_id: 5 });
    setupApiFetch([tx]);
    renderWithSWR(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    // Chip rendered (desktop + mobile each render once).
    await waitFor(() => {
      expect(screen.queryAllByText("Recurring").length).toBeGreaterThan(0);
    });
    // No toggle on this row.
    expect(screen.queryAllByLabelText("Make recurring").length).toBe(0);
  });

  it("transfer-leg row: no recurring controls or chip rendered", async () => {
    // Both legs carry linked_account_name: the list request passes
    // collapse_transfers=true, and the server populates that field for every
    // MUTUALLY-linked pair. It — not the raw linked_transaction_id column — is
    // what marks a row as a transfer leg (TBD-268 U1).
    const expenseLeg = makeTx({
      id: 80, account_id: ACCT_A.id, account_name: ACCT_A.name,
      type: "expense", amount: 50, description: "Linked out",
      linked_transaction_id: 81, linked_account_name: "Acct B",
    });
    const incomeLeg = makeTx({
      id: 81, account_id: 200, account_name: "Acct B",
      type: "income", amount: 50, description: "Linked in",
      linked_transaction_id: 80, linked_account_name: ACCT_A.name,
    });
    setupApiFetch([expenseLeg, incomeLeg]);
    renderWithSWR(<TransactionsPage />);

    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);

    // Mirror notice present (sanity: we are in the linked edit path).
    await screen.findAllByText(/Changes to amount apply to both rows/i);

    // No recurring toggle, no chip — the whole control block is hidden for legs.
    expect(screen.queryAllByLabelText("Make recurring").length).toBe(0);
    expect(screen.queryByText("Recurring")).toBeNull();
  });
});
