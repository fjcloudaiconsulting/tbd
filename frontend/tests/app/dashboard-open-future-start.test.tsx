import { render, screen, waitFor } from "@testing-library/react";

import DashboardPage from "@/app/dashboard/page";
import { apiFetch } from "@/lib/api";
import { formatLocalDate, todayISO } from "@/lib/format";
import { useAuth } from "@/components/auth/AuthProvider";

// TBD-242 D1 on the DASHBOARD — `open` shadows `upcoming`.
//
// Spec §1 rules that the two states are mutually exclusive: `periodStatus`'s
// `open` branch precedes `upcoming`, so an OPEN row whose `start_date` is
// still in the future is `open` and NOTHING ELSE. `main` decided the question
// with a second, raw-clock rule (`selectedPeriod.start_date > today`) and
// satisfied both at once on this roster.
//
// The consequence here is not a pill: `isFutureSelectedPeriod` gates the
// AI refinement toggle off and swaps the OnTrackTile for a "Plan ahead"
// panel, so reverting D1 silently strips the current period's forecast
// affordances for a reader west of Greenwich for up to ~12 hours after the
// server opens the period at ITS UTC date.

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
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

// The refine toggle fails CLOSED on an unresolved AI status, so the gate has
// to be explicitly open for its absence to mean "D1 hid it" rather than "the
// org is not entitled".
vi.mock("@/lib/hooks/use-ai-status", () => ({
  useAiStatus: vi.fn(() => ({
    forecast: { entitled: true, configured: true },
    categorize: { entitled: true, configured: true },
    budget: { entitled: true, configured: true },
  })),
}));

const stableRouter = { push: vi.fn(), replace: vi.fn() };
vi.mock("next/navigation", () => ({
  useRouter: () => stableRouter,
  usePathname: () => "/dashboard",
}));

const USER = {
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner",
  org_id: 1, org_name: "Acme", billing_cycle_day: 1, is_superadmin: false,
  is_active: true, mfa_enabled: false, subscription_status: null,
  subscription_plan: null, trial_end: null,
};

// Anchored to the local clock, never to literals: the rule under test reads
// `todayISO()`, so fixed dates would be a date bomb
// (`reference_wall_clock_date_bomb_tests`).
function isoOffset(days: number): string {
  const d = new Date(`${todayISO()}T00:00:00`);
  d.setDate(d.getDate() + days);
  return formatLocalDate(d);
}

const OPEN_FUTURE_START = { id: 50, start_date: isoOffset(1), end_date: null };
const CLOSED_PAST = {
  id: 51, start_date: isoOffset(-60), end_date: isoOffset(-30),
};
const PERIODS = [OPEN_FUTURE_START, CLOSED_PAST];

describe("DashboardPage — an open row with a future start is current, not future (TBD-242 D1)", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockReset();
    window.history.pushState({}, "", "/dashboard");
    vi.mocked(useAuth).mockReturnValue({
      user: USER as never,
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
      refreshMe: vi.fn(),
    } as never);
    vi.mocked(apiFetch).mockImplementation(((url: string) => {
      if (url === "/api/v1/accounts") return Promise.resolve([]);
      if (url === "/api/v1/categories") return Promise.resolve([]);
      if (url.startsWith("/api/v1/budgets")) return Promise.resolve([]);
      if (url === "/api/v1/settings/billing-cycle")
        return Promise.resolve({ billing_cycle_day: 1 });
      if (url === "/api/v1/settings/billing-period")
        return Promise.resolve(OPEN_FUTURE_START);
      if (url === "/api/v1/settings/billing-periods")
        return Promise.resolve(PERIODS);
      if (url.startsWith("/api/v1/transactions"))
        return Promise.resolve({ items: [], total: 0, limit: 200, offset: 0 });
      if (url.includes("forecast-plans/current")) return Promise.resolve(null);
      return Promise.resolve({});
    }) as never);
  });

  it("fence: keeps the AI refine toggle and the current-period hero on an open row whose start is tomorrow", async () => {
    // Kills: reverting `isFutureSelectedPeriod` to the raw-clock rule —
    //   const isFutureSelectedPeriod = selectedPeriod.start_date > _today;
    // in `app/dashboard/page.tsx`. Under that revert this row is BOTH
    // current and future: `AIForecastRefineToggle` is gated off by
    // `!isPast && !isFuture`, and OnTrackTile swaps to its "Plan ahead"
    // future panel.
    render(<DashboardPage />);

    await waitFor(() =>
      expect(screen.getByTestId("on-track-tile")).toBeInTheDocument(),
    );

    // The page opened on the open row.
    expect(screen.getByText("CURRENT")).toBeInTheDocument();

    // The ruled consequence: the refine toggle renders.
    expect(
      screen.getByTestId("ai-forecast-refine-toggle"),
    ).toBeInTheDocument();

    // ...and the hero is the current-period no-plan tile, not the future
    // "Plan ahead" panel. `aria-label` is the tile's stable branch handle.
    expect(screen.getByTestId("on-track-tile")).toHaveAttribute(
      "aria-label",
      "No plan for this period",
    );
    expect(screen.queryByText("Future period")).toBeNull();

    // Third observable of the same flag, in the Budget Progress empty state.
    expect(screen.queryByText(/Future budgets live in Forecasts/)).toBeNull();
  });
});
