import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import ForecastPlansPage from "@/app/forecast-plans/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/forecast-plans",
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

// recharts pulls in window.matchMedia and friends; stub the bits the test
// surfaces touch.
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  BarChart: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  Bar: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Cell: () => null,
}));

const USER = {
  id: 1, username: "user", email: "user@example.com",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner" as const, org_id: 1, org_name: "Org",
  billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

// Master + sub categories of both types so we can prove the dropdown
// shows masters AND subs that match the selected type.
const CATEGORIES = [
  // Income side
  {
    id: 10, name: "Salary", type: "income", parent_id: null,
    parent_name: null, description: null, slug: "salary",
    is_system: false, transaction_count: 0,
  },
  {
    id: 11, name: "Bonus", type: "income", parent_id: 10,
    parent_name: "Salary", description: null, slug: "bonus",
    is_system: false, transaction_count: 0,
  },
  {
    id: 12, name: "Side gigs", type: "income", parent_id: null,
    parent_name: null, description: null, slug: "side-gigs",
    is_system: false, transaction_count: 0,
  },
  // Expense side
  {
    id: 20, name: "Groceries", type: "expense", parent_id: null,
    parent_name: null, description: null, slug: "groceries",
    is_system: false, transaction_count: 0,
  },
  {
    id: 21, name: "Supermarket", type: "expense", parent_id: 20,
    parent_name: "Groceries", description: null, slug: "supermarket",
    is_system: false, transaction_count: 0,
  },
];

const PERIOD = {
  id: 1, start_date: "2026-05-01", end_date: null,
};

function makePlan(items: Array<{
  id?: number; category_id: number; category_name?: string;
  type: "income" | "expense"; planned_amount: number;
  source?: "manual" | "recurring" | "history";
  parent_id?: number | null; actual_amount?: number; variance?: number;
}> = []) {
  return {
    id: 100,
    billing_period_id: PERIOD.id,
    period_start: PERIOD.start_date,
    period_end: null,
    status: "draft" as const,
    total_planned_income: 0,
    total_planned_expense: 0,
    total_actual_income: 0,
    total_actual_expense: 0,
    items: items.map((it, idx) => ({
      id: it.id ?? idx + 1,
      plan_id: 100,
      category_id: it.category_id,
      category_name: it.category_name ?? "Cat",
      parent_id: it.parent_id ?? null,
      type: it.type,
      planned_amount: it.planned_amount,
      source: it.source ?? "manual",
      actual_amount: it.actual_amount ?? 0,
      variance: it.variance ?? 0,
    })),
  };
}

function setupAuth() {
  (useAuth as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    user: USER, loading: false, login: vi.fn(), logout: vi.fn(),
    refresh: vi.fn(),
  });
}

