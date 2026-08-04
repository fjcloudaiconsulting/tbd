import React from "react";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithSWR } from "../utils/render-with-swr";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { todayISO } from "@/lib/format";
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

  it("a failing refresh does NOT overwrite the partial-success banner", async () => {
    // ⭐ FENCE (TBD-301). The promote-failure path is newly reachable: this
    // change removed the client-side clamp that used to guarantee the server
    // could not refuse. Two failures land together here -- the promote is
    // refused, and the refresh that follows it fails too -- and the SECOND
    // one used to win, because it propagated to handleSaveEdit's outer catch
    // and replaced the message with its own. The user was then shown a bare
    // refetch error, with no way to learn that the edit itself had persisted.
    const tx = makeTx({ id: 76, description: "Double fail", recurring_id: null });
    const apiFetchMock = vi.mocked(apiFetch);
    apiFetchMock.mockReset();
    let listGets = 0;
    apiFetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.startsWith("/api/v1/accounts")) return [ACCT_A] as never;
      if (url.startsWith("/api/v1/categories")) return [CATEGORY_GROCERIES] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) return [] as never;
      if (url === "/api/v1/transactions/76" && method === "PUT") {
        return { ...tx, description: "Double fail edited" } as never;
      }
      if (url === "/api/v1/transactions/76/promote-to-recurring" && method === "POST") {
        throw new Error("Next due date cannot be earlier than 2026-08-15");
      }
      if (url.startsWith("/api/v1/transactions") && method === "GET") {
        listGets += 1;
        // The mount fetch succeeds; the post-promote refresh does not.
        if (listGets > 1) throw new Error("session expired mid-refresh");
        return { items: [tx], total: 1, limit: 25, offset: 0 } as never;
      }
      return null as never;
    });

    renderWithSWR(<TransactionsPage />);
    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);
    fireEvent.click(screen.getAllByLabelText("Make recurring")[0]);
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    // The refresh really was attempted and really did fail -- without this
    // the test would pass on a build that never refetches at all.
    await waitFor(() => {
      expect(listGets).toBeGreaterThan(1);
    });

    // ⭐ The surviving message is still the one that says what persisted, and
    // it still carries the SERVER's reason rather than the refresh's.
    expect(
      screen.getByText(/Transaction updated, but promote-to-recurring failed/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Next due date cannot be earlier than 2026-08-15/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/session expired mid-refresh/i),
    ).not.toBeInTheDocument();
  });

  // ── TBD-301: the client no longer rules on the frontier date ─────────────
  //
  // The lower bound on `next_due_date` is the start of the org's CURRENT
  // billing cycle (TBD-283), not `today`. For any org whose cycle does not
  // begin today there is a legal window BEFORE today. This page used to
  // refuse that window twice over: a JS guard in the save handler, and a
  // `min={todayISO()}` on the date input in BOTH render trees. Both were
  // stricter than the API, so both are gone; the server's 400 names the
  // real boundary and reaches the user through the existing
  // partial-success path.

  /**
   * The promote block renders TWICE, once per layout, and the two copies are
   * maintained by hand.
   *
   * ⚠ Do NOT reach for them positionally. `getAllByLabelText` returns matches
   * grouped by MATCHING STRATEGY, not in document order (verified against
   * @testing-library/dom 10.4.1, `dist/queries/label-text.js`): elements with
   * a real `<label>` or `aria-labelledby` are collected first, in document
   * order, then `aria-label` matches are CONCATENATED as a trailing group and
   * the whole thing is de-duplicated.
   *
   * Which index means which tree therefore depends ON THE CONTROL, and it is
   * not uniform across this block:
   *
   *   - "Next due date" and "Number of payments": index 0 is MOBILE, index 1
   *     is DESKTOP -- the opposite of source order. Mobile has a real
   *     `<label htmlFor>`; desktop carries only `aria-label`.
   *   - "Make recurring": index 0 is DESKTOP, index 1 is MOBILE. BOTH
   *     checkboxes sit inside an implicit `<label>` wrapper, so both land in
   *     the first group and source order survives.
   *
   * Scope by container id instead, which cannot drift when a `<label>` is
   * added or removed on either side.
   */
  function treeContainer(id: number, mobile: boolean): HTMLElement {
    return screen.getByTestId(
      mobile ? `edit-recurring-row-mobile-${id}` : `edit-recurring-row-${id}`,
    );
  }

  /** Open the edit row for `id` in the desktop (`mobile:false`) or mobile
   *  tree, tick "Make recurring", set the next-due date to `nextDue`, Save,
   *  and return the promote body (null if the promote never fired). */
  async function promoteWithNextDue(
    id: number,
    nextDue: string,
    { mobile = false }: { mobile?: boolean } = {},
  ): Promise<Record<string, unknown> | null> {
    const tx = makeTx({ id, description: "Backdated", recurring_id: null });
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

    await screen.findAllByLabelText("Make recurring");
    const tree = within(treeContainer(id, mobile));
    fireEvent.click(tree.getByLabelText("Make recurring"));

    fireEvent.change(tree.getByLabelText("Next due date"), {
      target: { value: nextDue },
    });

    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

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

  it("does NOT refuse a next_due_date before today, and sends it verbatim (DESKTOP)", async () => {
    // ⭐ FENCE (TBD-301). RED against the `editRecNextDue < todayISO()` guard,
    // which returned early with "Date must be today or later" and never
    // issued the PUT at all.
    const body = await promoteWithNextDue(90, "2020-03-09");
    expect(body).not.toBeNull();
    expect(body!.next_due_date).toBe("2020-03-09");
    expect(
      screen.queryByText(/Date must be today or later/i),
    ).not.toBeInTheDocument();
  });

  it("does NOT refuse a next_due_date before today, and sends it verbatim (MOBILE)", async () => {
    // ⭐ FENCE. The date input is hand-maintained in two render trees; the
    // save handler is shared. Both halves must agree, and this repo has
    // shipped a fix to one tree and not the other before.
    const body = await promoteWithNextDue(91, "2020-03-09", { mobile: true });
    expect(body).not.toBeNull();
    expect(body!.next_due_date).toBe("2020-03-09");
    expect(
      screen.queryByText(/Date must be today or later/i),
    ).not.toBeInTheDocument();
  });

  it("sends YESTERDAY verbatim, and identically whichever billing_cycle_day the org has", async () => {
    // ⭐ FENCE (TBD-301), the definition-of-done case stated directly: a date
    // inside the org's current cycle but before today reaches the API
    // unmodified. `2020-03-09` above is far outside any cycle; this one sits
    // in the window the removed rules were actually eating.
    //
    // Anchored to `today - 1` rather than a literal, so it cannot rot into a
    // date that is no longer in the past. Which cycle it lands in varies by
    // run date, and that is fine: the asserted property is "unmodified",
    // which holds on every calendar day.
    const d = new Date();
    d.setDate(d.getDate() - 1);
    const yesterday = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    expect(yesterday).not.toBe(todayISO());

    // Run the SAME input through two different orgs.
    //
    // ⚠ Be honest about what this half can and cannot fail. TODAY it is
    // TAUTOLOGICAL: neither `app/transactions/page.tsx` nor
    // `components/floating/TransactionForm.tsx` reads `billing_cycle_day` at
    // all (their only mentions of it are comments), so the two iterations run
    // byte-identical code and the second can only agree with the first. It
    // cannot go RED against any wrong implementation that exists now.
    //
    // That is still a legitimate purpose -- it is a REGRESSION detector, and
    // the thing it detects is specific: someone reintroducing a client-side
    // rule keyed on the cycle day, which is the exact shape TBD-283 put out
    // of the client's reach. Neither run alone would catch that.
    //
    // What it is NOT is the definition of done. The `expect(sent)` assertion
    // below, and the two verbatim-wire fences above it, are what establish
    // that the user's date reaches the API unmodified.
    const sent: string[] = [];
    for (const [i, cycleDay] of [1, 17].entries()) {
      vi.mocked(useAuth).mockReturnValue({
        user: { ...USER, billing_cycle_day: cycleDay } as never,
        loading: false,
        needsSetup: false,
        login: vi.fn(),
        register: vi.fn(),
        logout: vi.fn(),
        refreshMe: vi.fn(),
      });
      const body = await promoteWithNextDue(94 + i, yesterday);
      expect(body, `billing_cycle_day=${cycleDay}`).not.toBeNull();
      sent.push(body!.next_due_date as string);
      cleanup();
    }
    expect(sent).toEqual([yesterday, yesterday]);
  });

  it("CONTROL: the blank-date guard still refuses and still blocks the PUT", async () => {
    // The two fences above assert an absence, which is the shape that most
    // easily passes for the wrong reason. This is the same setup driven the
    // other way: a DIFFERENT client-side rule on the SAME field still fires
    // and still stops the write, proving the harness can observe a
    // client-side refusal at all. If this went silent too, the fences above
    // would be measuring a broken save path rather than a removed date rule.
    const tx = makeTx({ id: 92, description: "Blank due", recurring_id: null });
    setupApiFetch([tx]);
    renderWithSWR(<TransactionsPage />);
    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);
    fireEvent.click((await screen.findAllByLabelText("Make recurring"))[0]);
    fireEvent.change((await screen.findAllByLabelText("Next due date"))[0], {
      target: { value: "" },
    });
    fireEvent.click(screen.getAllByRole("button", { name: /^Save$/i })[0]);

    await waitFor(() => {
      expect(
        screen.getAllByText(/Pick a next due date/i).length,
      ).toBeGreaterThan(0);
    });
    const apiFetchMock = vi.mocked(apiFetch);
    expect(
      apiFetchMock.mock.calls.filter(
        (c) =>
          c[0] === "/api/v1/transactions/92" &&
          (c[1] as RequestInit | undefined)?.method === "PUT",
      ),
    ).toHaveLength(0);
  });

  it("neither render tree constrains the next-due-date input with a `min` floor", async () => {
    // ⭐ FENCE (TBD-301). The JS guard is shared between the two trees, but
    // the `min` attribute was written out by hand TWICE. jsdom does not
    // enforce `min`, so no behavioural test can see it -- in a real browser
    // it greys out every date before today in the picker, which is the same
    // wrong rule wearing a different hat. Asserting the attribute is absent
    // is the only way to fence it.
    // `status: "pending"` is load-bearing, not decoration: both settled-date
    // inputs are gated on `editStatus === "pending"`, and `startEdit` seeds
    // that from the row. With `makeTx`'s default `"settled"` the control at
    // the bottom of this test matched zero elements and asserted nothing.
    const tx = makeTx({
      id: 93,
      description: "Min check",
      recurring_id: null,
      status: "pending",
    });
    setupApiFetch([tx]);
    renderWithSWR(<TransactionsPage />);
    await waitForStableTxList();
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit:/ })[0]);
    fireEvent.click((await screen.findAllByLabelText("Make recurring"))[0]);

    await screen.findAllByLabelText("Next due date");
    // Named trees, not indices -- see the note on `treeContainer`.
    for (const mobile of [false, true]) {
      const el = within(treeContainer(93, mobile)).getByLabelText(
        "Next due date",
      );
      expect(el.getAttribute("min"), mobile ? "mobile" : "desktop").toBeNull();
    }

    // Control: the transaction-date -> settled-date `min` relationship is a
    // real domain rule and is NOT what this ticket removes. Its presence
    // proves the assertions above can distinguish "no min attribute" from
    // "this query never finds a min attribute on anything". Unconditional on
    // purpose -- guarded behind `if (settled.length > 0)` it was dead code,
    // which is precisely the failure a control exists to rule out.
    const settled = screen.getAllByLabelText(/Expected settlement date/i);
    expect(settled).toHaveLength(2);
    for (const [i, el] of settled.entries()) {
      expect(el.getAttribute("min"), `settled-date input ${i}`).not.toBeNull();
    }
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
