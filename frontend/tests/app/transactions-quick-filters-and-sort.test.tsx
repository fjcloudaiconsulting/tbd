import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { FILTERS_KEY_TRANSACTIONS } from "@/lib/hooks/persisted-keys";

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

const ACCT = {
  id: 100, name: "Checking", account_type_id: 1,
  account_type_name: "Checking", account_type_slug: "checking",
  balance: 0, currency: "EUR", is_active: true,
  close_day: null, is_default: true,
};

const CATEGORY = {
  id: 11, name: "Groceries", type: "expense" as const,
  parent_id: null, parent_name: null, description: null,
  slug: "groceries", is_system: false, transaction_count: 0,
};

// Helper: format Date as local YYYY-MM-DD (mirrors lib/format.formatLocalDate
// so tests don't drift in non-UTC test environments).
function isoLocal(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function todayLocal(): string {
  return isoLocal(new Date());
}

type Period = { id: number; start_date: string; end_date: string | null };

function setupApiFetch(periods: Period[] = []) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
    if (url.startsWith("/api/v1/categories")) return [CATEGORY] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) return periods as never;
    if (url.startsWith("/api/v1/transactions"))
      return { items: [], total: 0, limit: 25, offset: 0 } as never;
    return null as never;
  });
  return apiFetchMock;
}

function lastListUrl(mock: ReturnType<typeof vi.mocked<typeof apiFetch>>): string | null {
  for (let i = mock.mock.calls.length - 1; i >= 0; i--) {
    const url = mock.mock.calls[i][0] as string;
    if (typeof url === "string" && url.startsWith("/api/v1/transactions?")) {
      return url;
    }
  }
  return null;
}

function listUrlsAfter(
  mock: ReturnType<typeof vi.mocked<typeof apiFetch>>,
  startIndex: number,
): string[] {
  const out: string[] = [];
  for (let i = startIndex; i < mock.mock.calls.length; i++) {
    const url = mock.mock.calls[i][0] as string;
    if (typeof url === "string" && url.startsWith("/api/v1/transactions?")) {
      out.push(url);
    }
  }
  return out;
}

