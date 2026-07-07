/**
 * Accounts page — SWR reference-data auth gate (SWR Phase 2).
 *
 * Accounts now come from the shared `useAccounts` hook, whose `enabled` gate is
 * `!loading && !!user`. Until auth resolves the SWR key is null, so no request
 * is issued — the auth-race guard that keeps a token-less fetch from 401/403ing
 * on a hard refresh. Once auth resolves, the accounts request fires.
 */
import React from "react";

import { renderWithSWR, screen, waitFor, act } from "@/tests/utils/render-with-swr";
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

function accountsFetchCount(): number {
  return vi.mocked(apiFetch).mock.calls.filter(([url]) =>
    typeof url === "string" && url.startsWith("/api/v1/accounts"),
  ).length;
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [] as never;
    if (url.startsWith("/api/v1/account-types")) return [] as never;
    return null as never;
  });
});

describe("AccountsPage SWR auth gate", () => {
  it("does not fetch accounts while auth is still loading", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: null, loading: true } as never);
    renderWithSWR(<AccountsPage />);

    // Flush microtasks so SWR's mount effect gets a real chance to fire a
    // fetch; the gate must keep the key null so none is issued.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(accountsFetchCount()).toBe(0);
  });

  it("fetches accounts once auth resolves", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: USER, loading: false } as never);
    renderWithSWR(<AccountsPage />);

    await waitFor(() => expect(accountsFetchCount()).toBeGreaterThan(0));
  });
});

describe("AccountsPage spinner waits for both loads", () => {
  it("keeps the spinner until the SWR accounts request settles, even after aux loads", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: USER, loading: false } as never);

    // Account types + pending (the aux load) resolve immediately, but the
    // accounts SWR request is held in flight. fetching = !auxLoaded ||
    // !accountsSettled, so the spinner must stay until accounts settle too.
    let resolveAccounts!: (v: unknown) => void;
    const accountsInFlight = new Promise((res) => { resolveAccounts = res; });
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.startsWith("/api/v1/accounts")) return accountsInFlight as never;
      if (url.startsWith("/api/v1/account-types")) return [] as never;
      return null as never;
    });

    renderWithSWR(<AccountsPage />);

    // Let the aux load settle; accounts is still pending → spinner stays.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByRole("status", { name: /loading/i })).toBeInTheDocument();

    // Accounts settle → spinner clears.
    await act(async () => { resolveAccounts([]); });
    await waitFor(() =>
      expect(screen.queryByRole("status", { name: /loading/i })).toBeNull(),
    );
  });
});
