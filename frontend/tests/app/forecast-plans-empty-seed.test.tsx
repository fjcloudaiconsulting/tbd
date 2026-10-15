import React from "react";
import { renderWithSWR, waitFor, screen, fireEvent, within } from "../utils/render-with-swr";
import ForecastPlansClient from "@/app/forecast-plans/ForecastPlansClient";
import { apiFetch } from "@/lib/api";
import type { BillingPeriod, Category, ForecastPlan } from "@/lib/types";

// TBD-624 F4/F5: the server no longer seeds this island, so everything it
// shows comes from client fetches made as the TAB's own identity.

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/forecast-plans",
  useSearchParams: () => ({ get: () => null }),
}));
vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: vi.fn(() => ({ user: { id: 1, role: "member", is_superadmin: false } })),
}));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  BarChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Bar: () => null, XAxis: () => null, YAxis: () => null, Tooltip: () => null, Cell: () => null,
}));

const OLDER_OPEN: BillingPeriod = { id: 92, start_date: "2020-01-01", end_date: null };
const NEWER_OPEN: BillingPeriod = { id: 93, start_date: "2026-06-01", end_date: null };
const SALARY: Category = {
  id: 10, name: "Salary", type: "income", parent_id: null, parent_name: null,
  description: null, slug: "salary", is_system: false, transaction_count: 0,
};

const PLAN = {
  id: 100, billing_period_id: NEWER_OPEN.id, period_start: NEWER_OPEN.start_date,
  period_end: null, status: "draft", total_planned_income: 0, total_planned_expense: 0,
  total_actual_income: 0, total_actual_expense: 0, forecast_input_granularity: "master",
  items: [],
} as unknown as ForecastPlan;

const api = vi.mocked(apiFetch) as unknown as ReturnType<typeof vi.fn>;
const paths = () => api.mock.calls.map((c) => c[0] as string);

function mountEmpty() {
  return renderWithSWR(
    <ForecastPlansClient initialPeriods={[]} initialCategories={[]} initialPlan={null} />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe("TBD-624 F4: empty seeds, non-admin", () => {
  it("ensure-future 403 does not stop the periods GET; plan keyed on the newest open row", async () => {
    // Kills: chaining the periods GET behind ensure-future (blank page for a
    // non-admin), and losing the TBD-242 newest-open selection on this path.
    api.mockImplementation((p: string) => {
      if (p.startsWith("/api/v1/settings/billing-periods/ensure-future")) {
        return Promise.reject(new Error("403 admin only"));
      }
      if (p === "/api/v1/settings/billing-periods") {
        return Promise.resolve([OLDER_OPEN, NEWER_OPEN]); // oldest first
      }
      if (p === "/api/v1/categories") return Promise.resolve([]);
      return Promise.resolve(null);
    });
    mountEmpty();
    await waitFor(() =>
      expect(paths()).toContain(`/api/v1/forecast-plans?period_start=${NEWER_OPEN.start_date}`),
    );
    expect(paths()).toContain("/api/v1/settings/billing-periods");
    expect(paths()).not.toContain(`/api/v1/forecast-plans?period_start=${OLDER_OPEN.start_date}`);
  });
});

const CLOSED_A: BillingPeriod = { id: 80, start_date: "2026-05-01", end_date: "2026-05-31" };
const CLOSED_B: BillingPeriod = { id: 81, start_date: "2026-04-01", end_date: "2026-04-30" };

function rosterApi(roster: BillingPeriod[]) {
  api.mockImplementation((p: string) => {
    if (p.startsWith("/api/v1/settings/billing-periods/ensure-future")) return Promise.resolve([]);
    if (p === "/api/v1/settings/billing-periods") return Promise.resolve(roster);
    if (p === "/api/v1/categories") return Promise.resolve([]);
    return Promise.resolve(null);
  });
}
const planPaths = () => paths().filter((p) => p.startsWith("/api/v1/forecast-plans?"));

describe("TBD-624 F4b: selection on the fetched roster", () => {
  it("newest-first roster (as the backend returns): picks the newest open row", async () => {
    // Kills: picking the LAST index of the fetched list.
    rosterApi([NEWER_OPEN, OLDER_OPEN]);
    mountEmpty();
    await waitFor(() => expect(planPaths()).toEqual([`/api/v1/forecast-plans?period_start=${NEWER_OPEN.start_date}`]));
  });

  it("no open row: falls back to index 0", async () => {
    // Kills: the no-open fallback becoming the last index (successor of the
    // deleted RSC fence's `?? periods[0]` half).
    rosterApi([CLOSED_A, CLOSED_B]);
    mountEmpty();
    await waitFor(() => expect(planPaths()).toEqual([`/api/v1/forecast-plans?period_start=${CLOSED_A.start_date}`]));
  });
});

describe("TBD-624 F6: periods load state", () => {
  it("periods GET pending: the page shows its loading state", async () => {
    // Kills: an empty seed rendering a bare header while periods are in flight.
    api.mockImplementation((p: string) => {
      if (p === "/api/v1/settings/billing-periods") return new Promise(() => {});
      return Promise.resolve(null);
    });
    mountEmpty();
    expect(await screen.findByRole("status", { name: "Loading" })).toBeTruthy();
  });

  it("periods GET fails: the error is shown, not a blank page", async () => {
    // Kills: swallowing the failure when nothing was seeded.
    api.mockImplementation((p: string) => {
      if (p === "/api/v1/settings/billing-periods") return Promise.reject(new Error("periods boom"));
      return Promise.resolve(null);
    });
    mountEmpty();
    expect(await screen.findByText("periods boom")).toBeTruthy();
  });
});

describe("TBD-624 F5: empty seeds, categories", () => {
  it("categories fetched client-side reach the picker", async () => {
    // Kills: the recovery effect not running on an empty seed.
    api.mockImplementation((p: string) => {
      if (p === "/api/v1/categories") return Promise.resolve([SALARY]);
      if (p === "/api/v1/settings/billing-periods") return Promise.resolve([NEWER_OPEN]);
      if (p.startsWith("/api/v1/forecast-plans?")) return Promise.resolve(PLAN);
      return Promise.resolve(null);
    });
    mountEmpty();
    fireEvent.click(await screen.findByRole("button", { name: /\+ Add Item/ }));
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "income" } });
    fireEvent.focus(screen.getByRole("combobox", { name: /Plan item category/i }));
    const listbox = await screen.findByRole("listbox");
    await waitFor(() =>
      expect(within(listbox).getAllByRole("option").some((o) => o.textContent?.includes("Salary"))).toBe(true),
    );
  });
});
