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

const SAVINGS_ACCT = {
  id: 2,
  name: "Savings",
  account_type_id: 2,
  account_type_name: "Savings",
  account_type_slug: "savings",
  balance: 5000,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: false,
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

function setupRefs(opts: { withSecondAccount?: boolean } = {}) {
  const apiFetchMock = vi.mocked(apiFetch);
  apiFetchMock.mockReset();
  const accounts = opts.withSecondAccount ? [ACCT, SAVINGS_ACCT] : [ACCT];
  apiFetchMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/v1/accounts")) return accounts as never;
    if (url.startsWith("/api/v1/categories")) return [CAT] as never;
    if (url === "/api/v1/transactions") return { id: 99 } as never;
    if (url === "/api/v1/transactions/transfer") return { id: 100 } as never;
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
    "/reports",
    "/reports/abc123",
    "/plans",
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

describe("AppShellAddTransactionCta quick-add menu", () => {
  it("renders both the primary CTA and the chevron toggle", async () => {
    setupRefs();
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    expect(
      screen.getByTestId("appshell-add-transaction-cta"),
    ).toBeInTheDocument();
    const toggle = screen.getByTestId("appshell-quick-add-menu-toggle");
    expect(toggle).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-haspopup", "menu");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("chevron toggle meets the 44px touch-target floor on both axes", async () => {
    setupRefs();
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    const toggle = screen.getByTestId("appshell-quick-add-menu-toggle");
    // Both axes must clear 44px per DESIGN.md touch-target rule. The
    // primary CTA's test above already asserts min-h-[44px]; the
    // chevron has a smaller default footprint so we assert both.
    expect(toggle.className).toContain("min-h-[44px]");
    expect(toggle.className).toContain("min-w-[44px]");
  });

  it("opens the menu when the chevron is clicked and shows both items", async () => {
    setupRefs();
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    expect(screen.queryByTestId("appshell-quick-add-menu")).toBeNull();
    fireEvent.click(screen.getByTestId("appshell-quick-add-menu-toggle"));
    await waitFor(() => {
      expect(
        screen.getByTestId("appshell-quick-add-menu"),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByTestId("appshell-quick-add-menu-transaction"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("appshell-quick-add-menu-transfer"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("appshell-quick-add-menu-toggle"),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("clicking the Transfer menu item opens the transfer panel", async () => {
    setupRefs({ withSecondAccount: true });
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    fireEvent.click(screen.getByTestId("appshell-quick-add-menu-toggle"));
    await waitFor(() => {
      expect(
        screen.getByTestId("appshell-quick-add-menu-transfer"),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("appshell-quick-add-menu-transfer"));
    await waitFor(() => {
      expect(screen.getByTestId("add-transfer-panel")).toBeInTheDocument();
    });
    expect(screen.getByRole("dialog")).toHaveTextContent("Add transfer");
    expect(screen.getByLabelText("From account")).toBeInTheDocument();
    expect(screen.getByLabelText("To account")).toBeInTheDocument();
  });

  it("submitting the transfer form posts to the transfer endpoint and dispatches the refresh event", async () => {
    const apiFetchMock = setupRefs({ withSecondAccount: true });
    const dispatchSpy = vi.spyOn(window, "dispatchEvent");
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    fireEvent.click(screen.getByTestId("appshell-quick-add-menu-toggle"));
    await waitFor(() => {
      expect(
        screen.getByTestId("appshell-quick-add-menu-transfer"),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("appshell-quick-add-menu-transfer"));
    await waitFor(() => {
      expect(screen.getByLabelText("To account")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("To account"), {
      target: { value: String(SAVINGS_ACCT.id) },
    });
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "120.00" },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^Save$/i }));
    });

    await waitFor(() => {
      const call = apiFetchMock.mock.calls.find(
        ([url]) => url === "/api/v1/transactions/transfer",
      );
      expect(call).toBeTruthy();
    });
    await waitFor(() => {
      const calls = dispatchSpy.mock.calls
        .map((c) => c[0])
        .filter(
          (e): e is Event =>
            e instanceof Event && e.type === "pfv:transaction-added",
        );
      expect(calls.length).toBeGreaterThanOrEqual(1);
    });

    dispatchSpy.mockRestore();
  });

  it("closes the menu on Escape and returns focus to the chevron", async () => {
    setupRefs();
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    fireEvent.click(screen.getByTestId("appshell-quick-add-menu-toggle"));
    await waitFor(() => {
      expect(
        screen.getByTestId("appshell-quick-add-menu"),
      ).toBeInTheDocument();
    });
    await act(async () => {
      fireEvent.keyDown(document, { key: "Escape" });
    });
    await waitFor(() => {
      expect(screen.queryByTestId("appshell-quick-add-menu")).toBeNull();
    });
    expect(document.activeElement).toBe(
      screen.getByTestId("appshell-quick-add-menu-toggle"),
    );
  });

  it("Tab inside the menu closes it and returns focus to the chevron (P2 menu-Tab contract)", async () => {
    setupRefs();
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    fireEvent.click(screen.getByTestId("appshell-quick-add-menu-toggle"));
    await waitFor(() => {
      expect(
        screen.getByTestId("appshell-quick-add-menu-transaction"),
      ).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(
        screen.getByTestId("appshell-quick-add-menu-transaction"),
      );
    });
    await act(async () => {
      fireEvent.keyDown(screen.getByTestId("appshell-quick-add-menu"), {
        key: "Tab",
      });
    });
    await waitFor(() => {
      expect(screen.queryByTestId("appshell-quick-add-menu")).toBeNull();
    });
    expect(document.activeElement).toBe(
      screen.getByTestId("appshell-quick-add-menu-toggle"),
    );
  });

  it("ArrowDown / ArrowUp move focus between menu items", async () => {
    setupRefs();
    await act(async () => {
      render(<AppShellAddTransactionCta />);
    });
    fireEvent.click(screen.getByTestId("appshell-quick-add-menu-toggle"));
    await waitFor(() => {
      expect(
        screen.getByTestId("appshell-quick-add-menu-transaction"),
      ).toBeInTheDocument();
    });
    // Auto-focus lands on item 1 after the open tick.
    await waitFor(() => {
      expect(document.activeElement).toBe(
        screen.getByTestId("appshell-quick-add-menu-transaction"),
      );
    });
    fireEvent.keyDown(screen.getByTestId("appshell-quick-add-menu"), {
      key: "ArrowDown",
    });
    expect(document.activeElement).toBe(
      screen.getByTestId("appshell-quick-add-menu-transfer"),
    );
    fireEvent.keyDown(screen.getByTestId("appshell-quick-add-menu"), {
      key: "ArrowUp",
    });
    expect(document.activeElement).toBe(
      screen.getByTestId("appshell-quick-add-menu-transaction"),
    );
  });
});
