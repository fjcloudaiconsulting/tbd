/**
 * TBD-221 PR B — the Spending donut reads the UNGATED server rollup.
 *
 * Both shells are covered: the CustomDashboard provider
 * (`DashboardDataProvider`, the live default) and LegacyDashboard
 * (`app/dashboard/page.tsx`, the rollback path).
 *
 * What is being replaced: the donut used to aggregate client-side over a
 * `limit=200` page of raw transactions, filtered only on
 * `linked_transaction_id == null`, over a client calendar window. It therefore
 * (a) disagreed with every server aggregate on manual balance adjustments and
 * reverted reconciliation rows, (b) used a different window from the budget
 * bars beside it, and (c) silently dropped the oldest rows of any period with
 * more than 200 transactions.
 *
 * ⚠⚠ The replacement is `GET /api/v1/transactions/spending-by-category` (PR
 * 628), NOT `GET /api/v1/forecast`'s per-category rollup. An earlier draft of
 * this change read the forecast's copy, which coupled a HISTORICAL ACTUALS
 * tile to the Forecast feature gate (TBD-197): an org with Forecast switched
 * off saw "No expense data yet" over a period holding real settled expense.
 * Both payloads carry the same numbers, so the wrong one type-checks and looks
 * right — which is why F-A below exists and why the fixture makes the two
 * sources return VISIBLY different data.
 *
 * The fixture mirrors, row for row, what the real backend returned when these
 * URLs were issued against a live `team-221b` stack:
 *
 *   GET /api/v1/transactions/spending-by-category?period_start=2026-08-01
 *     -> executed_expense 455.00; Home 90.00 (master), Utilities 160.00
 *        (sub of Home), Bulk 205.00 (205 rows)
 *   GET /api/v1/transactions?...&category_id=<Home>&category_match=exact&...
 *     -> total 1, sum 90.00
 *   the same URL WITHOUT category_match=exact
 *     -> total 2, sum 250.00      <- the defect F-B fences
 */
import React from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";

import { renderWithSWR } from "@/tests/utils/render-with-swr";
import {
  DashboardDataProvider,
  useDashboard,
} from "@/components/dashboard/DashboardDataProvider";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import DashboardPage from "@/app/dashboard/page";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock("@/lib/pagination", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/pagination")>("@/lib/pagination");
  return { ...actual, fetchAll: vi.fn(async () => []) };
});

vi.mock("@/components/auth/AuthProvider", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/auth/AuthProvider")
  >("@/components/auth/AuthProvider");
  return {
    ...actual,
    useAuth: vi.fn(),
    AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  };
});

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/dashboard",
}));

// ── fixtures ─────────────────────────────────────────────────────────────────

const USER = {
  id: 1, username: "u", email: "u@x.io",
  first_name: null, last_name: null, phone: null, avatar_url: null,
  email_verified: true, role: "owner", org_id: 1, org_name: "Acme",
  billing_cycle_day: 1, is_superadmin: false, is_active: true,
  mfa_enabled: false, subscription_status: null, subscription_plan: null,
  trial_end: null,
};

const PERIOD = { id: 1, start_date: "2026-05-01", end_date: null };

// The rollup's window. Deliberately NOT the calendar month end the client
// would compute from billing_cycle_day=1 (2026-05-31): the server's
// `period_spend_window_end` is the analysis bound, and the drilldown has to
// carry THIS date, not `monthTo`.
const ROLLUP_START = "2026-05-01";
const ROLLUP_END = "2026-05-24";
const CLIENT_CALENDAR_END = "2026-05-31";

const HOME_ID = 10;
const UTILITIES_ID = 11;
const BULK_ID = 12;

/**
 * ⚠ THE FIXTURE MUST MAKE `exact` AND `subtree` DISAGREE, or F-B pins nothing.
 * Home is a MASTER with 90.00 of DIRECT spend and Utilities is its SUBCATEGORY
 * with 160.00. Two slices here; one master-includes-subs drilldown returning
 * 250.00 under a slice labelled 90.00. A fixture whose master had no direct
 * spend, or whose master had no children, makes both implementations agree and
 * the fence go green against the defect.
 *
 * Bulk carries 205.00 across 205 rows — past the list endpoint's `le=200` cap,
 * which is TBD-221's original defect ("200-transaction cap breaks heavy users").
 */
