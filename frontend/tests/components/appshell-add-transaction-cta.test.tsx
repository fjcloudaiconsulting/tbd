import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import AppShellAddTransactionCta, {
  shouldShowAddTransactionCta,
} from "@/components/AppShellAddTransactionCta";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

const ACCT = {
  id: 1,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Checking",
  account_type_slug: "checking",
  balance: 1000,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const CAT = {
  id: 10,
  name: "Groceries",
  type: "expense" as const,
  parent_id: null,
  parent_name: null,
  description: null,
  slug: "groceries",
  is_system: false,
  transaction_count: 0,
};

function setupRefs() {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return [ACCT] as never;
    if (url.startsWith("/api/v1/categories")) return [CAT] as never;
    if (url === "/api/v1/transactions") return { id: 99 } as never;
    return null as never;
  });
  return apiFetchMock;
}

describe("shouldShowAddTransactionCta route gate", () => {
  // Each entry is the locked allow-list from the redesign brief. Keep
  // these in lockstep with SHOW_ON inside AppShellAddTransactionCta.
  const SHOW_PATHS: ReadonlyArray<string> = [
    "/dashboard",
    "/transactions",
    "/transactions/123",
    "/accounts",
    "/accounts/45/edit",
    "/categories",
    "/forecast-plans",
    "/forecast-plans/2026-05-01",
    "/budgets",
    "/recurring",
  ];

  // Settings, admin, and system trees stay clear of the brass CTA: they
  // have their own flows (security, audit, plans) and a transaction
  // shortcut would be out of context there.
  const HIDE_PATHS: ReadonlyArray<string> = [
    "/settings",
    "/settings/security",
    "/settings/organization",
    "/settings/billing",
    "/admin",
    "/admin/orgs",
    "/admin/audit",
    "/system",
    "/system/plans",
    "/login",
    "/register",
    "/profile",
    "/import",
    "/docs",
    "/",
  ];

  for (const path of SHOW_PATHS) {
    it(`shows on ${path}`, () => {
      expect(shouldShowAddTransactionCta(path)).toBe(true);
    });
  }

  for (const path of HIDE_PATHS) {
    it(`hides on ${path}`, () => {
      expect(shouldShowAddTransactionCta(path)).toBe(false);
    });
  }

  it("hides when pathname is null (defensive)", () => {
    expect(shouldShowAddTransactionCta(null)).toBe(false);
  });
});

describe("AppShellAddTransactionCta component", () => {
  it("renders the CTA with an accessible name", async () => {
    setupRefs();
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    const cta = screen.getByTestId("appshell-add-transaction-cta");
    expect(cta).toBeInTheDocument();
    // Accessible name comes from aria-label so the icon-only mobile
    // variant keeps the same affordance label as the desktop one.
    expect(cta).toHaveAttribute("aria-label", "New transaction");
  });

  it("uses the brass btnPrimary styling and meets the 44px touch target", async () => {
    setupRefs();
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    const cta = screen.getByTestId("appshell-add-transaction-cta");
    // btnPrimary token class. Asserting the class string lets the test
    // detect drift if someone reaches for raw Tailwind utilities.
    expect(cta.className).toContain("bg-accent");
    expect(cta.className).toContain("text-accent-text");
    // Touch-target floor per DESIGN.md.
    expect(cta.className).toContain("min-h-[44px]");
  });

  it("renders the visible label on desktop (sm:inline) inside the button", async () => {
    setupRefs();
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    // The visible text is wrapped in a `hidden sm:inline` span; in JSDOM
    // we still find it as text content.
    expect(screen.getByText("New transaction")).toBeInTheDocument();
  });

  it("opens the SlideInPanel on click and surfaces the form", async () => {
    setupRefs();
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    expect(screen.queryByTestId("add-transaction-panel")).toBeNull();
    fireEvent.click(screen.getByTestId("appshell-add-transaction-cta"));
    await waitFor(() => {
      expect(screen.getByTestId("add-transaction-panel")).toBeInTheDocument();
    });
    // The dialog header is "Add transaction"; the trigger label is
    // "New transaction". Two distinct registers, one for the page-level
    // CTA, one for the panel headline.
    expect(screen.getByRole("dialog")).toHaveTextContent("Add transaction");
    expect(screen.getByLabelText("Description")).toBeInTheDocument();
  });

  it("dispatches `pfv:transaction-added` after a successful save", async () => {
    setupRefs();
    const dispatchSpy = vi.spyOn(window, "dispatchEvent");
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    fireEvent.click(screen.getByTestId("appshell-add-transaction-cta"));
    await waitFor(() => {
      expect(screen.getByLabelText("Description")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Coffee" },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "4.50" },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    await waitFor(() => {
      const calls = dispatchSpy.mock.calls
        .map((c) => c[0])
        .filter((e): e is Event => e instanceof Event && e.type === "pfv:transaction-added");
      expect(calls.length).toBeGreaterThanOrEqual(1);
    });

    dispatchSpy.mockRestore();
  });
});
