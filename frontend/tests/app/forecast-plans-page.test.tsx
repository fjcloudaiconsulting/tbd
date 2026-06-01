import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { SWRConfig } from "swr";

import ForecastPlansClient from "@/app/forecast-plans/ForecastPlansClient";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { BillingPeriod, Category, ForecastPlan } from "@/lib/types";

// The page itself is now an async Server Component (RSC) — it calls
// getServerSession(), parallel-fetches initial categories/periods/plan,
// and hands them down to <ForecastPlansClient />. Vitest + jsdom can't
// natively render async server components, and the page contains no
// interactive UI of its own beyond the redirect/seed wiring. We mount
// the client directly with synthetic initial props instead; the wiring
// itself is exercised by the production build's `server-only` import
// boundary on `lib/auth-server.ts` (a client-side leak would fail
// `npm run build`).

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

// The client now reads useAuth() to gate the admin-only build-granularity
// control. Default to an admin user; individual tests can override.
vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(() => ({
    user: { id: 1, role: "owner", is_superadmin: false },
  })),
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

// Master + sub categories of both types so we can prove the dropdown
// shows masters AND subs that match the selected type.
const CATEGORIES: Category[] = [
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
  {
    id: 22, name: "Restaurant", type: "expense", parent_id: 20,
    parent_name: "Groceries", description: null, slug: "restaurant",
    is_system: false, transaction_count: 0,
  },
];

const PERIOD: BillingPeriod = {
  id: 1, start_date: "2026-05-01", end_date: null,
};

