/**
 * Hide balances page sweep (TBD-527, fences F3 + F5).
 *
 * Each real page is rendered inside the REAL AppShell with a sentinel amount
 * (7373.37) in its fixtures. The test proves the sentinel is on the page
 * (positive control), clicks the real header toggle, and then asserts the
 * sentinel is gone from every text node AND every attribute (`title`,
 * `aria-label`, ...). That catches both a call site that bypasses the
 * formatters (a hand-built `toFixed`) and a component that formats correctly
 * but never repaints on the toggle.
 *
 * ⚠ The assertion after the click is SYNCHRONOUS on purpose. The page is
 * settled first; a `waitFor` after the click would let an unrelated later
 * re-render mask a component that does not subscribe to the toggle.
 *
 * ⚠ LIMITATION: jsdom paints no recharts axis ticks or tooltips, even with
 * `rechartsWithFixedSize`, so a tick formatter is NOT proven here. That is
 * `formatMeasureValue`'s unit fence (F2 in `tests/lib/hide-balances.test.ts`)
 * plus a browser check at the visual gate.
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";

import AccountsPage from "@/app/accounts/page";
import BudgetsPage from "@/app/budgets/page";
import DashboardPage from "@/app/dashboard/page";
import ForecastPlansClient from "@/app/forecast-plans/ForecastPlansClient";
import ReportEditorPage from "@/app/reports/[id]/page";
import TransactionsPage from "@/app/transactions/page";
import { useAuth } from "@/components/auth/AuthProvider";
import NotificationPopover from "@/components/notifications/NotificationPopover";
import { apiFetch } from "@/lib/api";
import { setBalancesHidden } from "@/lib/format";
import { DEFAULT_FEATURES } from "@/lib/features";
import * as reportsApi from "@/lib/reports/api";
import { downloadCsv } from "@/lib/reports/csv";
import type { Notification } from "@/lib/types";

import { ALL_ENTRIES } from "../utils/mock-report-sources";

vi.mock("recharts", async () => {
  const { rechartsWithFixedSize } = await import("@/tests/utils/recharts");
  return rechartsWithFixedSize();
});

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<typeof import("@/components/auth/AuthProvider")>(
    "@/components/auth/AuthProvider",
  );
  return { ...actual, useAuth: vi.fn() };
});

const stableRouter = { push: vi.fn(), replace: vi.fn(), refresh: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/dashboard",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({ id: "10" }),
}));

// Same Canvas stub as `reports-editor-page.test.tsx`: jsdom cannot measure
// react-grid-layout, so the stub renders each widget through the page's own
// `renderWidget`.
vi.mock("@/components/reports/Canvas", () => ({
  default: ({
    layout,
    renderWidget,
  }: {
    layout: { widgets: { id: string }[] };
    renderWidget: (w: { id: string }) => React.ReactNode;
  }) => (
    <div data-testid="reports-canvas">
      {layout.widgets.map((w) => (
        <div key={w.id}>{renderWidget(w as never)}</div>
      ))}
    </div>
  ),
}));

vi.mock("@/lib/reports/api", () => ({
  getReport: vi.fn(),
  saveLayout: vi.fn(),
  runQuery: vi.fn(),
  deleteReport: vi.fn(),
  listVersions: vi.fn(),
  restoreVersion: vi.fn(),
  updateReport: vi.fn(),
  duplicateReport: vi.fn(),
}));

vi.mock("@/lib/reports/csv", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/reports/csv")>();
  return { ...actual, downloadCsv: vi.fn() };
});

const SENTINEL = /7[,.]?373/;
const AMOUNT = 7373.37;

/** Every place the sentinel still shows: text, then any attribute value. */
function leaks(): string[] {
  const hits: string[] = [];
  if (SENTINEL.test(document.body.textContent ?? "")) hits.push("textContent");
  for (const el of Array.from(document.body.querySelectorAll("*"))) {
    for (const attr of Array.from(el.attributes)) {
      if (SENTINEL.test(attr.value)) hits.push(`<${el.tagName.toLowerCase()} ${attr.name}="${attr.value}">`);
    }
  }
  return hits;
}

async function settle() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
}

/** Positive control, click the real header toggle, then assert nothing leaks. */
async function expectToggleMasksPage() {
  await waitFor(() => expect(leaks().length).toBeGreaterThan(0), { timeout: 5000 });
  await settle();
  fireEvent.click(screen.getByRole("button", { name: "Hide balances" }));
  expect(leaks()).toEqual([]);
}

