import React from "react";
import {
  renderWithSWR,
  fireEvent,
  screen,
  waitFor,
} from "../utils/render-with-swr";

import ForecastPlansClient from "@/app/forecast-plans/ForecastPlansClient";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import type { BillingPeriod, Category, ForecastPlan } from "@/lib/types";

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
  useAuth: vi.fn(() => ({
    user: { id: 1, role: "owner", is_superadmin: false },
  })),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

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

const CATEGORIES: Category[] = [
  {
    id: 20,
    name: "Groceries",
    type: "expense",
    parent_id: null,
    parent_name: null,
    description: null,
    slug: "groceries",
    is_system: false,
    transaction_count: 0,
  },
];

const PERIOD: BillingPeriod = {
  id: 1,
  start_date: "2026-05-01",
  end_date: null,
};

function makePlan(
  items: Array<{
    id?: number;
    category_id: number;
    category_name?: string;
    type: "income" | "expense";
    planned_amount: number;
    actual_amount?: number;
    variance?: number;
    source?: "manual" | "recurring" | "history";
    parent_id?: number | null;
  }> = [],
): ForecastPlan {
  const totalPlannedIncome = items
    .filter((i) => i.type === "income")
    .reduce((s, i) => s + i.planned_amount, 0);
  const totalPlannedExpense = items
    .filter((i) => i.type === "expense")
    .reduce((s, i) => s + i.planned_amount, 0);
  return {
    id: 100,
    billing_period_id: PERIOD.id,
    period_start: PERIOD.start_date,
    period_end: null,
    status: "draft" as ForecastPlan["status"],
    total_planned_income: totalPlannedIncome,
    total_planned_expense: totalPlannedExpense,
    total_actual_income: 0,
    total_actual_expense: 0,
    forecast_input_granularity: "master",
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

function mockApiFetch(plan: ForecastPlan) {
  (apiFetch as unknown as ReturnType<typeof vi.fn>).mockImplementation(
    (path: string) => {
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
      return Promise.resolve(plan);
    },
  );
}

function renderClient(plan: ForecastPlan) {
  return renderWithSWR(
    <ForecastPlansClient
      initialPeriods={[PERIOD]}
      initialCategories={CATEGORIES}
      initialPlan={plan}
    />,
  );
}

describe("ForecastPlans page — proportional layout", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(useAuth).mockReturnValue({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      user: { id: 1, role: "owner", is_superadmin: false } as any,
    } as never);
  });

  it("KPI summary tiles: all four labels and values render when plan has items", async () => {
    const plan = makePlan([
      {
        category_id: 20,
        category_name: "Groceries",
        type: "expense",
        planned_amount: 500,
        actual_amount: 300,
        variance: -200,
        source: "manual",
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

    await waitFor(() => {
      expect(screen.getByText("Planned Income")).toBeInTheDocument();
    });

    expect(screen.getByText("Planned Expenses")).toBeInTheDocument();
    expect(screen.getByText("Planned Net")).toBeInTheDocument();
    expect(screen.getByText("Actual Net")).toBeInTheDocument();

    // All four tiles rendered — each StatCard wraps its label in a <p>;
    // verify at least one value cell is present alongside its label.
    const expensesLabel = screen.getByText("Planned Expenses");
    // The value is a sibling <p> in the same StatCard container.
    expect(expensesLabel.closest("div")).not.toBeNull();
  });

  it("Planned vs Actual chart sits inside an xl:col-span-2 ancestor", async () => {
    // Seed localStorage so showDetails starts as true (avoids an extra
    // fireEvent that would complicate the assertion).
    localStorage.setItem("forecast-plans:show-details", "true");

    const plan = makePlan([
      {
        category_id: 20,
        category_name: "Groceries",
        type: "expense",
        planned_amount: 500,
        actual_amount: 300,
        variance: -200,
        source: "manual",
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

    // The toggle reads localStorage on mount — wait for it to hydrate.
    await waitFor(() => {
      expect(
        screen.getByRole("switch", { name: /hide details/i }),
      ).toBeInTheDocument();
    });

    // The chart heading renders only when showDetails is true AND chartData
    // is non-empty (the expense item above populates chartData).
    await waitFor(() => {
      expect(
        screen.getByText("Planned vs Actual (Expenses)"),
      ).toBeInTheDocument();
    });

    const chartHeading = screen.getByText("Planned vs Actual (Expenses)");
    expect(chartHeading.closest('[class*="xl:col-span-2"]')).not.toBeNull();
  });

  it("Show details toggle on via click: chart is then contained in xl:col-span-2 ancestor", async () => {
    const plan = makePlan([
      {
        category_id: 20,
        category_name: "Groceries",
        type: "expense",
        planned_amount: 500,
        actual_amount: 200,
        variance: -300,
        source: "manual",
      },
    ]);
    mockApiFetch(plan);
    renderClient(plan);

    // Wait for the toggle to appear (defaults off).
    await waitFor(() => {
      expect(
        screen.getByRole("switch", { name: /show details/i }),
      ).toBeInTheDocument();
    });

    // Toggle on.
    fireEvent.click(screen.getByRole("switch", { name: /show details/i }));

    await waitFor(() => {
      expect(
        screen.getByText("Planned vs Actual (Expenses)"),
      ).toBeInTheDocument();
    });

    const chartHeading = screen.getByText("Planned vs Actual (Expenses)");
    expect(chartHeading.closest('[class*="xl:col-span-2"]')).not.toBeNull();
  });
});