describe("TransactionsPage — quick filter buttons", () => {
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

  afterEach(() => {
    cleanup();
  });

  it("Today button sends date_from=date_to=today and clears any prior period filter", async () => {
    const periods: Period[] = [
      { id: 7, start_date: "2026-04-01", end_date: "2026-04-30" },
    ];
    const mock = setupApiFetch(periods);

    render(<TransactionsPage />);

    // Wait for initial fetches.
    await waitFor(() => {
      expect(lastListUrl(mock)).not.toBeNull();
    });

    // Pre-select a closed period via the dropdown to seed the state, then
    // confirm clicking Today swaps it to a date_from/date_to pair.
    const periodSelect = await screen.findByLabelText("Billing period");
    fireEvent.change(periodSelect, { target: { value: "7" } });

    await waitFor(() => {
      const url = lastListUrl(mock);
      expect(url).toContain("date_from=2026-04-01");
    });

    const todayBtn = screen.getByRole("button", { name: "Today" });
    const startIdx = mock.mock.calls.length;
    fireEvent.click(todayBtn);

    const today = todayLocal();
    await waitFor(() => {
      const after = listUrlsAfter(mock, startIdx);
      expect(after.length).toBeGreaterThan(0);
      const last = after[after.length - 1];
      expect(last).toContain(`date_from=${today}`);
      expect(last).toContain(`date_to=${today}`);
    });
  });

  it("This Month button sends date_from=first-of-month and date_to=last-of-month", async () => {
    const mock = setupApiFetch();

    render(<TransactionsPage />);

    await waitFor(() => expect(lastListUrl(mock)).not.toBeNull());

    const startIdx = mock.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "This Month" }));

    const now = new Date();
    const firstOfMonth = isoLocal(new Date(now.getFullYear(), now.getMonth(), 1));
    const lastOfMonth = isoLocal(new Date(now.getFullYear(), now.getMonth() + 1, 0));
    await waitFor(() => {
      const after = listUrlsAfter(mock, startIdx);
      expect(after.length).toBeGreaterThan(0);
      const last = after[after.length - 1];
      expect(last).toContain(`date_from=${firstOfMonth}`);
      expect(last).toContain(`date_to=${lastOfMonth}`);
    });
  });

  it("This Week button sends date_from=Monday-of-this-week and date_to=today", async () => {
    const mock = setupApiFetch();

    render(<TransactionsPage />);

    await waitFor(() => expect(lastListUrl(mock)).not.toBeNull());

    const startIdx = mock.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "This Week" }));

    const now = new Date();
    const day = now.getDay();
    const diff = day === 0 ? 6 : day - 1;
    const mon = new Date(now);
    mon.setDate(now.getDate() - diff);
    const monIso = isoLocal(mon);
    const today = todayLocal();

    await waitFor(() => {
      const after = listUrlsAfter(mock, startIdx);
      expect(after.length).toBeGreaterThan(0);
      const last = after[after.length - 1];
      expect(last).toContain(`date_from=${monIso}`);
      expect(last).toContain(`date_to=${today}`);
    });
  });

  it("All button drops every date constraint, including a previously-selected period", async () => {
    const periods: Period[] = [
      { id: 7, start_date: "2026-04-01", end_date: "2026-04-30" },
    ];
    const mock = setupApiFetch(periods);

    render(<TransactionsPage />);
    await waitFor(() => expect(lastListUrl(mock)).not.toBeNull());

    // Seed: pick a closed period from the dropdown.
    const periodSelect = await screen.findByLabelText("Billing period");
    fireEvent.change(periodSelect, { target: { value: "7" } });
    await waitFor(() => {
      const url = lastListUrl(mock);
      expect(url).toContain("date_from=2026-04-01");
    });

    const startIdx = mock.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "All" }));

    await waitFor(() => {
      const after = listUrlsAfter(mock, startIdx);
      expect(after.length).toBeGreaterThan(0);
      const last = after[after.length - 1];
      expect(last).not.toContain("date_from=");
      expect(last).not.toContain("date_to=");
    });
  });

  it("does not expose open/current periods as quick filter or dropdown options", async () => {
    const periods: Period[] = [
      { id: 5, start_date: "2026-04-01", end_date: "2026-04-30" },
      { id: 6, start_date: "2026-05-01", end_date: null },
    ];
    setupApiFetch(periods);

    render(<TransactionsPage />);

    expect(screen.queryByRole("button", { name: /current period/i })).toBeNull();

    const periodSelect = await screen.findByLabelText("Billing period");
    expect(periodSelect).toHaveTextContent("2026-04-01");
    expect(periodSelect).not.toHaveTextContent("2026-05-01");
    expect(periodSelect).not.toHaveTextContent("current");
  });

  it("clears a persisted open period after billing periods load", async () => {
    window.localStorage.setItem(
      FILTERS_KEY_TRANSACTIONS,
      JSON.stringify({
        filterAccount: "",
        filterCategory: "",
        filterType: "",
        filterStatus: "",
        filterDateFrom: "",
        filterDateTo: "",
        filterSearch: "",
        filterPeriod: "6",
      }),
    );
    const periods: Period[] = [
      { id: 5, start_date: "2026-04-01", end_date: "2026-04-30" },
      { id: 6, start_date: "2026-05-01", end_date: null },
    ];
    const mock = setupApiFetch(periods);

    render(<TransactionsPage />);

    await screen.findByLabelText("Billing period");
    await waitFor(() => {
      const periodsCallIndex = mock.mock.calls.findIndex(
        (call) =>
          typeof call[0] === "string" &&
          (call[0] as string).startsWith("/api/v1/settings/billing-periods"),
      );
      expect(periodsCallIndex).toBeGreaterThanOrEqual(0);
      const txUrlsAfterPeriodsLoad = listUrlsAfter(mock, periodsCallIndex + 1);
      expect(txUrlsAfterPeriodsLoad.length).toBeGreaterThan(0);
      expect(
        txUrlsAfterPeriodsLoad.filter((url) =>
          url.includes("date_from=2026-05-01"),
        ),
      ).toEqual([]);
      const stored = window.localStorage.getItem(FILTERS_KEY_TRANSACTIONS);
      expect(stored).not.toBeNull();
      expect(JSON.parse(stored!).filterPeriod).toBe("");
    });
  });

  it("Manually editing the From-date input clears any pinned period filter", async () => {
    const periods: Period[] = [{ id: 7, start_date: "2026-04-01", end_date: "2026-04-30" }];
    const mock = setupApiFetch(periods);

    render(<TransactionsPage />);
    await waitFor(() => expect(lastListUrl(mock)).not.toBeNull());

    // Pin a closed period via the dropdown first.
    const periodSelect = await screen.findByLabelText("Billing period");
    fireEvent.change(periodSelect, { target: { value: "7" } });
    await waitFor(() => {
      const url = lastListUrl(mock);
      expect(url).toContain("date_from=2026-04-01");
    });

    // Now type a different From-date — this must drop the period and use
    // the typed value instead.
    const startIdx = mock.mock.calls.length;
    const fromInput = screen.getByLabelText("From date");
    fireEvent.change(fromInput, { target: { value: "2026-03-15" } });

    await waitFor(() => {
      const after = listUrlsAfter(mock, startIdx);
      expect(after.length).toBeGreaterThan(0);
      const last = after[after.length - 1];
      expect(last).toContain("date_from=2026-03-15");
    });
  });
});