function mockInitialFetches(plan: ReturnType<typeof makePlan>) {
  // ensure-future-periods POST happens first, then categories + periods,
  // then GET /forecast-plans?period_start=...
  (apiFetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(
    (path: string, init?: RequestInit) => {
      if (path.startsWith("/api/v1/settings/billing-periods/ensure-future")) {
        return Promise.resolve([]);
      }
      if (path === "/api/v1/categories") {
        return Promise.resolve(CATEGORIES);
      }
      if (path === "/api/v1/settings/billing-periods") {
        return Promise.resolve([PERIOD]);
      }
      if (path.startsWith("/api/v1/forecast-plans?")) {
        return Promise.resolve(plan);
      }
      if (path.startsWith("/api/v1/forecast-plans/refresh-from-sources")) {
        return Promise.resolve(plan);
      }
      // populate is exercised in a separate test
      if (path.includes("/populate")) {
        return Promise.resolve(plan);
      }
      // default — return whatever was requested as empty plan-like
      void init;
      return Promise.resolve(plan);
    },
  );
}

describe("ForecastPlansPage — dropdown + refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    setupAuth();
  });

  it("Bug 1: income type shows master AND sub categories in the dropdown", async () => {
    mockInitialFetches(makePlan());

    render(<ForecastPlansPage />);

    // Wait for the page to render the Auto-populate header button,
    // which means the initial fetches have settled.
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Auto-populate" }),
      ).toBeTruthy();
    });

    // Open the add form
    fireEvent.click(screen.getByRole("button", { name: /\+ Add Item/ }));

    // Switch type to income
    const typeSelect = screen.getByLabelText("Type") as HTMLSelectElement;
    fireEvent.change(typeSelect, { target: { value: "income" } });

    // Open the CategorySelect dropdown
    const combobox = screen.getByRole("combobox", {
      name: /Plan item category/i,
    });
    fireEvent.focus(combobox);

    // Both master "Salary" and sub "Bonus" must be present
    const listbox = await screen.findByRole("listbox");
    expect(within(listbox).getByText("Salary")).toBeTruthy();
    expect(within(listbox).getByText("Bonus")).toBeTruthy();
    expect(within(listbox).getByText("Side gigs")).toBeTruthy();
    // Expense-only categories must NOT be present
    expect(within(listbox).queryByText("Groceries")).toBeNull();
    expect(within(listbox).queryByText("Supermarket")).toBeNull();
  });

  it("Bug 4: a category already in the plan renders as disabled with '(already added)'", async () => {
    // Plan has "Side gigs" (master, no children) already added as income
    mockInitialFetches(
      makePlan([
        {
          category_id: 12,
          category_name: "Side gigs",
          type: "income",
          planned_amount: 500,
          source: "manual",
        },
      ]),
    );

    render(<ForecastPlansPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /\+ Add Item/ })).toBeTruthy();
    });

    fireEvent.click(screen.getByRole("button", { name: /\+ Add Item/ }));
    const typeSelect = screen.getByLabelText("Type") as HTMLSelectElement;
    fireEvent.change(typeSelect, { target: { value: "income" } });

    const combobox = screen.getByRole("combobox", {
      name: /Plan item category/i,
    });
    fireEvent.focus(combobox);

    const listbox = await screen.findByRole("listbox");

    // The Side gigs option still appears in the dropdown and carries the
    // "(already added)" hint instead of vanishing.
    const options = within(listbox).getAllByRole("option");
    const sideGigsButton = options.find((b) =>
      (b.textContent ?? "").includes("Side gigs"),
    ) as HTMLButtonElement | undefined;
    expect(sideGigsButton).toBeTruthy();
    expect(sideGigsButton!.textContent).toContain("(already added)");

    // The button is disabled (cannot be selected)
    expect(sideGigsButton!.disabled).toBe(true);
    expect(sideGigsButton!.getAttribute("aria-disabled")).toBe("true");

    // Click it — the dropdown stays open and no selection happens.
    fireEvent.click(sideGigsButton!);
    expect(screen.queryByRole("listbox")).toBeTruthy();
  });

  it("Bug 4: subcategory of an already-added master is disabled too", async () => {
    // Plan has Salary master already added as income.
    // Bonus is a child of Salary; picking Bonus would roll up to Salary,
    // which is a no-op against the existing item — so Bonus should be
    // shown as disabled too.
    mockInitialFetches(
      makePlan([
        {
          category_id: 10,
          category_name: "Salary",
          type: "income",
          planned_amount: 3000,
          source: "manual",
        },
      ]),
    );

    render(<ForecastPlansPage />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /\+ Add Item/ })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: /\+ Add Item/ }));
    const typeSelect = screen.getByLabelText("Type") as HTMLSelectElement;
    fireEvent.change(typeSelect, { target: { value: "income" } });

    const combobox = screen.getByRole("combobox", {
      name: /Plan item category/i,
    });
    fireEvent.focus(combobox);

    const listbox = await screen.findByRole("listbox");
    const options = within(listbox).getAllByRole("option");
    const bonusButton = options.find((b) =>
      (b.textContent ?? "").includes("Bonus"),
    ) as HTMLButtonElement | undefined;
    expect(bonusButton).toBeTruthy();
    expect(bonusButton!.disabled).toBe(true);
    expect(bonusButton!.textContent).toContain("(already added)");
  });

  it("Bug 5: Refresh from sources renders a confirm modal with the locked copy", async () => {
    mockInitialFetches(
      makePlan([
        {
          category_id: 20,
          category_name: "Groceries",
          type: "expense",
          planned_amount: 500,
          source: "recurring",
        },
      ]),
    );

    render(<ForecastPlansPage />);

    await waitFor(() => {
      expect(screen.getByText("Refresh from sources")).toBeTruthy();
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Refresh from sources" }),
    );

    // Confirm modal copy — title rendered as the dialog heading
    expect(screen.getByText("Refresh from sources", { selector: "h3" }))
      .toBeTruthy();
    expect(
      screen.getByText(
        /This replaces auto-generated rows .*recurring templates, history averages.* with fresh data\. Lines you added or edited yourself stay untouched\./,
      ),
    ).toBeTruthy();

    // Confirm fires the refresh endpoint
    fireEvent.click(screen.getByRole("button", { name: /^Confirm$/ }));

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        expect.stringContaining(
          "/api/v1/forecast-plans/refresh-from-sources?period_start=",
        ),
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("Bug 5: Refresh button is hidden on an empty draft plan", async () => {
    mockInitialFetches(makePlan());

    render(<ForecastPlansPage />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Auto-populate" }),
      ).toBeTruthy();
    });

    expect(
      screen.queryByRole("button", { name: "Refresh from sources" }),
    ).toBeNull();
  });

  it("flipping the form type clears a previously selected category", async () => {
    mockInitialFetches(makePlan());

    render(<ForecastPlansPage />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Auto-populate" }),
      ).toBeTruthy();
    });

    fireEvent.click(screen.getByRole("button", { name: /\+ Add Item/ }));

    // Default formType is "expense". Pick "Supermarket" — an expense
    // sub-category (its master "Groceries" is suppressed in the
    // dropdown when it has children, so the leaf is the user-pickable
    // option here).
    const combobox = screen.getByRole("combobox", {
      name: /Plan item category/i,
    }) as HTMLInputElement;
    fireEvent.focus(combobox);
    const listbox = await screen.findByRole("listbox");
    const supermarketOption = within(listbox)
      .getAllByRole("option")
      .find((o) => (o.textContent ?? "").includes("Supermarket"));
    expect(supermarketOption).toBeTruthy();
    fireEvent.click(supermarketOption!);

    // Combobox should now show "Supermarket"
    expect(combobox.value).toBe("Supermarket");

    // Flip type to income — the stale expense pick must be cleared so a
    // submit can't slip through with a mismatched (income, expense-cat)
    // pair.
    const typeSelect = screen.getByLabelText("Type") as HTMLSelectElement;
    fireEvent.change(typeSelect, { target: { value: "income" } });

    expect(combobox.value).toBe("");
  });

  it("PR #146 #1: source=history renders an honest 'Auto' label, not 'Avg (3mo)'", async () => {
    // populate now also surfaces categories whose only signal is in the
    // current period (one-off furniture purchase, etc.) but writes them
    // with source=history. "Avg (3mo)" lied; rename to "Auto" — broader
    // and matches the L3.10 import preview "Auto" badge.
    mockInitialFetches(
      makePlan([
        {
          category_id: 20,
          category_name: "Groceries",
          type: "expense",
          planned_amount: 250,
          source: "history",
        },
      ]),
    );

    render(<ForecastPlansPage />);

    await waitFor(() => {
      expect(screen.getByText("Groceries")).toBeTruthy();
    });

    // The honest label appears.
    expect(screen.getAllByText("Auto").length).toBeGreaterThan(0);
    // The misleading old label is gone.
    expect(screen.queryByText("Avg (3mo)")).toBeNull();
  });
});
