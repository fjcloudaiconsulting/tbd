// The account currency field (TBD-325) — component coverage.
//
// `lib/currencies.test.ts` covers the label helpers as pure functions. This
// file covers the two decisions the page itself makes, which review flagged as
// the most regression-prone and entirely unfenced:
//
//   1. The field is a SELECT while the org has no accounts, and a read-only
//      display once it has one. Getting this backwards offers a choice the
//      server refuses with a 409.
//   2. The POST body carries the org's settled currency, not the picker's stale
//      local state (`page.tsx`: `currency: orgCurrency ?? acctCurrency`).
//
// Mirrors the mocking pattern in accounts-payment-source.test.tsx.

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { renderWithSWR } from "../utils/render-with-swr";

import AccountsPage from "@/app/accounts/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/accounts",
}));

const USER = {
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner",
  org_id: 1, org_name: "Acme", billing_cycle_day: 1, is_superadmin: false,
  is_active: true, mfa_enabled: false, password_set: true,
  subscription_status: null, subscription_plan: null, trial_end: null,
  allow_manual_balance_adjustment: false,
};

const ACCOUNT_TYPES = [
  { id: 1, name: "Checking", slug: "checking", is_system: true, account_count: 0 },
];

// ⚠ JPY, not EUR. EUR is the field's default, so an assertion against an
// EUR account cannot tell "read the org's currency" from "fell back to the
// default" — the two produce the same string.
const EXISTING_JPY = {
  id: 10, name: "Tokyo", account_type_id: 1, account_type_name: "Checking",
  account_type_slug: "checking", balance: "150.00", currency: "JPY",
  is_active: true, is_default: true, close_day: null,
  opening_balance: "0.00", opening_balance_date: "2026-01-01",
  payment_source_account_id: null,
};

function mockApi(accounts: unknown[]) {
  vi.mocked(apiFetch).mockImplementation((path: string) => {
    if (path === "/api/v1/account-types") return Promise.resolve(ACCOUNT_TYPES);
    if (path === "/api/v1/accounts") return Promise.resolve(accounts);
    if (path.startsWith("/api/v1/accounts/") && path.endsWith("/reconcile")) {
      return Promise.resolve({});
    }
    if (path.startsWith("/api/v1/transactions")) {
      return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
    }
    return Promise.resolve([]);
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    refresh: vi.fn(),
    logout: vi.fn(),
    login: vi.fn(),
  } as never);
});

async function openCreateForm() {
  fireEvent.click(await screen.findByRole("button", { name: /Add Account/i }));
}

describe("an org with no accounts chooses its currency", () => {
  it("renders a select, not a free-text box", async () => {
    // The pre-TBD-325 field was `<input type="text" maxLength={3}>`, which let
    // a typo create a second currency. A regression to that would still find a
    // labelled control, so assert the TAG, not merely that the field exists.
    mockApi([]);
    renderWithSWR(<AccountsPage />);
    await openCreateForm();

    const field = await screen.findByLabelText(/^Currency$/i);
    expect(field.tagName).toBe("SELECT");
    expect(field).not.toHaveAttribute("readonly");
  });

  it("offers the common currencies with their symbols", async () => {
    mockApi([]);
    renderWithSWR(<AccountsPage />);
    await openCreateForm();

    const field = await screen.findByLabelText(/^Currency$/i);
    expect(within(field).getByRole("option", { name: /€ EUR/ })).toBeInTheDocument();
    expect(within(field).getByRole("option", { name: /\$ USD/ })).toBeInTheDocument();
    // No symbol exists for CHF, so it must still render with its code and name
    // rather than being dropped or rendering "CHF CHF".
    expect(
      within(field).getByRole("option", { name: /^CHF · Swiss Franc$/ }),
    ).toBeInTheDocument();
  });

  it("submits the chosen currency", async () => {
    mockApi([]);
    renderWithSWR(<AccountsPage />);
    await openCreateForm();

    fireEvent.change(await screen.findByLabelText(/^Account name$/i), {
      target: { value: "First" },
    });
    fireEvent.change(await screen.findByLabelText(/^Type$/), { target: { value: "1" } });
    fireEvent.change(await screen.findByLabelText(/^Currency$/i), {
      target: { value: "JPY" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create Account/i }));

    await waitFor(() => {
      const post = vi
        .mocked(apiFetch)
        .mock.calls.find(
          ([path, init]) => path === "/api/v1/accounts" && init?.method === "POST",
        );
      expect(post).toBeTruthy();
      expect(JSON.parse(String(post![1]?.body)).currency).toBe("JPY");
    });
  });
});

describe("an org that already has an account cannot choose again", () => {
  it("renders the settled currency read-only, with its symbol", async () => {
    mockApi([EXISTING_JPY]);
    renderWithSWR(<AccountsPage />);
    await openCreateForm();

    const field = await screen.findByLabelText(/^Currency$/i);
    expect(field.tagName).toBe("INPUT");
    expect(field).toHaveAttribute("readonly");
    expect(field).toHaveValue("¥ JPY");
  });

  it("submits the org's currency, never the picker's default", async () => {
    // ⚠ THE FENCE THAT MATTERS MOST HERE. `acctCurrency` is still "EUR" in
    // local state — the read-only branch never updates it. If the submit read
    // that instead of `orgCurrency`, every second account in a non-EUR org
    // would POST EUR and be refused with a 409 the user cannot act on.
    mockApi([EXISTING_JPY]);
    renderWithSWR(<AccountsPage />);
    await openCreateForm();

    fireEvent.change(await screen.findByLabelText(/^Account name$/i), {
      target: { value: "Second" },
    });
    fireEvent.change(await screen.findByLabelText(/^Type$/), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: /Create Account/i }));

    await waitFor(() => {
      const post = vi
        .mocked(apiFetch)
        .mock.calls.find(
          ([path, init]) => path === "/api/v1/accounts" && init?.method === "POST",
        );
      expect(post).toBeTruthy();
      expect(JSON.parse(String(post![1]?.body)).currency).toBe("JPY");
    });
  });
});
