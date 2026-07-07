/**
 * Accounts page — initial fetch error state (#521 deferred follow-up).
 *
 * When the INITIAL accounts fetch fails (no data in the SWR cache at all),
 * the page must show an error state with a Retry action — NOT the
 * "No accounts yet" empty state, which is reserved for a successful fetch
 * that genuinely returned zero accounts. Retry revalidates the SWR request;
 * a retry that fails again keeps the error state visible.
 */
import React from "react";

import { renderWithSWR, screen, waitFor, fireEvent } from "@/tests/utils/render-with-swr";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/lib/pagination", async () => {
  const actual = await vi.importActual<typeof import("@/lib/pagination")>("@/lib/pagination");
  return { ...actual, fetchAll: vi.fn().mockResolvedValue([]) };
});

import AccountsPage from "@/app/accounts/page";

const USER = {
  id: 1, username: "user", email: "user@example.com",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const, org_id: 1, org_name: "Org",
  billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null, allow_manual_balance_adjustment: false,
};

const ACCOUNT_TYPE = {
  id: 1, name: "Checking", slug: "checking",
  is_system: true, account_count: 1,
};

const ACCOUNT = {
  id: 10, name: "Main Checking", account_type_id: 1,
  account_type_name: "Checking", balance: 100, currency: "EUR",
  close_day: null, is_active: true, is_default: true,
  opening_balance: "0.00", opening_balance_date: null,
};

// Mock apiFetch so the accounts endpoint fails `failures` times before
// succeeding with `payload`. Account types always resolve (with one type,
// so a wrongly-rendered empty state would say "No accounts yet…", the
// string the assertions below guard against).
function mockAccountsFailingTimes(failures: number, payload: unknown = [ACCOUNT]) {
  let accountsCalls = 0;
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/account-types")) return [ACCOUNT_TYPE] as never;
    if (url.startsWith("/api/v1/accounts")) {
      accountsCalls += 1;
      if (accountsCalls <= failures) throw new Error("network down");
      return payload as never;
    }
    return null as never;
  });
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(useAuth).mockReturnValue({ user: USER, loading: false } as never);
});

describe("AccountsPage initial fetch error", () => {
  it("shows the error state (not the empty state) when the initial accounts fetch fails", async () => {
    mockAccountsFailingTimes(Infinity);
    renderWithSWR(<AccountsPage />);

    await waitFor(() =>
      expect(screen.getByTestId("accounts-initial-load-error")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
    // The empty state must NOT render on a failed fetch.
    expect(screen.queryByText(/no accounts yet/i)).toBeNull();
    // Neither should the loading spinner keep spinning.
    expect(screen.queryByRole("status", { name: /loading/i })).toBeNull();
  });

  it("retry refetches and renders the list on success", async () => {
    mockAccountsFailingTimes(1);
    renderWithSWR(<AccountsPage />);

    await waitFor(() =>
      expect(screen.getByTestId("accounts-initial-load-error")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() =>
      expect(screen.getByTestId("account-row-10")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("accounts-initial-load-error")).toBeNull();
  });

  it("keeps the error state when the retry fails again", async () => {
    mockAccountsFailingTimes(Infinity);
    renderWithSWR(<AccountsPage />);

    await waitFor(() =>
      expect(screen.getByTestId("accounts-initial-load-error")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /retry/i }));

    // The retry re-issues the fetch; once it settles (button back to
    // "Retry", enabled) the error state must still be shown — a failing
    // retry must not fall through to the empty state or a silent spinner.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled(),
    );
    expect(screen.getByTestId("accounts-initial-load-error")).toBeInTheDocument();
    expect(screen.queryByText(/no accounts yet/i)).toBeNull();
    expect(screen.queryByRole("status", { name: /loading/i })).toBeNull();
  });

  it("shows a genuinely-empty success as the empty state, not the error state", async () => {
    mockAccountsFailingTimes(0, []);
    renderWithSWR(<AccountsPage />);

    await waitFor(() =>
      expect(screen.getByText(/no accounts yet/i)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("accounts-initial-load-error")).toBeNull();
  });
});