function makePlan(
  items: Array<{
    id?: number; category_id: number; category_name?: string;
    type: "income" | "expense"; planned_amount: number;
    source?: "manual" | "recurring" | "history";
    parent_id?: number | null; actual_amount?: number; variance?: number;
  }> = [],
  granularity: "master" | "subcategory" = "master",
): ForecastPlan {
  return {
    id: 100,
    billing_period_id: PERIOD.id,
    period_start: PERIOD.start_date,
    period_end: null,
    status: "draft" as ForecastPlan["status"],
    total_planned_income: 0,
    total_planned_expense: 0,
    total_actual_income: 0,
    total_actual_expense: 0,
    forecast_input_granularity: granularity,
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

// All apiFetch traffic from the client is mutating (ensure-future POST,
// /populate, /refresh-from-sources, item CRUD, plan lifecycle). Plus, the
// post-mount ensure-future block re-fetches /billing-periods. Default
// mock implementation handles all of these and echoes back the supplied
// plan on plan-mutating writes so the test can assert UI updates.
function mockApiFetch(plan: ForecastPlan) {
  (apiFetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(
    (path: string, init?: RequestInit) => {
      if (path.startsWith("/api/v1/settings/billing-periods/ensure-future")) {
        return Promise.resolve([]);
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
      if (path.includes("/populate")) {
        return Promise.resolve(plan);
      }
      void init;
      return Promise.resolve(plan);
    },
  );
}

function renderClient(plan: ForecastPlan | null) {
  // Each render gets a fresh SWR cache so state doesn't leak between
  // tests (the default cache is module-scoped and would let an earlier
  // empty-plan test paint stale data into a later with-items test).
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <ForecastPlansClient
        initialPeriods={[PERIOD]}
        initialCategories={CATEGORIES}
        initialPlan={plan}
      />
    </SWRConfig>,
  );
}

describe("ForecastPlansClient — dropdown + refresh", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    // Default to an admin user; the non-admin test overrides per-case.
    vi.mocked(useAuth).mockReturnValue({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      user: { id: 1, role: "owner", is_superadmin: false } as any,
    } as never);
  });

  it("Bug 1: income type shows master AND sub categories in the dropdown", async () => {
    mockApiFetch(makePlan());
    renderClient(makePlan());

    // Initial paint already has the plan via fallbackData; the
    // Auto-populate button must be present on a draft.
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
    const plan = makePlan([
      {
        category_id: 12,
        category_name: "Side gigs",
        type: "income",
        planned_amount: 500,
        source: "manual",
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

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
    const plan = makePlan([
      {
        category_id: 10,
        category_name: "Salary",
        type: "income",
        planned_amount: 3000,
        source: "manual",
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

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

  it("subcategory mode: after adding one sub, OTHER subs of the same master stay enabled", async () => {
    // Subcategory mode. Supermarket (21, sub of Groceries 20) is already
    // added. The core regression: Restaurant (22, the OTHER sub of
    // Groceries) MUST remain enabled — only the exact added sub and the
    // master are disabled.
    const plan = makePlan(
      [
        {
          category_id: 21,
          category_name: "Supermarket",
          type: "expense",
          planned_amount: 200,
          parent_id: 20,
          source: "manual",
        },
      ],
      "subcategory",
    );
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /\+ Add Item/ })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: /\+ Add Item/ }));
    // formType defaults to expense — no change needed.
    const combobox = screen.getByRole("combobox", {
      name: /Plan item category/i,
    });
    fireEvent.focus(combobox);

    const listbox = await screen.findByRole("listbox");
    const options = within(listbox).getAllByRole("option");

    const restaurant = options.find((b) =>
      (b.textContent ?? "").includes("Restaurant"),
    ) as HTMLButtonElement | undefined;
    const supermarket = options.find((b) =>
      (b.textContent ?? "").includes("Supermarket"),
    ) as HTMLButtonElement | undefined;

    // The other sub stays enabled (the bug being fixed) — previously the
    // whole master tree locked after one sub was added.
    expect(restaurant).toBeTruthy();
    expect(restaurant!.disabled).toBe(false);
    // The exact added sub is disabled.
    expect(supermarket!.disabled).toBe(true);
    // (Groceries master is not a pickable option because CategorySelect
    // hides masters that have children, so only its subs are selectable.)
  });

  it("subcategory mode: adding a sub posts the SUB category id (no roll-up to master)", async () => {
    const plan = makePlan([], "subcategory");
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /\+ Add Item/ })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: /\+ Add Item/ }));

    const combobox = screen.getByRole("combobox", {
      name: /Plan item category/i,
    });
    fireEvent.focus(combobox);
    const listbox = await screen.findByRole("listbox");
    fireEvent.click(within(listbox).getByText("Supermarket"));

    fireEvent.change(screen.getByLabelText("Planned Amount"), {
      target: { value: "200" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Add$/ }));

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/v1/forecast-plans/100/items"),
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"category_id":21'),
        }),
      );
    });
  });

  it("master mode: adding a sub rolls up to the master id (legacy behavior preserved)", async () => {
    const plan = makePlan([], "master");
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /\+ Add Item/ })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: /\+ Add Item/ }));

    const combobox = screen.getByRole("combobox", {
      name: /Plan item category/i,
    });
    fireEvent.focus(combobox);
    const listbox = await screen.findByRole("listbox");
    fireEvent.click(within(listbox).getByText("Supermarket"));

    fireEvent.change(screen.getByLabelText("Planned Amount"), {
      target: { value: "200" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Add$/ }));

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/v1/forecast-plans/100/items"),
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"category_id":20'),
        }),
      );
    });
  });

  it("subcategory mode: list groups subs under their master with the master total", async () => {
    const plan = makePlan(
      [
        {
          category_id: 21, category_name: "Supermarket", type: "expense",
          planned_amount: 200, parent_id: 20, source: "manual",
          actual_amount: 0, variance: -200,
        },
        {
          category_id: 22, category_name: "Restaurant", type: "expense",
          planned_amount: 150, parent_id: 20, source: "manual",
          actual_amount: 0, variance: -150,
        },
      ],
      "subcategory",
    );
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(screen.getByTestId("fp-master-20")).toBeTruthy();
    });
    const masterRow = screen.getByTestId("fp-master-20");
    // Master header shows the master name and the summed planned total.
    expect(masterRow.textContent).toContain("Groceries");
    expect(masterRow.textContent).toContain("350");
    // Both subs render as their own rows.
    expect(screen.getByText("Supermarket")).toBeTruthy();
    expect(screen.getByText("Restaurant")).toBeTruthy();
  });

  it("the build-granularity control persists the org setting and is hidden for non-admins", async () => {
    const plan = makePlan([], "master");
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(
        screen.getByRole("radiogroup", { name: /build granularity/i }),
      ).toBeTruthy();
    });

    fireEvent.click(screen.getByRole("radio", { name: /Subcategories/ }));

    await waitFor(() => {
      expect(apiFetch).toHaveBeenCalledWith(
        "/api/v1/settings",
        expect.objectContaining({
          method: "PUT",
          body: expect.stringContaining("forecast_input_granularity"),
        }),
      );
    });
  });

  it("the build-granularity control is hidden for non-admin members", async () => {
    vi.mocked(useAuth).mockReturnValue({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      user: { id: 2, role: "member", is_superadmin: false } as any,
    } as never);
    const plan = makePlan([], "master");
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Auto-populate" })).toBeTruthy();
    });
    expect(
      screen.queryByRole("radiogroup", { name: /build granularity/i }),
    ).toBeNull();
  });

  it("Bug 5: Refresh from sources renders a confirm modal with the locked copy", async () => {
    const plan = makePlan([
      {
        category_id: 20,
        category_name: "Groceries",
        type: "expense",
        planned_amount: 500,
        source: "recurring",
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

    // Refresh from sources is gated behind the Show details toggle
    // (defaults off post-PR-B forecasts UX restructure). Flip it on
    // before asserting the button is visible.
    await waitFor(() => {
      expect(screen.getByRole("switch", { name: /show details/i })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("switch", { name: /show details/i }));

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
    mockApiFetch(makePlan());
    renderClient(makePlan());

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
    mockApiFetch(makePlan());
    renderClient(makePlan());

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

  it("Show details toggle defaults off: Variance/Source columns and Refresh-from-sources are hidden on a draft plan", async () => {
    const plan = makePlan([
      {
        category_id: 20,
        category_name: "Groceries",
        type: "expense",
        planned_amount: 500,
        source: "history",
        actual_amount: 300,
        variance: -200,
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(screen.getByText("Groceries")).toBeTruthy();
    });

    // Toggle is off by default — labelled "Show details".
    const toggle = screen.getByRole("switch", { name: /show details/i });
    expect(toggle.getAttribute("aria-checked")).toBe("false");

    // Variance + Source columns hidden.
    expect(screen.queryByText("Variance")).toBeNull();
    expect(screen.queryByText("Source")).toBeNull();
    // Auto label (source) is also hidden.
    expect(screen.queryByText("Auto")).toBeNull();
    // Refresh-from-sources hidden on draft when details off.
    expect(
      screen.queryByRole("button", { name: "Refresh from sources" }),
    ).toBeNull();
  });

  it("Show details toggle persists in localStorage and rehydrates on reload", async () => {
    mockApiFetch(makePlan());
    const { unmount } = renderClient(makePlan());

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: /show details/i })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("switch", { name: /show details/i }));

    // Persisted to localStorage.
    await waitFor(() => {
      expect(localStorage.getItem("forecast-plans:show-details")).toBe("true");
    });
    unmount();

    // Re-render with the same localStorage state — the toggle should
    // come back as "Hide details" (i.e. on).
    renderClient(makePlan());
    await waitFor(() => {
      expect(screen.getByRole("switch", { name: /hide details/i })).toBeTruthy();
    });
  });

  it("Finalized plan + details on: Refresh from sources opens 'Edit and refresh plan' modal with locked copy and confirm label", async () => {
    const finalized: ForecastPlan = {
      ...makePlan([
        {
          category_id: 20,
          category_name: "Groceries",
          type: "expense",
          planned_amount: 500,
          source: "recurring",
          actual_amount: 200,
          variance: -300,
        },
      ]),
      status: "active",
    };

    mockApiFetch(finalized);
    renderClient(finalized);

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: /show details/i })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("switch", { name: /show details/i }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Refresh from sources" }),
      ).toBeTruthy();
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Refresh from sources" }),
    );

    // Modal renders with the spec-locked copy.
    expect(
      screen.getByText("Edit and refresh plan", { selector: "h3" }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        /This will revert the plan to draft, replace auto-generated rows with fresh data, and keep lines you added or edited yourself\./,
      ),
    ).toBeTruthy();
    // Confirm label is "Edit and refresh", not generic "Confirm".
    expect(
      screen.getByRole("button", { name: /^Edit and refresh$/ }),
    ).toBeTruthy();
  });

  it("Finalized refresh confirm calls /revert then /refresh-from-sources in order", async () => {
    const finalized: ForecastPlan = {
      ...makePlan([
        {
          category_id: 20,
          category_name: "Groceries",
          type: "expense",
          planned_amount: 500,
          source: "recurring",
          actual_amount: 200,
          variance: -300,
        },
      ]),
      status: "active",
    };

    const calls: string[] = [];
    (apiFetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (path: string) => {
        if (path.startsWith("/api/v1/settings/billing-periods/ensure-future"))
          return Promise.resolve([]);
        if (path === "/api/v1/settings/billing-periods")
          return Promise.resolve([PERIOD]);
        if (path.startsWith("/api/v1/forecast-plans?"))
          return Promise.resolve(finalized);
        if (path.includes("/revert")) {
          calls.push("revert");
          return Promise.resolve({ ...finalized, status: "draft" });
        }
        if (path.startsWith("/api/v1/forecast-plans/refresh-from-sources")) {
          calls.push("refresh");
          return Promise.resolve({ ...finalized, status: "draft" });
        }
        return Promise.resolve(finalized);
      },
    );

    renderClient(finalized);

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: /show details/i })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("switch", { name: /show details/i }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Refresh from sources" }),
      ).toBeTruthy();
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Refresh from sources" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /^Edit and refresh$/ }),
    );

    await waitFor(() => {
      expect(calls).toEqual(["revert", "refresh"]);
    });
  });

  it("Finalized refresh: revert ok + refresh fail leaves plan in draft and surfaces error (no silent fallback)", async () => {
    const finalized: ForecastPlan = {
      ...makePlan([
        {
          category_id: 20,
          category_name: "Groceries",
          type: "expense",
          planned_amount: 500,
          source: "recurring",
          actual_amount: 200,
          variance: -300,
        },
      ]),
      status: "active",
    };
    const draftCopy: ForecastPlan = { ...finalized, status: "draft" };
    // Backend state — flips from active to draft after the revert call
    // succeeds. A background SWR revalidate (or post-write reload) must
    // see the new state, not the original `finalized` object.
    let currentBackendPlan: ForecastPlan = finalized;

    (apiFetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (path: string) => {
        if (path.startsWith("/api/v1/settings/billing-periods/ensure-future"))
          return Promise.resolve([]);
        if (path === "/api/v1/settings/billing-periods")
          return Promise.resolve([PERIOD]);
        if (path.startsWith("/api/v1/forecast-plans?"))
          return Promise.resolve(currentBackendPlan);
        if (path.includes("/revert")) {
          currentBackendPlan = draftCopy;
          return Promise.resolve(draftCopy);
        }
        if (path.startsWith("/api/v1/forecast-plans/refresh-from-sources"))
          return Promise.reject(new Error("Refresh blew up"));
        return Promise.resolve(currentBackendPlan);
      },
    );

    renderClient(finalized);

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: /show details/i })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("switch", { name: /show details/i }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Refresh from sources" }),
      ).toBeTruthy();
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Refresh from sources" }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: /^Edit and refresh$/ }),
    );

    // Error surfaces. Plan remains in draft (Edit Plan disappears,
    // Auto-populate appears since drafts can populate again).
    await waitFor(() => {
      expect(screen.getByText(/Refresh blew up/)).toBeTruthy();
    });
    expect(
      screen.queryByRole("button", { name: /^Edit Plan$/ }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Auto-populate" }),
    ).toBeTruthy();
  });

  it("PR #146 #1: source=history renders an honest 'Auto' label, not 'Avg (3mo)'", async () => {
    // populate now also surfaces categories whose only signal is in the
    // current period (one-off furniture purchase, etc.) but writes them
    // with source=history. "Avg (3mo)" lied; rename to "Auto" — broader
    // and matches the L3.10 import preview "Auto" badge.
    const plan = makePlan([
      {
        category_id: 20,
        category_name: "Groceries",
        type: "expense",
        planned_amount: 250,
        source: "history",
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

    // Source column is gated behind Show details (off by default
    // post-PR-B). Flip it on so the label is visible.
    await waitFor(() => {
      expect(screen.getByRole("switch", { name: /show details/i })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("switch", { name: /show details/i }));

    await waitFor(() => {
      expect(screen.getByText("Groceries")).toBeTruthy();
    });

    // The honest label appears.
    expect(screen.getAllByText("Auto").length).toBeGreaterThan(0);
    // The misleading old label is gone.
    expect(screen.queryByText("Avg (3mo)")).toBeNull();
  });

  it("ensure-future refresh keeps the user on their selected period when a future stub appears newest-first", async () => {
    // Regression for the periodIdx-shift bug. The backend lists
    // periods newest-first; ensure-future may create a brand-new
    // future stub that lands at index 0. Naively calling
    // `setPeriods(fresh)` while keeping the stale `periodIdx` would
    // silently shift the user off the current period onto the
    // future stub. The fix re-derives the index by `start_date`.

    const CURRENT_OPEN: BillingPeriod = {
      id: 5,
      start_date: "2026-05-01",
      end_date: null,
    };
    const PAST: BillingPeriod = {
      id: 4,
      start_date: "2026-04-01",
      end_date: "2026-04-30",
    };
    const FUTURE_STUB: BillingPeriod = {
      id: 6,
      start_date: "2026-06-01",
      end_date: "2026-06-30",
    };

    // The RSC saw [CURRENT_OPEN, PAST] (no future stub yet). Current
    // is at index 0 because end_date===null. That's where the user
    // starts.
    const initialPeriods = [CURRENT_OPEN, PAST];
    const planForCurrent: ForecastPlan = {
      ...makePlan([
        {
          category_id: 20,
          category_name: "Groceries",
          type: "expense",
          planned_amount: 500,
          source: "manual",
        },
      ]),
      period_start: CURRENT_OPEN.start_date,
    };

    // After ensure-future fires, the backend now lists FUTURE_STUB at
    // index 0 (newest-first), shifting CURRENT_OPEN to index 1 and
    // PAST to index 2. Without the selection-preservation fix, the
    // page would render the FUTURE_STUB (its start_date and the
    // "future" label) instead of the user's selected current period.
    const freshPeriods = [FUTURE_STUB, CURRENT_OPEN, PAST];

    (apiFetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (path: string) => {
        if (path.startsWith("/api/v1/settings/billing-periods/ensure-future")) {
          return Promise.resolve([]);
        }
        if (path === "/api/v1/settings/billing-periods") {
          return Promise.resolve(freshPeriods);
        }
        if (path.startsWith("/api/v1/forecast-plans?")) {
          return Promise.resolve(planForCurrent);
        }
        return Promise.resolve(planForCurrent);
      },
    );

    render(
      <SWRConfig value={{ provider: () => new Map() }}>
        <ForecastPlansClient
          initialPeriods={initialPeriods}
          initialCategories={CATEGORIES}
          initialPlan={planForCurrent}
        />
      </SWRConfig>,
    );

    // Wait for ensure-future + the periods re-fetch to settle. The
    // mock returns a 3-period list; once that lands, the page
    // re-renders with the refreshed list. If the bug were live,
    // periodIdx would still be 0 — which now points at FUTURE_STUB.
    await waitFor(() => {
      expect(
        (apiFetch as unknown as ReturnType<typeof vi.fn>).mock.calls.filter(
          (c) =>
            typeof c[0] === "string" &&
            c[0] === "/api/v1/settings/billing-periods",
        ).length,
      ).toBeGreaterThanOrEqual(1);
    });

    // The selected-period label must still show CURRENT_OPEN's
    // start_date AND its small "current" badge in the period nav —
    // NOT FUTURE_STUB's start_date or its "future" badge.
    await waitFor(() => {
      expect(screen.getByText(CURRENT_OPEN.start_date)).toBeTruthy();
    });
    // The "current" pill in the period nav is an exact-match span;
    // the contextual guidance paragraph uses the word "current"
    // elsewhere, so target the badge precisely.
    expect(
      screen.getAllByText((_, el) => el?.textContent?.trim() === "current"),
    ).not.toHaveLength(0);
    expect(screen.queryByText(FUTURE_STUB.start_date)).toBeNull();
    // No "future" badge — that would mean the page slid to the stub.
    expect(
      screen.queryByText((_, el) => el?.textContent?.trim() === "future"),
    ).toBeNull();
  });

  // ── Inline row edit Cancel button (PR #forecast-plans-cancel) ─────────
  //
  // Before this PR the inline-edit Cancel was a tiny `text-xs text-muted`
  // link nestled next to Save — easy to miss. The user-direct complaint
  // was "the only way to bail out of an edit session is to MAKE an edit
  // first, which then offers a Discard." Promoting Cancel to a proper
  // button (and locking the contract in tests) makes the bail-out path
  // obvious without forcing the user through a destructive Discard.

  it("inline row edit: Cancel restores the row to view mode without a server PUT", async () => {
    const plan = makePlan([
      {
        category_id: 20,
        category_name: "Groceries",
        type: "expense",
        planned_amount: 250,
        source: "manual",
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(screen.getByText("Groceries")).toBeTruthy();
    });

    // Clear `apiFetch` history captured by the initial mount (ensure-future
    // POST + billing-periods GET) so the post-Cancel assertion is exact.
    (apiFetch as unknown as ReturnType<typeof vi.fn>).mockClear();
    mockApiFetch(plan);

    // Enter edit mode on the Groceries row.
    const editButtons = screen.getAllByRole("button", { name: /^Edit$/ });
    fireEvent.click(editButtons[0]);

    // The edit input renders pre-filled with the persisted planned amount.
    const input = screen.getByDisplayValue("250") as HTMLInputElement;
    expect(input).toBeTruthy();

    // The Cancel button is rendered as a proper button (not a link-style
    // span) so it's visually discoverable.
    const cancel = screen.getByTestId("fp-edit-cancel-1");
    expect(cancel.tagName).toBe("BUTTON");

    // Click Cancel — no edit was typed yet.
    fireEvent.click(cancel);

    // The edit input is gone (row is back to view mode) and no PUT
    // fired against the items endpoint.
    await waitFor(() => {
      expect(screen.queryByDisplayValue("250")).toBeNull();
    });
    const putCalls = (
      apiFetch as unknown as ReturnType<typeof vi.fn>
    ).mock.calls.filter(
      (c) =>
        typeof c[0] === "string" &&
        c[0].includes("/api/v1/forecast-plans/") &&
        c[0].includes("/items/") &&
        (c[1] as RequestInit | undefined)?.method === "PUT",
    );
    expect(putCalls).toHaveLength(0);
  });

  it("inline row edit: Cancel after typing discards the in-flight edit (no PUT)", async () => {
    const plan = makePlan([
      {
        category_id: 20,
        category_name: "Groceries",
        type: "expense",
        planned_amount: 250,
        source: "manual",
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(screen.getByText("Groceries")).toBeTruthy();
    });

    (apiFetch as unknown as ReturnType<typeof vi.fn>).mockClear();
    mockApiFetch(plan);

    // Enter edit mode.
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit$/ })[0]);
    const input = screen.getByDisplayValue("250") as HTMLInputElement;

    // Type a different value (in-flight local edit only — no PATCH yet).
    fireEvent.change(input, { target: { value: "999" } });
    expect((screen.getByDisplayValue("999") as HTMLInputElement).value).toBe(
      "999",
    );

    // Cancel discards.
    fireEvent.click(screen.getByTestId("fp-edit-cancel-1"));

    // The edit input is gone, the persisted "250" amount is shown again,
    // and no PUT fired.
    await waitFor(() => {
      expect(screen.queryByDisplayValue("999")).toBeNull();
    });
    expect(screen.getByText("250.00")).toBeTruthy();
    const putCalls = (
      apiFetch as unknown as ReturnType<typeof vi.fn>
    ).mock.calls.filter(
      (c) =>
        typeof c[0] === "string" &&
        c[0].includes("/api/v1/forecast-plans/") &&
        c[0].includes("/items/") &&
        (c[1] as RequestInit | undefined)?.method === "PUT",
    );
    expect(putCalls).toHaveLength(0);
  });

  it("inline row edit: re-opening edit after Cancel shows the persisted value, not the discarded draft", async () => {
    const plan = makePlan([
      {
        category_id: 20,
        category_name: "Groceries",
        type: "expense",
        planned_amount: 250,
        source: "manual",
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(screen.getByText("Groceries")).toBeTruthy();
    });

    // Edit → type a draft → Cancel.
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit$/ })[0]);
    fireEvent.change(screen.getByDisplayValue("250") as HTMLInputElement, {
      target: { value: "999" },
    });
    fireEvent.click(screen.getByTestId("fp-edit-cancel-1"));

    // Re-enter edit on the same row — the input must be seeded from the
    // persisted plan, not from the discarded "999" draft.
    fireEvent.click(screen.getAllByRole("button", { name: /^Edit$/ })[0]);
    await waitFor(() => {
      expect(
        (screen.getByDisplayValue("250") as HTMLInputElement).value,
      ).toBe("250");
    });
    expect(screen.queryByDisplayValue("999")).toBeNull();
  });
});