const ROLLUP_CATEGORIES = [
  { category_id: HOME_ID, category_name: "Home", parent_id: null, executed: "90.00" },
  { category_id: UTILITIES_ID, category_name: "Utilities", parent_id: HOME_ID, executed: "160.00" },
  { category_id: BULK_ID, category_name: "Bulk", parent_id: null, executed: "205.00" },
];

const ROLLUP_TOTAL = 455; // 90 + 160 + 205
const HOME_SLICE = 90;

function rollup(over: Record<string, unknown> = {}) {
  return {
    period_start: ROLLUP_START,
    period_end: ROLLUP_END,
    executed_expense: String(ROLLUP_TOTAL.toFixed(2)),
    categories: ROLLUP_CATEGORIES,
    ...over,
  };
}

/**
 * `GET /api/v1/forecast`'s payload. Its `categories` carry DELIBERATELY
 * different data from the rollup above, and its `executed_expense` a
 * deliberately different scalar: any implementation that reads the donut's
 * slices or its total off THIS payload renders "FromForecast" / 999 instead of
 * the three real categories / 455. It also 404s for a forecast-off org, which
 * is the failure this whole ticket removes.
 */
function projection(over: Record<string, unknown> = {}) {
  return {
    period_start: ROLLUP_START,
    period_end: ROLLUP_END,
    executed_income: "0",
    executed_expense: "999.00",
    executed_net: "-999.00",
    pending_income: "0",
    pending_expense: "40.00",
    recurring_income: "0",
    recurring_expense: "0",
    forecast_income: "0",
    forecast_expense: "1039.00",
    forecast_net: "-1039.00",
    categories: [
      {
        category_id: 77, category_name: "FromForecast", parent_id: null,
        executed: "999.00", pending: "0", recurring: "0", forecast: "999.00",
      },
    ],
    ...over,
  };
}

function tx(over: Record<string, unknown> = {}) {
  return {
    id: 1, account_id: 10, amount: "3.00", type: "expense", status: "settled",
    date: "2026-05-10", description: "Row", category_id: 99,
    category_name: "Bogus", account_name: "Checking", currency: "EUR",
    linked_transaction_id: null, linked_account_name: null,
    is_imported: false, is_manual_adjustment: false, settled_date: "2026-05-10",
    tags: [],
    ...over,
  };
}

/**
 * A period with MORE than 200 settled expense rows.
 *
 * The server caps `limit` at 200, so the deleted snapshot fetch could only ever
 * see the first 200 — 200 x 3.00 = 600.00, against a real period total of
 * 455.00 whose largest category alone is 205 rows. Any implementation that
 * still aggregates client-side lands on 600 here.
 */
const CAPPED_SNAPSHOT = Array.from({ length: 200 }, (_, i) =>
  tx({ id: 1000 + i, description: `Capped ${i}` }),
);
const CAPPED_SNAPSHOT_SUM = 600;

// The Home drilldown's answer WITH `category_match=exact`: the master's direct
// row, summing to exactly the slice the user clicked.
const HOME_EXACT_ROWS = [
  tx({ id: 1, amount: "90.00", category_id: HOME_ID, category_name: "Home", description: "Home direct" }),
];

// What the SAME query returns WITHOUT it: master-includes-subs, so the
// Utilities row rides along and the list sums to 250 against a 90 slice. This
// is the landmine PR A's `category_match` param exists for, reproduced live
// against the real backend.
const HOME_SUBTREE_ROWS = [
  ...HOME_EXACT_ROWS,
  tx({ id: 2, amount: "160.00", category_id: UTILITIES_ID, category_name: "Utilities", description: "Utilities sub" }),
];

function sumAmounts(rows: { amount: string }[]) {
  return rows.reduce((s, r) => s + Number(r.amount), 0);
}

type Handler = (url: string, init?: RequestInit) => Promise<unknown>;

/**
 * One mock for both shells. Records every URL so the fences can assert on the
 * shape of the requests, not only on the rendered numbers.
 */
