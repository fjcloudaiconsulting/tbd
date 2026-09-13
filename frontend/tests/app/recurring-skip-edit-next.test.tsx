/**
 * TBD-272 / TBD-273. The recurring page's "Skip next" and "Edit next" actions.
 *
 * Both act on the series FRONTIER: the server takes `occurrence_date` as the
 * next_due_date the client saw and 409s when it no longer matches, so the date
 * must come off the row, never from the clock. Each fence names the wrong
 * implementation it kills.
 *
 *  F1  skip-next POSTs exactly {occurrence_date: next_due_date} to the row's id
 *      (after the table sorted), then refetches the list.
 *      Kills: today's date, a missing body, the wrong row, no reload.
 *  F2  The confirm copy carries description, date and signed amount; the
 *      instalment line only for an instalment series.
 *      Kills: copy without the date, and "1 of the null payments".
 *  F3  Both actions render in BOTH trees with their aria-labels; absent in the
 *      Paused table and for a finished instalment series.
 *      Kills: one tree only, and gating on `is_active` alone.
 *  F4  A rejected request shows the server message verbatim AND reloads.
 *  F5  A second confirm click while the first POST is in flight sends nothing.
 *  F6  edit-next materialises BEFORE the PUT; the PUT goes to the RETURNED id
 *      with body keys exactly ["amount"].
 *      Kills: a PUT to the template, the wrong id, a full-form body, reversed
 *      order.
 *  F7  0, abc, 1.234, an 11-digit amount and the template's own amount (typed
 *      as "1200" against the wire's "1200.00") send NO request, and Save is
 *      disabled, with the message for each case.
 *      Kills: an uncapped integer part, one message for both cases, and a
 *      strict compare of the typed string against the Decimal string.
 *  F8  A PUT failure after materialise shows the partial copy, reloads, and
 *      never retries materialise.
 */
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import RecurringPage from "@/app/recurring/page";
import { ApiResponseError, apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { formatMoney } from "@/lib/format";
import type { RecurringTransaction } from "@/lib/types";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/recurring",
  // TBD-316 makes the recurring page read ?recurring_id; without this the
  // merged tree crashes this file, as it did TBD-285's toast test.
  useSearchParams: () => new URLSearchParams(),
}));

const USER = {
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner",
  org_id: 1, org_name: "Acme", billing_cycle_day: 1, is_superadmin: false,
  is_active: true, mfa_enabled: false, password_set: true,
  subscription_status: null, subscription_plan: null, trial_end: null,
  allow_manual_balance_adjustment: false,
};

function rec(over: Partial<RecurringTransaction>): RecurringTransaction {
  return {
    id: 1, account_id: 1, account_name: "Checking", category_id: 1,
    category_name: "Bills", description: "x", amount: "10.00",
    type: "expense", frequency: "monthly", next_due_date: "2026-09-01",
    auto_settle: false, is_active: true, occurrence_count: null,
    occurrences_elapsed: 0, ...over,
  };
}

// Amounts are STRINGS, as the Decimal is on the wire. Distinct ids and dates, listed OUT of the table's default (next_due_date asc)
// order, so a lookup by position instead of by row cannot pass.
const RENT = rec({ id: 41, description: "Rent", amount: "1200.00", next_due_date: "2026-10-01" });
const SOFA = rec({
  id: 42, description: "Sofa", amount: "250.00", next_due_date: "2026-08-15",
  occurrence_count: 12, occurrences_elapsed: 3,
});
const LAPTOP_DONE = rec({
  id: 43, description: "Laptop", amount: "99.00", next_due_date: "2026-07-01",
  occurrence_count: 12, occurrences_elapsed: 12,
});
const PAUSED = rec({ id: 44, description: "Gym", amount: "30.00", is_active: false });
const ROWS = [RENT, SOFA, LAPTOP_DONE, PAUSED];

type Handler = (init?: RequestInit) => unknown;