const USER = {
  id: 1,
  username: "u",
  email: "u@x.io",
  first_name: null,
  last_name: null,
  phone: null,
  avatar_url: null,
  email_verified: true,
  role: "owner",
  org_id: 1,
  org_name: "Acme",
  billing_cycle_day: 1,
  is_superadmin: false,
  is_active: true,
  mfa_enabled: false,
  subscription_status: null,
  subscription_plan: null,
  trial_end: null,
  allow_manual_balance_adjustment: false,
};

const PERIOD = { id: 1, start_date: "2026-09-01", end_date: null };

const ACCT = {
  id: 10,
  name: "Checking",
  account_type_id: 1,
  account_type_name: "Checking",
  account_type_slug: "checking",
  balance: AMOUNT,
  currency: "EUR",
  is_active: true,
  close_day: null,
  is_default: true,
};

const CAT = {
  id: 1,
  name: "Groceries",
  type: "expense",
  parent_id: null,
  parent_name: null,
  description: null,
  slug: "groceries",
  is_system: false,
  transaction_count: 1,
};

const TX = {
  id: 1,
  account_id: 10,
  amount: String(AMOUNT),
  type: "expense",
  status: "settled",
  date: "2026-09-10",
  description: "Rent",
  category_id: 1,
  category_name: "Groceries",
  account_name: "Checking",
  currency: "EUR",
  linked_transaction_id: null,
  is_imported: false,
  settled_date: "2026-09-10",
};

const BUDGET = {
  id: 1,
  category_id: 1,
  category_name: "Groceries",
  amount: AMOUNT,
  spent: 100,
  remaining: AMOUNT - 100,
  percent_used: 1,
  period_start: "2026-09-01",
  period_end: "2026-09-30",
};

const PLAN = {
  id: 1,
  billing_period_id: 1,
  period_start: "2026-09-01",
  period_end: null,
  status: "draft" as const,
  total_planned_income: 0,
  total_planned_expense: AMOUNT,
  total_actual_income: 0,
  total_actual_expense: 0,
  items: [
    {
      id: 1,
      plan_id: 1,
      category_id: 1,
      category_name: "Groceries",
      parent_id: null,
      type: "expense",
      planned_amount: AMOUNT,
      source: "manual",
      actual_amount: 0,
      variance: AMOUNT,
    },
  ],
};

function routeApi() {
  vi.mocked(apiFetch).mockImplementation((async (url: string) => {
    if (url.startsWith("/api/v1/reports/sources")) return ALL_ENTRIES;
    if (url.startsWith("/api/v1/account-types")) return [];
    if (url.startsWith("/api/v1/accounts")) return [ACCT];
    if (url.startsWith("/api/v1/categories")) return [CAT];
    if (url.startsWith("/api/v1/settings/billing-periods/ensure-future")) return null;
    if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD];
    if (url.startsWith("/api/v1/settings/billing-period")) return PERIOD;
    if (url.startsWith("/api/v1/settings/billing-cycle")) return { billing_cycle_day: 1 };
    if (url.startsWith("/api/v1/budgets")) return [BUDGET];
    if (url.startsWith("/api/v1/forecast-plans")) return PLAN;
    if (url.startsWith("/api/v1/forecast")) return null;
    if (url.startsWith("/api/v1/recurring")) return [];
    if (url.startsWith("/api/v1/transactions")) return { items: [TX], total: 1, limit: 200, offset: 0 };
    if (url.startsWith("/api/v1/notifications")) return { items: [], unread_count: 0 };
    return [];
  }) as never);
}

function renderPage(ui: React.ReactElement) {
  return render(<SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{ui}</SWRConfig>);
}

beforeEach(() => {
  setBalancesHidden(false);
  vi.mocked(apiFetch).mockReset();
  vi.mocked(downloadCsv).mockReset();
  window.history.pushState({}, "", "/dashboard");
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    needsSetup: false,
    billingUiEnabled: false,
    features: { ...DEFAULT_FEATURES, reports: true, plans: false },
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
  routeApi();
});