function makeHandler(opts: {
  rollupResponse?: unknown;
  rollupRejects?: boolean;
  projectionRejects?: boolean;
  snapshotRows?: ReturnType<typeof tx>[];
  urls: string[];
}): Handler {
  const {
    rollupResponse = rollup(),
    rollupRejects = false,
    projectionRejects = false,
    snapshotRows = CAPPED_SNAPSHOT,
  } = opts;
  return async (url: string) => {
    opts.urls.push(url);
    if (url.startsWith("/api/v1/accounts")) return [];
    if (url.startsWith("/api/v1/categories")) return [];
    if (url.startsWith("/api/v1/settings/billing-periods")) return [PERIOD];
    if (url.startsWith("/api/v1/settings/billing-period")) return PERIOD;
    if (url.startsWith("/api/v1/settings/billing-cycle"))
      return { billing_cycle_day: 1 };
    if (url.startsWith("/api/v1/forecast-plans/current")) return null;
    if (url.startsWith("/api/v1/forecast/account-balances"))
      return { period_start: ROLLUP_START, period_end: ROLLUP_END, totals: [], accounts: [] };
    if (url.startsWith("/api/v1/forecast?period_start=")) {
      if (projectionRejects) throw new Error("forecast boom");
      return projection();
    }
    if (url.startsWith("/api/v1/budgets")) return [];
    // ⚠ MUST precede the generic /api/v1/transactions branch below — the
    // rollup lives on the transactions router, so its URL shares the prefix.
    if (url.startsWith("/api/v1/transactions/spending-by-category")) {
      if (rollupRejects) throw new Error("rollup boom");
      return rollupResponse;
    }
    if (url.startsWith("/api/v1/transactions?status=pending"))
      return { items: [], total: 0 };
    if (url.startsWith("/api/v1/transactions")) {
      // The drilldown: `category_match=exact` decides which set comes back,
      // exactly as the real endpoint does.
      if (url.includes(`category_id=${HOME_ID}`)) {
        const rows = url.includes("category_match=exact")
          ? HOME_EXACT_ROWS
          : HOME_SUBTREE_ROWS;
        return { items: rows, total: rows.length };
      }
      if (url.includes(`category_id=${BULK_ID}`)) {
        // 205 rows exist server-side; the page returns 10 of them.
        return { items: CAPPED_SNAPSHOT.slice(0, 10), total: 205 };
      }
      // The deleted snapshot fetch, and the unfiltered recent-tx page.
      if (url.includes("limit=200"))
        return { items: snapshotRows, total: snapshotRows.length };
      return { items: snapshotRows.slice(0, 10), total: snapshotRows.length };
    }
    return null;
  };
}