function mockApi(handlers: Record<string, Handler> = {}) {
  vi.mocked(apiFetch).mockImplementation(((url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url}`;
    if (key === "GET /api/v1/recurring") return Promise.resolve(ROWS);
    const h = handlers[key];
    if (!h) return Promise.reject(new Error(`unexpected ${key}`));
    return Promise.resolve().then(() => h(init));
  }) as never);
}

const calls = () =>
  vi.mocked(apiFetch).mock.calls.map(([url, init]) => ({
    key: `${(init as RequestInit | undefined)?.method ?? "GET"} ${url}`,
    body: (init as RequestInit | undefined)?.body,
  }));

const listLoadsAfter = (key: string) => {
  const all = calls();
  const i = all.findIndex((c) => c.key === key);
  return i < 0 ? 0 : all.slice(i + 1).filter((c) => c.key === "GET /api/v1/recurring").length;
};

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  window.localStorage.clear();
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never, loading: false, needsSetup: false,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  } as never);
});

/** The desktop and mobile trees both render in jsdom; scope by the <table>. */
async function trees(label: string) {
  const active = await screen.findByTestId("recurring-active-table");
  const table = within(active).getByRole("table");
  const all = within(active).queryAllByLabelText(label);
  return {
    desktop: all.filter((el) => table.contains(el)),
    mobile: all.filter((el) => !table.contains(el)),
  };
}

async function open(label: string) {
  const { desktop } = await trees(label);
  expect(desktop).toHaveLength(1);
  fireEvent.click(desktop[0]);
  return screen.findByRole("dialog");
}

const signed = (n: number) => `-${formatMoney(n)}`;

describe("recurring page: skip next (TBD-272)", () => {
  it("F1: POSTs the row's own next_due_date to the row's id, then reloads", async () => {
    mockApi({ "POST /api/v1/recurring/41/skip-next": () => ({ id: 900 }) });
    render(<RecurringPage />);
    const dialog = await open("Skip next: Rent");
    fireEvent.click(within(dialog).getByRole("button", { name: "Skip" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const post = calls().filter((c) => c.key.includes("skip-next"));
    expect(post.map((c) => c.key)).toEqual(["POST /api/v1/recurring/41/skip-next"]);
    expect(typeof post[0].body, "request body").toBe("string");
    expect(JSON.parse(String(post[0].body))).toEqual({ occurrence_date: "2026-10-01" });
    expect(listLoadsAfter("POST /api/v1/recurring/41/skip-next")).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Skipped "Rent" on 2026-10-01.')).toBeInTheDocument();
  });

  it("F2: open-ended copy has description, date and signed amount, no payments line", async () => {
    mockApi();
    render(<RecurringPage />);
    const dialog = await open("Skip next: Rent");
    expect(within(dialog).getByText("Skip Next Occurrence")).toBeInTheDocument();
    const text = dialog.textContent ?? "";
    expect(text).toContain(`Skip "Rent" on 2026-10-01 (${signed(1200)})?`);
    expect(text).toContain("It will stay on your Transactions page marked Excluded and won't be counted in balances or reports.");
    expect(text).toContain("This can't be undone.");
    expect(text).not.toMatch(/occurrences\./);
  });

  it("F2: instalment copy says it still counts as 1 of the N payments", async () => {
    mockApi();
    render(<RecurringPage />);
    const dialog = await open("Skip next: Sofa");
    const text = dialog.textContent ?? "";
    expect(text).toContain(`Skip "Sofa" on 2026-08-15 (${signed(250)})?`);
    expect(text).toContain("It still counts as 1 of the 12 occurrences.");
  });

  it("F3: both actions in BOTH trees; not for a finished series or a paused one", async () => {
    mockApi();
    render(<RecurringPage />);
    for (const label of ["Skip next: Rent", "Edit next amount: Rent"]) {
      const t = await trees(label);
      expect(t.desktop, `${label} desktop`).toHaveLength(1);
      expect(t.mobile, `${label} mobile`).toHaveLength(1);
    }
    for (const label of ["Skip next: Laptop", "Edit next amount: Laptop"]) {
      expect(screen.queryAllByLabelText(label), label).toHaveLength(0);
    }
    // CONTROL: the finished row did render.
    expect(screen.getByLabelText("Stop: Laptop")).toBeInTheDocument();

    const paused = screen.getByTestId("recurring-paused-table");
    expect(within(paused).queryAllByLabelText(/^(Skip next|Edit next amount):/)).toHaveLength(0);
    expect(within(paused).getByLabelText("Resume: Gym")).toBeInTheDocument();
  });

  it("F4: a 409 shows the server message verbatim and reloads", async () => {
    const detail = "The next occurrence is already in your transactions. Skip it there.";
    mockApi({
      "POST /api/v1/recurring/41/skip-next": () => {
        throw new ApiResponseError(409, detail);
      },
    });
    render(<RecurringPage />);
    const dialog = await open("Skip next: Rent");
    fireEvent.click(within(dialog).getByRole("button", { name: "Skip" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByText(detail)).toBeInTheDocument();
    expect(listLoadsAfter("POST /api/v1/recurring/41/skip-next")).toBeGreaterThanOrEqual(1);
  });

  it("F5: a second confirm click while the POST is in flight sends nothing", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    mockApi({ "POST /api/v1/recurring/41/skip-next": () => gate.then(() => ({ id: 1 })) });
    render(<RecurringPage />);
    const dialog = await open("Skip next: Rent");
    const confirm = within(dialog).getByRole("button", { name: "Skip" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    fireEvent.click(within(dialog).getAllByRole("button").at(-1)!);

    expect(calls().filter((c) => c.key.includes("skip-next"))).toHaveLength(1);
    await act(async () => { release(); await gate; });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(calls().filter((c) => c.key.includes("skip-next"))).toHaveLength(1);
  });
});

describe("recurring page: edit next amount (TBD-273)", () => {
  async function openEdit() {
    const dialog = await open("Edit next amount: Rent");
    const input = within(dialog).getByLabelText("Amount for 2026-10-01") as HTMLInputElement;
    return { dialog, input, save: within(dialog).getByRole("button", { name: "Save amount" }) };
  }

  it("F6: materialises first, then PUTs only the amount to the RETURNED id", async () => {
    mockApi({
      "POST /api/v1/recurring/41/materialise-next": () => ({ id: 777 }),
      "PUT /api/v1/transactions/777": () => ({ id: 777 }),
    });
    render(<RecurringPage />);
    const { dialog, input, save } = await openEdit();
    expect(input.value).toBe("1200.00");
    expect(dialog.textContent).toContain(
      `Change the amount of "Rent" on 2026-10-01 only. The series stays at ${signed(1200)}.`,
    );
    fireEvent.change(input, { target: { value: "1150.50" } });
    fireEvent.click(save);

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const keys = calls().map((c) => c.key);
    const m = keys.indexOf("POST /api/v1/recurring/41/materialise-next");
    const p = keys.indexOf("PUT /api/v1/transactions/777");
    expect(m).toBeGreaterThanOrEqual(0);
    expect(p).toBeGreaterThan(m);
    expect(keys.filter((k) => k.startsWith("PUT"))).toEqual(["PUT /api/v1/transactions/777"]);
    expect(JSON.parse(String(calls()[m].body))).toEqual({ occurrence_date: "2026-10-01" });
    const body = JSON.parse(String(calls()[p].body));
    expect(Object.keys(body)).toEqual(["amount"]);
    expect(Number(body.amount)).toBe(1150.5);
    expect(listLoadsAfter("PUT /api/v1/transactions/777")).toBeGreaterThanOrEqual(1);
    expect(
      screen.getByText(`"Rent" on 2026-10-01 is now ${signed(1150.5)}. Later occurrences stay at ${signed(1200)}.`),
    ).toBeInTheDocument();
  });

  const ABOVE_ZERO = "Enter an amount above 0.";
  const DIGITS = "Enter an amount with up to 10 digits and 2 decimals.";
  it.each([
    ["0", ABOVE_ZERO],
    ["abc", ABOVE_ZERO],
    ["1.234", DIGITS],
    ["99999999999", DIGITS],
    ["1200", null],
    ["1200.00", null],
  ])("F7: amount %j sends no request and Save is disabled", async (value, message) => {
    mockApi();
    render(<RecurringPage />);
    const { input, save } = await openEdit();
    const before = calls().length;
    fireEvent.change(input, { target: { value } });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(calls().length).toBe(before);
    if (message) {
      expect(input).toHaveAttribute("aria-invalid", "true");
      expect(screen.getByText(message)).toBeInTheDocument();
      expect(screen.queryByText(message === DIGITS ? ABOVE_ZERO : DIGITS)).toBeNull();
    } else {
      expect(input).not.toHaveAttribute("aria-invalid", "true");
    }
  });

  it("F7 control: a valid different amount enables Save", async () => {
    mockApi();
    render(<RecurringPage />);
    const { input, save } = await openEdit();
    fireEvent.change(input, { target: { value: "99.99" } });
    expect(save).not.toBeDisabled();
    expect(input).not.toHaveAttribute("aria-invalid", "true");
  });

  it("F4: a materialise failure shows the server message verbatim and reloads", async () => {
    const detail = "That occurrence is no longer next. Refresh and try again.";
    mockApi({
      "POST /api/v1/recurring/41/materialise-next": () => {
        throw new ApiResponseError(409, detail);
      },
    });
    render(<RecurringPage />);
    const { input, save } = await openEdit();
    fireEvent.change(input, { target: { value: "10" } });
    fireEvent.click(save);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByText(detail)).toBeInTheDocument();
    expect(calls().some((c) => c.key.startsWith("PUT"))).toBe(false);
    expect(listLoadsAfter("POST /api/v1/recurring/41/materialise-next")).toBeGreaterThanOrEqual(1);
  });

  it("C3: a materialise with no HTTP response says to refresh, and reloads", async () => {
    mockApi({
      "POST /api/v1/recurring/41/materialise-next": () => {
        throw new TypeError("Failed to fetch");
      },
    });
    render(<RecurringPage />);
    const { input, save } = await openEdit();
    fireEvent.change(input, { target: { value: "10" } });
    fireEvent.click(save);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByText("Failed to fetch. Refresh to check whether it was created.")).toBeInTheDocument();
    expect(calls().some((c) => c.key.startsWith("PUT"))).toBe(false);
    expect(listLoadsAfter("POST /api/v1/recurring/41/materialise-next")).toBeGreaterThanOrEqual(1);
  });

  it("F8: a PUT failure after materialise shows the partial copy, reloads, never retries", async () => {
    mockApi({
      "POST /api/v1/recurring/41/materialise-next": () => ({ id: 778 }),
      "PUT /api/v1/transactions/778": () => {
        throw new ApiResponseError(422, "Amount is too large.");
      },
    });
    render(<RecurringPage />);
    const { input, save } = await openEdit();
    fireEvent.change(input, { target: { value: "10" } });
    fireEvent.click(save);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(
      screen.getByText(
        `The 2026-10-01 occurrence was created at ${signed(1200)}, but the new amount didn't save: Amount is too large. Edit it on the Transactions page.`,
      ),
    ).toBeInTheDocument();
    expect(calls().filter((c) => c.key.includes("materialise-next"))).toHaveLength(1);
    expect(listLoadsAfter("PUT /api/v1/transactions/778")).toBeGreaterThanOrEqual(1);
  });
});