describe("F3 scanner self-test", () => {
  function Canary() {
    return <span title={`${(AMOUNT).toFixed(2)} due`}>{(AMOUNT).toFixed(2)}</span>;
  }

  it("flags a hand-built amount in text and in an attribute even when hidden", () => {
    setBalancesHidden(true);
    render(<Canary />);
    const hits = leaks();
    expect(hits).toContain("textContent");
    expect(hits.some((h) => h.startsWith("<span title="))).toBe(true);
  });
});

describe("F3: Hide balances masks every figure on the page", () => {
  it("accounts", async () => {
    renderPage(<AccountsPage />);
    await expectToggleMasksPage();
  });

  it("transactions", async () => {
    renderPage(<TransactionsPage />);
    await expectToggleMasksPage();
  });

  it("budgets", async () => {
    renderPage(<BudgetsPage />);
    await expectToggleMasksPage();
  });

  it("forecast plans", async () => {
    renderPage(
      <ForecastPlansClient
        initialPeriods={[PERIOD]}
        initialCategories={[CAT as never]}
        initialPlan={PLAN as never}
      />,
    );
    await expectToggleMasksPage();
  });

  it("dashboard", async () => {
    renderPage(<DashboardPage />);
    await expectToggleMasksPage();
  });

  it("notifications popover", async () => {
    const item = {
      id: 1,
      category: "cc_statement",
      event_type: "scheduler.cc_statement.closed",
      title: "Visa statement closed",
      body: "Your Visa statement closed. 7,373.37 EUR is due on 2026-10-01.",
      link_url: "/accounts?edit=3",
      seen_at: null,
      read_at: null,
      audit_event_id: null,
      created_at: "2026-09-15T08:00:00",
    } as unknown as Notification;
    renderPage(
      <>
        <AccountsPage />
        <NotificationPopover items={[item]} onAfterReadChange={() => {}} onClose={() => {}} />
      </>,
    );
    await expectToggleMasksPage();
  });

  describe("reports", () => {
    const LAYOUT = {
      version: 1,
      widgets: [
        {
          id: "kpi",
          type: "kpi",
          title: "Total spend",
          grid: { x: 0, y: 0, w: 3, h: 2 },
          config: { dataset: "transactions", measure: { agg: "sum", field: "amount" } },
        },
        {
          id: "table",
          type: "table",
          title: "By category",
          grid: { x: 0, y: 2, w: 12, h: 6 },
          config: {
            dataset: "transactions",
            measures: [{ measure: { agg: "sum", field: "amount" } }],
            dimensions: ["category"],
            sort: { by: "value", dir: "desc" },
            limit: 100,
          },
        },
        {
          id: "bar",
          type: "bar",
          title: "Spend by category",
          grid: { x: 0, y: 8, w: 6, h: 4 },
          config: {
            dataset: "transactions",
            measure: { agg: "sum", field: "amount" },
            dimensions: ["category"],
            sort: { by: "value", dir: "desc" },
            limit: 10,
          },
        },
      ],
    };

    beforeEach(() => {
      vi.mocked(reportsApi.getReport).mockResolvedValue({
        id: 10,
        owner_user_id: 1,
        org_id: 1,
        visibility: "private",
        name: "Spend",
        description: null,
        layout_json: LAYOUT,
        canvas_filters_json: {},
        schema_version: 1,
        created_at: "2026-09-01T10:00:00",
        updated_at: "2026-09-01T10:00:00",
      } as never);
      vi.mocked(reportsApi.runQuery).mockResolvedValue({
        rows: [{ category: "Groceries", value: AMOUNT }],
        meta: { row_count: 1, truncated: false, query_ms: 1 },
      } as never);
    });

    it("a report with a KPI, a table and a bar widget", async () => {
      renderPage(<ReportEditorPage params={{ id: "10" } as never} />);
      await expectToggleMasksPage();
    });

    it("F5: CSV export stays unmasked while hidden", async () => {
      renderPage(<ReportEditorPage params={{ id: "10" } as never} />);
      await expectToggleMasksPage();

      const buttons = screen.getAllByTestId("widget-csv-export");
      expect(buttons.length).toBe(3);
      for (const b of buttons) fireEvent.click(b);
      const csvs = vi.mocked(downloadCsv).mock.calls.map((c) => String(c[1]));
      expect(csvs).toHaveLength(3);
      for (const csv of csvs) expect(csv).toContain("7373.37");
    });
  });
});