function setAuth(features: Record<string, boolean> | undefined) {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never,
    loading: false,
    needsSetup: false,
    features,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

// ── provider probe ───────────────────────────────────────────────────────────

function Probe() {
  const ctx = useDashboard();
  const pctSum = ctx.sortedSpending.reduce((s, r) => s + r.pct, 0);
  return (
    <div>
      <span data-testid="loading">{String(ctx.loading)}</span>
      <span data-testid="projection-failed">{String(ctx.projectionFailed)}</span>
      <span data-testid="rollup-failed">{String(ctx.rollupFailed)}</span>
      <span data-testid="has-projection">
        {String(ctx.forecastProjection !== null)}
      </span>
      <span data-testid="donut-count">{ctx.donutData.length}</span>
      <span data-testid="donut-names">
        {ctx.donutData.map((d) => d.name).join("|")}
      </span>
      <span data-testid="total-spend">{ctx.totalSpend}</span>
      <span data-testid="slice-sum">
        {ctx.donutData.reduce((s, d) => s + d.value, 0)}
      </span>
      <span data-testid="pct-sum">{pctSum.toFixed(4)}</span>
      <span data-testid="chart-filter">{ctx.chartFilter ?? "null"}</span>
      <span data-testid="list-sum">
        {ctx.sortedVisibleTxs.reduce((s, t) => s + Number(t.amount), 0)}
      </span>
      <span data-testid="list-count">{ctx.sortedVisibleTxs.length}</span>
      <button
        data-testid="click-home-slice"
        onClick={() => {
          const slice = ctx.donutData.find((d) => d.name === "Home");
          if (slice) ctx.setChartFilter(slice.categoryId);
        }}
      />
    </div>
  );
}

function mountProvider() {
  return renderWithSWR(
    <DashboardDataProvider>
      <Probe />
    </DashboardDataProvider>,
  );
}

const txUrls = (urls: string[]) =>
  urls.filter((u) => u.startsWith("/api/v1/transactions"));
const forecastUrls = (urls: string[]) =>
  urls.filter(
    (u) =>
      u.startsWith("/api/v1/forecast") &&
      !u.startsWith("/api/v1/forecast/account-balances"),
  );

describe("TBD-221 — CustomDashboard shell (DashboardDataProvider)", () => {
  let urls: string[];

  beforeEach(() => {
    urls = [];
    vi.mocked(apiFetch).mockReset();
    window.localStorage.clear();
    setAuth(undefined);
  });

  // ── F-A: the reason the endpoint exists ────────────────────────────────────

  it("F-A: renders real spend with Forecast switched OFF, and asks a non-forecast URL for it", async () => {
    // The org that used to see "No expense data yet" over 455.00 of real
    // settled expense. `/api/v1/forecast` is never requested at all here, so a
    // donut fed from that payload has nothing to render.
    setAuth({ forecast: false, budgets: true });
    vi.mocked(apiFetch).mockImplementation(makeHandler({ urls }) as never);

    mountProvider();

    await waitFor(() =>
      expect(screen.getByTestId("donut-count").textContent).toBe("3"),
    );
    expect(screen.getByTestId("donut-names").textContent).toBe(
      "Bulk|Utilities|Home",
    );
    expect(screen.getByTestId("total-spend").textContent).toBe(
      String(ROLLUP_TOTAL),
    );
    // Not an error state: the rollup succeeded.
    expect(screen.getByTestId("rollup-failed").textContent).toBe("false");
    // Non-vacuity: prove the forecast really was skipped, so the numbers above
    // cannot have come from it. (TBD-197 F12 holds this rule too.)
    expect(forecastUrls(urls)).toEqual([]);
    expect(screen.getByTestId("has-projection").textContent).toBe("false");
    // And prove the rollup URL is the one that was asked.
    expect(
      urls.some((u) =>
        u.startsWith("/api/v1/transactions/spending-by-category?period_start="),
      ),
    ).toBe(true);
  });

  it("F-A2 (control): the same numbers appear with Forecast ON", async () => {
    // Without this control, F-A could pass on a donut that only ever works
    // when the forecast is absent.
    setAuth({ forecast: true, budgets: true });
    vi.mocked(apiFetch).mockImplementation(makeHandler({ urls }) as never);

    mountProvider();

    await waitFor(() =>
      expect(screen.getByTestId("donut-count").textContent).toBe("3"),
    );
    expect(screen.getByTestId("total-spend").textContent).toBe(
      String(ROLLUP_TOTAL),
    );
    // The forecast WAS fetched this time and carries a rival rollup — and the
    // donut still shows the real one.
    expect(forecastUrls(urls).length).toBeGreaterThan(0);
    expect(screen.getByTestId("has-projection").textContent).toBe("true");
    expect(screen.getByTestId("donut-names").textContent).not.toContain(
      "FromForecast",
    );
    expect(screen.getByTestId("total-spend").textContent).not.toBe("999");
  });

  // ── F-B: the master-includes-subs landmine ────────────────────────────────

  it("F-B: the drilldown passes category_match=exact, so a 90.00 slice opens 90.00 of rows", async () => {
    vi.mocked(apiFetch).mockImplementation(makeHandler({ urls }) as never);

    mountProvider();

    await waitFor(() =>
      expect(screen.getByTestId("donut-count").textContent).toBe("3"),
    );

    act(() => {
      screen.getByTestId("click-home-slice").click();
    });

    await waitFor(() =>
      expect(screen.getByTestId("chart-filter").textContent).toBe(
        String(HOME_ID),
      ),
    );

    const drill = await waitFor(() => {
      const found = txUrls(urls).find((u) =>
        u.includes(`category_id=${HOME_ID}`),
      );
      expect(found).toBeTruthy();
      return found!;
    });

    // The rollup's WHERE clause, reproduced exactly.
    expect(drill).toContain("category_match=exact");
    expect(drill).toContain("reportable=true");
    expect(drill).toContain("type=expense");
    expect(drill).toContain("status=settled");
    // ⚠ The window is the ROLLUP's, not the client `monthTo`.
    expect(drill).toContain(`date_from=${ROLLUP_START}`);
    expect(drill).toContain(`date_to=${ROLLUP_END}`);
    expect(drill).not.toContain(`date_to=${CLIENT_CALENDAR_END}`);
    // ⚠ `reportable` already excludes every non-null linked_transaction_id, a
    // strict superset of collapse_transfers. Sending both contradicts itself.
    expect(drill).not.toContain("collapse_transfers");
    // Paginated, not capped.
    expect(drill).toContain("limit=10&offset=0");

    // …and the list the click opened sums to the slice it opened.
    await waitFor(() =>
      expect(screen.getByTestId("list-count").textContent).toBe("1"),
    );
    expect(screen.getByTestId("list-sum").textContent).toBe(String(HOME_SLICE));

    // The fixture genuinely separates the two implementations: without `exact`
    // the same query returns the subcategory row too and sums to 250 against a
    // 90.00 slice. Verified live against the real endpoint.
    expect(sumAmounts(HOME_SUBTREE_ROWS)).toBe(250);
    expect(sumAmounts(HOME_EXACT_ROWS)).toBe(HOME_SLICE);
    expect(sumAmounts(HOME_SUBTREE_ROWS)).not.toBe(sumAmounts(HOME_EXACT_ROWS));
  });

  // ── F-C: the two failure flags are independent ────────────────────────────

  it("F-C1: a FORECAST failure does not blank the donut", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeHandler({ urls, projectionRejects: true }) as never,
    );

    mountProvider();

    await waitFor(() =>
      expect(screen.getByTestId("projection-failed").textContent).toBe("true"),
    );
    // The donut is untouched by it.
    expect(screen.getByTestId("rollup-failed").textContent).toBe("false");
    await waitFor(() =>
      expect(screen.getByTestId("donut-count").textContent).toBe("3"),
    );
    expect(screen.getByTestId("total-spend").textContent).toBe(
      String(ROLLUP_TOTAL),
    );
  });

  it("F-C2: a ROLLUP failure does not blank the forecast", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeHandler({ urls, rollupRejects: true }) as never,
    );

    mountProvider();

    await waitFor(() =>
      expect(screen.getByTestId("rollup-failed").textContent).toBe("true"),
    );
    // OnTrackTile's inputs are intact.
    expect(screen.getByTestId("projection-failed").textContent).toBe("false");
    expect(screen.getByTestId("has-projection").textContent).toBe("true");
    // And NO client-side fallback: 200 settled expense rows are on the wire
    // and the donut still shows nothing rather than 600.
    expect(screen.getByTestId("donut-count").textContent).toBe("0");
    expect(screen.getByTestId("total-spend").textContent).toBe("0");
  });

  // ── F-D: the total is the sum of the rendered slices ──────────────────────

  it("F-D: totalSpend equals the sum of the rendered slices and the server's executed_expense", async () => {
    vi.mocked(apiFetch).mockImplementation(makeHandler({ urls }) as never);

    mountProvider();

    await waitFor(() =>
      expect(screen.getByTestId("donut-count").textContent).toBe("3"),
    );
    const total = screen.getByTestId("total-spend").textContent;
    expect(total).toBe(screen.getByTestId("slice-sum").textContent);
    expect(total).toBe(String(ROLLUP_TOTAL));
    // The server computes `executed_expense` as the sum of the same rows.
    expect(Number(total)).toBe(Number(rollup().executed_expense));
    // Percentages are taken against that figure, so they add to 100.
    expect(Number(screen.getByTestId("pct-sum").textContent)).toBeCloseTo(100, 6);
    // The rival scalar on the forecast payload is a different number, so a
    // total sourced from there is visible here.
    expect(Number(projection().executed_expense)).not.toBe(ROLLUP_TOTAL);
  });

  // ── F-E: the 200-row cap is gone ──────────────────────────────────────────

  it("F-E: above 200 transactions the donut is still correct, and no limit=200 snapshot is fetched", async () => {
    vi.mocked(apiFetch).mockImplementation(makeHandler({ urls }) as never);

    mountProvider();

    await waitFor(() =>
      expect(screen.getByTestId("total-spend").textContent).toBe(
        String(ROLLUP_TOTAL),
      ),
    );
    // One category alone holds 205 rows — past the list endpoint's le=200 cap.
    expect(screen.getByTestId("donut-names").textContent).toContain("Bulk");
    // The capped snapshot sums to 600. Asserting the two differ is what makes
    // the line above evidence rather than a coincidence.
    expect(sumAmounts(CAPPED_SNAPSHOT)).toBe(CAPPED_SNAPSHOT_SUM);
    expect(CAPPED_SNAPSHOT_SUM).not.toBe(ROLLUP_TOTAL);

    // ⚠ This INVERTS the old dashboard-data-provider assertion that the
    // snapshot fires exactly once. Deleting the fetch is how the cap dies.
    await waitFor(() =>
      expect(
        txUrls(urls).some((u) => u.startsWith("/api/v1/transactions?limit=10")),
      ).toBe(true),
    );
    // The all-time pending list goes through `fetchAll`, whose own paginator
    // legitimately pages at limit=200 — it is mocked at the module boundary
    // here and never reaches apiFetch. What must be gone is the PERIOD
    // SNAPSHOT, i.e. a transactions URL whose FIRST param is limit=200.
    expect(
      txUrls(urls).filter((u) => u.startsWith("/api/v1/transactions?limit=200")),
    ).toEqual([]);
  });

  // ── F-F: period_start is a hint, not a filter ─────────────────────────────

  it("F-F: the drilldown window comes off the RESPONSE, not off the period_start we sent", async () => {
    // The server silently substitutes an unmatched period_start with the
    // current period — no 404, no 422 (verified live: period_start=1999-01-01
    // answered 200 with period_start 2026-08-01). Here the request carries
    // 2026-05-01 and the response answers with a different window; everything
    // downstream must follow the RESPONSE.
    const SUBSTITUTED_START = "2026-04-15";
    const SUBSTITUTED_END = "2026-04-30";
    vi.mocked(apiFetch).mockImplementation(
      makeHandler({
        urls,
        rollupResponse: rollup({
          period_start: SUBSTITUTED_START,
          period_end: SUBSTITUTED_END,
        }),
      }) as never,
    );

    mountProvider();

    await waitFor(() =>
      expect(screen.getByTestId("donut-count").textContent).toBe("3"),
    );
    // Prove the request really did carry the other date, or this fence is
    // asserting against a value nothing ever disagreed with.
    expect(
      urls.some((u) =>
        u.startsWith(
          `/api/v1/transactions/spending-by-category?period_start=${PERIOD.start_date}`,
        ),
      ),
    ).toBe(true);
    expect(SUBSTITUTED_START).not.toBe(PERIOD.start_date);

    act(() => {
      screen.getByTestId("click-home-slice").click();
    });

    const drill = await waitFor(() => {
      const found = txUrls(urls).find((u) =>
        u.includes(`category_id=${HOME_ID}`),
      );
      expect(found).toBeTruthy();
      return found!;
    });
    expect(drill).toContain(`date_from=${SUBSTITUTED_START}`);
    expect(drill).toContain(`date_to=${SUBSTITUTED_END}`);
    expect(drill).not.toContain(`date_from=${PERIOD.start_date}`);
  });
});