describe("TransactionsPage — sort direction across columns (Option B)", () => {
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

  afterEach(() => {
    cleanup();
  });

  function getDesktopHeader(name: string): HTMLElement {
    // The desktop header buttons are scoped to the hidden-md+ wrapper. The
    // useful identifier in jsdom is the visible text including the indicator
    // arrow. We just match by leading text.
    const buttons = screen
      .getAllByRole("button")
      .filter((b) => b.textContent?.startsWith(name));
    if (buttons.length === 0) {
      throw new Error(`No header button starting with "${name}"`);
    }
    return buttons[0];
  }

  async function awaitHeadersReady(
    mock: ReturnType<typeof vi.mocked<typeof apiFetch>>,
  ) {
    // The page kicks off two fetches in parallel: loadRefs() (sets accounts,
    // categories, billing periods) and loadTransactions() (the row data).
    // When loadRefs finishes, periods updates which re-creates the
    // loadTransactions callback, which re-runs the load effect and flips
    // `fetching` back to true briefly. Headers disappear during that window.
    //
    // A "two consecutive polls" stability check isn't enough — under CI's
    // slower scheduler the flicker can land between awaitHeadersReady
    // returning and the next getDesktopHeader call. Instead, gate on a
    // deterministic signal: the page has issued a SECOND /transactions
    // request (the post-periods refetch) AND headers are currently visible.
    // After the second transactions response resolves, fetching settles
    // permanently because nothing else updates loadTransactions' deps.
    const required = ["Date", "Description", "Account", "Category", "Status", "Amount"];
    await waitFor(
      () => {
        const txCalls = mock.mock.calls.filter(
          (c) => typeof c[0] === "string" && (c[0] as string).startsWith("/api/v1/transactions"),
        ).length;
        if (txCalls < 2) {
          throw new Error(`waiting for post-periods refetch (saw ${txCalls})`);
        }
        const labels = screen.queryAllByRole("button").map((b) => b.textContent ?? "");
        const allPresent = required.every((name) =>
          labels.some((t) => t.startsWith(name)),
        );
        if (!allPresent) {
          throw new Error("not all headers rendered yet");
        }
      },
      { timeout: 5000, interval: 25 },
    );
  }

  it("Default state: Date column shows desc indicator", async () => {
    const mock = setupApiFetch();
    render(<TransactionsPage />);

    await awaitHeadersReady(mock);
    expect(getDesktopHeader("Date").textContent).toMatch(/Date.*↓/);
  });

  it("Switching from Date (desc) to Description applies the column's natural default (asc)", async () => {
    const mock = setupApiFetch();
    render(<TransactionsPage />);

    await awaitHeadersReady(mock);

    fireEvent.click(getDesktopHeader("Description"));

    await waitFor(() => {
      expect(getDesktopHeader("Description").textContent).toMatch(/Description.*↑/);
    });
  });

  it("Switching from Description (asc) to Amount applies Amount's natural default (desc), not asc", async () => {
    const mock = setupApiFetch();
    render(<TransactionsPage />);

    await awaitHeadersReady(mock);

    // First click description so we're on an asc column.
    fireEvent.click(getDesktopHeader("Description"));
    await waitFor(() => {
      expect(getDesktopHeader("Description").textContent).toMatch(/↑/);
    });

    // Now click Amount — should land on desc (Amount's natural default),
    // NOT inherit Description's asc and NOT default to asc.
    fireEvent.click(getDesktopHeader("Amount"));
    await waitFor(() => {
      expect(getDesktopHeader("Amount").textContent).toMatch(/Amount.*↓/);
    });
  });

  it("Same-column click toggles direction (Date desc -> Date asc -> Date desc)", async () => {
    const mock = setupApiFetch();
    render(<TransactionsPage />);

    await awaitHeadersReady(mock);
    expect(getDesktopHeader("Date").textContent).toMatch(/Date.*↓/);

    fireEvent.click(getDesktopHeader("Date"));
    await waitFor(() => {
      expect(getDesktopHeader("Date").textContent).toMatch(/Date.*↑/);
    });

    fireEvent.click(getDesktopHeader("Date"));
    await waitFor(() => {
      expect(getDesktopHeader("Date").textContent).toMatch(/Date.*↓/);
    });
  });

  it("Switching from Amount (desc) to Status applies Status's natural default (asc)", async () => {
    const mock = setupApiFetch();
    render(<TransactionsPage />);

    await awaitHeadersReady(mock);

    // Land on Amount via its natural default first.
    fireEvent.click(getDesktopHeader("Amount"));
    await waitFor(() => {
      expect(getDesktopHeader("Amount").textContent).toMatch(/↓/);
    });

    fireEvent.click(getDesktopHeader("Status"));
    await waitFor(() => {
      expect(getDesktopHeader("Status").textContent).toMatch(/Status.*↑/);
    });
  });

  it("Mobile tap targets: every sortable header carries min-h-[32px] (WCAG 2.5.8)", async () => {
    const mock = setupApiFetch();
    render(<TransactionsPage />);

    await awaitHeadersReady(mock);

    const required = ["Date", "Description", "Account", "Category", "Status", "Amount"];
    for (const name of required) {
      const btn = getDesktopHeader(name);
      expect(btn.className).toContain("min-h-[32px]");
    }
  });
});
