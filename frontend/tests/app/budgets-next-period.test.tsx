import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { it, describe, expect, vi, beforeEach } from "vitest";
import BudgetsPage from "@/app/budgets/page";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";
import { formatLocalDate, todayISO } from "@/lib/format";
import { useAiStatus } from "@/lib/hooks/use-ai-status";

vi.mock("@/components/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));
vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: vi.fn() }));
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, apiFetch: vi.fn() };
});
vi.mock("@/lib/hooks/use-ai-status", () => ({ useAiStatus: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/budgets",
  useSearchParams: () => ({ get: () => null }),
}));

const USER = {
  id: 1, username: "u", email: "u@x.io", first_name: null, last_name: null,
  phone: null, avatar_url: null, email_verified: true, role: "owner" as const,
  org_id: 1, org_name: "Acme", billing_cycle_day: 1, is_superadmin: false,
  is_active: true, mfa_enabled: false, subscription_status: null,
  subscription_plan: null, trial_end: null, allow_manual_balance_adjustment: false,
};

// PERIOD_OPEN is the current period; NEXT_STUB is a future stub (start after
// today) representing "next period".
const PERIOD_OPEN = { id: 1, start_date: "2026-05-01", end_date: null };
const NEXT_STUB = { id: 2, start_date: "2999-01-01", end_date: "2999-01-31" };

const CURRENT_BUDGET = {
  id: 1, category_id: 10, category_name: "Groceries",
  amount: "500", spent: "200", percent_used: 40,
};

function setupAuth() {
  vi.mocked(useAuth).mockReturnValue({
    user: USER as never, loading: false, needsSetup: false,
    login: vi.fn(), register: vi.fn(), logout: vi.fn(), refreshMe: vi.fn(),
  } as never);
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  vi.mocked(useAiStatus).mockReset();
  vi.mocked(useAiStatus).mockReturnValue({ budget: { entitled: false, configured: false } } as never);
  setupAuth();
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.includes("ensure-future")) return [] as never;
    if (url.startsWith("/api/v1/categories")) return [] as never;
    if (url.startsWith("/api/v1/settings/billing-periods")) {
      return [PERIOD_OPEN, NEXT_STUB] as never;
    }
    if (url.includes("period_start=2999-01-01")) return [] as never; // next: empty
    if (url.startsWith("/api/v1/budgets")) return [CURRENT_BUDGET] as never;
    return null as never;
  });
});

it("offers the four seed actions in an empty next period", async () => {
  render(<BudgetsPage />);
  // Loads on the current period first.
  await waitFor(() => expect(screen.getByText("Groceries")).toBeInTheDocument());

  // Navigate to the next period (the › chevron moves toward newer periods).
  fireEvent.click(screen.getByLabelText("Next period"));

  // The empty next-period state shows all four seed actions.
  await waitFor(() =>
    expect(screen.getByTestId("next-period-seed")).toBeInTheDocument(),
  );
  expect(screen.getByRole("button", { name: /from forecast/i })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /copy this period/i })).toBeInTheDocument();
  expect(screen.getByTestId("ai-draft-btn")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /start blank/i })).toBeInTheDocument();
  // A future period is editable, so it is NOT flagged read-only.
  expect(screen.queryByText(/closed \(read-only\)/i)).toBeNull();
});

// Anchored to the local clock rather than to literals: the partition under
// test reads `todayISO()`, so fixed dates would be a date bomb.
function isoOffset(days: number): string {
  const d = new Date(`${todayISO()}T00:00:00`);
  d.setDate(d.getDate() + days);
  return formatLocalDate(d);
}

describe("budgets display window on a lapsed roster (TBD-242)", () => {
  // The D1 roster: an OPEN row whose start_date is still in the future (the
  // server opens a period at ITS UTC date, so a reader west of Greenwich sees
  // this for up to ~12 hours), plus a genuine closed stub for next period.
  const OPEN_FUTURE_START = {
    id: 30, start_date: isoOffset(1), end_date: null,
  };
  const CLOSED_STUB = {
    id: 31, start_date: isoOffset(2), end_date: isoOffset(40),
  };
  const CLOSED_PAST = {
    id: 32, start_date: isoOffset(-60), end_date: isoOffset(-30),
  };
  const ROSTER = [OPEN_FUTURE_START, CLOSED_STUB, CLOSED_PAST];

  beforeEach(() => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes("ensure-future")) return [] as never;
      if (url.startsWith("/api/v1/categories")) return [] as never;
      if (url.startsWith("/api/v1/settings/billing-periods")) {
        return ROSTER as never;
      }
      if (url.startsWith("/api/v1/budgets")) return [] as never;
      return null as never;
    });
  });

  it("fence: keeps the closed next-period stub, and keeps the open row selectable", async () => {
    // Kills: the raw-clock partition this replaced —
    //   past   = filter(bp.start_date <= today)
    //   future = filter(bp.start_date >  today).sort(asc)
    //   pl     = future.length ? [future[0], ...past] : past
    //
    // On THIS roster both the open row and the closed stub have
    // `start_date > today`, so both land in `future`, `future[0]` takes the
    // OPEN row, and the stub is DROPPED FROM THE LIST ENTIRELY — the user
    // cannot budget next period at all. The adjacent `[next, current, prev]`
    // invariant is false there too.
    //
    // The replacement partitions on the classifier's `upcoming` branch and
    // takes the complement as the rest, so it is total by construction: no
    // row can vanish.
    //
    // Injecting the old split goes red twice over: the stub is absent, and
    // `pl[0]` is the wrong row.
    render(<BudgetsPage />);

    // The open row is still present and is what `selectCurrentPeriodIndex`
    // picks, so it is the row the page opens on.
    await waitFor(() =>
      expect(screen.getByText(OPEN_FUTURE_START.start_date)).toBeInTheDocument(),
    );

    // ...and slot 0 — the "next period" slot the nav's newest-first ordering
    // reserves — is the CLOSED stub, reachable with one step toward newer.
    fireEvent.click(screen.getByLabelText("Next period"));
    await waitFor(() =>
      expect(
        screen.getByText(`${CLOSED_STUB.start_date} – ${CLOSED_STUB.end_date}`),
      ).toBeInTheDocument(),
    );
  });
});