// ── LegacyDashboard shell ────────────────────────────────────────────────────

// Recharts measures its parent and jsdom reports 0x0, so the pie renders
// nothing. The legend list is plain DOM and renders regardless, which is what
// these fences read.
describe("TBD-221 — LegacyDashboard shell (app/dashboard/page.tsx)", () => {
  let urls: string[];

  beforeEach(() => {
    urls = [];
    vi.mocked(apiFetch).mockReset();
    window.history.pushState({}, "", "/dashboard");
    window.localStorage.clear();
    setAuth(undefined);
  });

  it("F-A: the legend renders real spend with Forecast switched OFF", async () => {
    setAuth({ forecast: false, budgets: true });
    vi.mocked(apiFetch).mockImplementation(makeHandler({ urls }) as never);

    render(<DashboardPage />);

    const tile = await screen.findByTestId("spending-donut");
    await waitFor(() =>
      expect(within(tile).getByText("Home")).toBeInTheDocument(),
    );
    expect(within(tile).getByText("Utilities")).toBeInTheDocument();
    expect(within(tile).getByText("Bulk")).toBeInTheDocument();
    // 205/455 = 45%, 160/455 = 35%, 90/455 = 20%.
    expect(within(tile).getByText("45%")).toBeInTheDocument();
    expect(within(tile).getByText("35%")).toBeInTheDocument();
    expect(within(tile).getByText("20%")).toBeInTheDocument();
    // Not the "unavailable" state, and not the empty state.
    expect(within(tile).queryByText(/No expense data yet/i)).toBeNull();
    expect(
      within(tile).queryByText(/Spending by category unavailable/i),
    ).toBeNull();
    // Non-vacuity: the forecast was never fetched, so it cannot be the source.
    expect(forecastUrls(urls)).toEqual([]);
  });

  it("F-B: clicking a legend row issues the exact drilldown and the list agrees with the slice", async () => {
    vi.mocked(apiFetch).mockImplementation(makeHandler({ urls }) as never);

    render(<DashboardPage />);

    const tile = await screen.findByTestId("spending-donut");
    const row = await within(tile).findByText("Home");
    act(() => {
      row.closest("button")!.click();
    });

    await waitFor(() =>
      expect(screen.getByText(/Filtering: Home/)).toBeInTheDocument(),
    );

    const drill = await waitFor(() => {
      const found = txUrls(urls).find((u) =>
        u.includes(`category_id=${HOME_ID}`),
      );
      expect(found).toBeTruthy();
      return found!;
    });
    expect(drill).toContain("category_match=exact");
    expect(drill).toContain("reportable=true");
    expect(drill).toContain("type=expense");
    expect(drill).toContain("status=settled");
    expect(drill).toContain(`date_to=${ROLLUP_END}`);
    expect(drill).not.toContain("collapse_transfers");

    // The one row the server returned for that slice, and nothing else.
    await waitFor(() =>
      expect(screen.getAllByTestId(/^dash-settled-\d+$/)).toHaveLength(1),
    );
    expect(screen.getByText("Home direct")).toBeInTheDocument();
    expect(screen.queryByText("Utilities sub")).toBeNull();
  });

  it("F-C1: a forecast failure leaves the donut rendering", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeHandler({ urls, projectionRejects: true }) as never,
    );

    render(<DashboardPage />);

    const tile = await screen.findByTestId("spending-donut");
    await waitFor(() =>
      expect(within(tile).getByText("Home")).toBeInTheDocument(),
    );
    expect(
      within(tile).queryByText(/Spending by category unavailable/i),
    ).toBeNull();
    expect(within(tile).getByText("45%")).toBeInTheDocument();
  });

  it("F-C2: a rollup failure renders the donut error state with no number", async () => {
    vi.mocked(apiFetch).mockImplementation(
      makeHandler({ urls, rollupRejects: true }) as never,
    );

    render(<DashboardPage />);

    const tile = await screen.findByTestId("spending-donut");
    await waitFor(() =>
      expect(
        within(tile).getByText(/Spending by category unavailable/i),
      ).toBeInTheDocument(),
    );
    expect(
      within(tile).getByRole("button", { name: /retry/i }),
    ).toBeInTheDocument();
    // No fallback aggregation of the rows the list endpoint is still serving.
    expect(within(tile).queryByText("Bogus")).toBeNull();
    expect(within(tile).queryByText(/100%/)).toBeNull();
  });

  it("F-E: the legacy shell never issues the limit=200 period snapshot either", async () => {
    vi.mocked(apiFetch).mockImplementation(makeHandler({ urls }) as never);

    render(<DashboardPage />);
    await screen.findByTestId("spending-donut");

    await waitFor(() =>
      expect(
        txUrls(urls).some((u) => u.startsWith("/api/v1/transactions?limit=10")),
      ).toBe(true),
    );
    expect(
      txUrls(urls).filter((u) => u.startsWith("/api/v1/transactions?limit=200")),
    ).toEqual([]);
  });
});
