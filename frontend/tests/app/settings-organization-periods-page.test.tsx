/**
 * Spec tests 27-31 for `/settings/organization/periods`
 * (`specs/2026-07-29-billing-period-roster-design.md` §4c).
 *
 * Every `it` below is a FENCE, and each one was proven red against the
 * specific defect it exists to forbid before it was allowed to go green:
 *
 * * 27 — `activeTab` passed as the real pathname (`/settings/organization/periods`),
 *        which un-highlights every tab in `SettingsLayout`.
 * * 28 — a `gap` rendered as a chip on one of the two rows it names, instead
 *        of as an interstitial break in the rail.
 * * 29 — the two ends of §2.1 collapsed into one fact.
 * * 30 — the summary band written as `anomalies.filter((a) => a.off_window)`,
 *        which erases `no_open` and both refusal markers.
 * * 31 — the admin redirect removed.
 *
 * `renderWithSWR` is mandatory here: SWR's default cache is module-scoped and
 * this file mounts the same key with different payloads across `it` blocks, so
 * a shared cache would warm one test's roster into the next one.
 */
import { screen, waitFor, within } from "@testing-library/react";

import BillingPeriodRosterPage from "@/app/settings/organization/periods/page";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { renderWithSWR } from "@/tests/utils/render-with-swr";
import type {
  ReferencedPeriod,
  RosterAnomaly,
  RosterPeriod,
  RosterResponse,
} from "@/app/settings/organization/periods/rosterMarkers";

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

const pushMock = vi.fn();
const replaceMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: replaceMock }),
  // ⚠ Deliberately the REAL pathname. The page must NOT use it for
  // `activeTab`; test 27 is red if it does.
  usePathname: () => "/settings/organization/periods",
  useSearchParams: () => new URLSearchParams(),
}));

function mockUser(role: "owner" | "admin" | "member") {
  vi.mocked(useAuth).mockReturnValue({
    user: {
      id: 1,
      username: "u",
      email: "u@x.io",
      first_name: null,
      last_name: null,
      phone: null,
      avatar_url: null,
      email_verified: true,
      role,
      org_id: 1,
      org_name: "Acme Household",
      billing_cycle_day: 1,
      is_superadmin: false,
      is_active: true,
      mfa_enabled: false,
      subscription_status: null,
      subscription_plan: null,
      trial_end: null,
    } as never,
    loading: false,
    needsSetup: false,
    billingUiEnabled: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    refreshMe: vi.fn(),
  } as never);
}

function period(overrides: Partial<RosterPeriod> & Pick<RosterPeriod, "id" | "start_date">): RosterPeriod {
  return {
    end_date: null,
    effective_end: null,
    counting_through: null,
    status: "past",
    length_days: null,
    transaction_count: 0,
    settled_net: "0.00",
    ...overrides,
  };
}

function referenced(p: RosterPeriod | ReferencedPeriod): ReferencedPeriod {
  return {
    id: p.id,
    start_date: p.start_date,
    end_date: p.end_date,
    effective_end: p.effective_end,
    status: p.status,
  };
}

function response(overrides: Partial<RosterResponse> = {}): RosterResponse {
  const periods = overrides.periods ?? [];
  const base: RosterResponse = {
    roster: {
      period_count: periods.length,
      first_start: periods.length ? periods[0].start_date : null,
      last_start: periods.length ? periods[periods.length - 1].start_date : null,
      analyzed: true,
    },
    window: {
      from: periods.length ? periods[0].start_date : null,
      to: null,
      displayed_count: periods.length,
      truncated: false,
    },
    periods,
    anomalies: [],
    referenced_periods: Object.fromEntries(
      periods.map((p) => [String(p.id), referenced(p)]),
    ),
  };
  return {
    ...base,
    ...overrides,
    roster: { ...base.roster, ...(overrides.roster ?? {}) },
    window: { ...base.window, ...(overrides.window ?? {}) },
    referenced_periods: {
      ...base.referenced_periods,
      ...(overrides.referenced_periods ?? {}),
    },
  };
}

function serve(payload: RosterResponse) {
  vi.mocked(apiFetch).mockImplementation(((url: string) => {
    if (typeof url === "string" && url.startsWith("/api/v1/settings/billing-periods/roster")) {
      return Promise.resolve(payload);
    }
    return Promise.resolve({});
  }) as never);
}

/** The `<li>` a piece of text lives in. */
function closestLi(el: HTMLElement): HTMLElement {
  const li = el.closest("li");
  expect(li).not.toBeNull();
  return li as HTMLElement;
}

/**
 * The timeline `<ol>`. Row lookups are scoped to it because the roster-facts
 * `<dl>` above legitimately repeats the first and last start dates.
 */
function timeline(): HTMLElement {
  return screen.getByRole("list", { name: "Billing periods, oldest first" });
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
  pushMock.mockReset();
  replaceMock.mockReset();
});

describe("Billing period roster page", () => {
  // ── Test 27 ────────────────────────────────────────────────────────
  it("renders under SettingsLayout with the Organization tab active", async () => {
    mockUser("admin");
    serve(response({ periods: [period({ id: 1, start_date: "2026-06-01" })] }));
    renderWithSWR(<BillingPeriodRosterPage />);

    await waitFor(() => expect(screen.getByText("Roster health")).toBeInTheDocument());

    // SettingsLayout owns the route's single <h1>.
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Settings");

    const orgTab = screen.getByRole("link", { name: "Organization" });
    expect(orgTab).toHaveAttribute("href", "/settings/organization");
    // ⚠ The fence. `SettingsLayout` compares `activeTab` against its own tab
    // hrefs, so passing `usePathname()` ("/settings/organization/periods")
    // leaves every tab inactive and this assertion fails.
    expect(orgTab.className).toContain("border-accent");
    expect(orgTab.className).toContain("text-accent");

    // Exactly one tab is highlighted.
    const activeTabs = screen
      .getAllByRole("link")
      .filter((a) => a.className.includes("border-b-2 border-accent"));
    expect(activeTabs).toHaveLength(1);
  });

  // ── Test 28 ────────────────────────────────────────────────────────
  it("renders row-scoped markers inline and a gap as an interstitial rail break", async () => {
    mockUser("admin");
    const p1 = period({
      id: 1,
      start_date: "2026-01-01",
      end_date: "2026-01-31",
      effective_end: "2026-01-31",
      counting_through: "2026-01-31",
      length_days: 31,
    });
    const p2 = period({
      id: 2,
      start_date: "2026-03-01",
      end_date: "2026-04-30",
      effective_end: "2026-04-30",
      counting_through: "2026-04-30",
      length_days: 61,
    });
    const p3 = period({
      id: 3,
      start_date: "2026-04-01",
      // Inverted: it ends before it starts. Deliberately NOT 2026-03-01, so
      // the row-index assertions below can key off p2's start date uniquely.
      end_date: "2026-03-15",
      effective_end: "2026-03-15",
      counting_through: "2026-03-15",
      status: "invalid",
    });
    const anomalies: RosterAnomaly[] = [
      {
        kind: "gap",
        from_period_id: 1,
        to_period_id: 2,
        from_date: "2026-02-01",
        to_date: "2026-02-28",
        off_window: false,
      },
      {
        kind: "overlap",
        from_period_id: 2,
        to_period_id: 3,
        from_date: "2026-04-01",
        to_date: "2026-04-30",
        off_window: false,
      },
      { kind: "inverted", period_id: 3, off_window: false },
    ];
    serve(response({ periods: [p1, p2, p3], anomalies }));
    renderWithSWR(<BillingPeriodRosterPage />);

    await waitFor(() =>
      expect(screen.queryAllByText("Coverage gap").length).toBeGreaterThan(0),
    );
    const ol = timeline();

    // ⚠ The fence. A gap is geometry, not a badge: ONE marker, in its own
    // <li>. Rendering it inline puts a chip on BOTH rows it names.
    expect(screen.getAllByText("Coverage gap")).toHaveLength(1);

    // The gap sits in its OWN <li>, between the two rows it names, and that
    // <li> carries neither row's start date and no rail spine.
    const gapLi = closestLi(screen.getByText("Coverage gap"));
    expect(gapLi.textContent).not.toContain("2026-01-01");
    expect(gapLi.textContent).not.toContain("2026-03-01");
    expect(gapLi.className).not.toContain("border-l");
    expect(gapLi.querySelector(".border-dashed")).not.toBeNull();
    expect(
      screen.getByText(/Nothing covers 2026-02-01 to 2026-02-28/),
    ).toBeInTheDocument();
    // 28 days, inclusive.
    expect(screen.getByText(/those 28 days belong to no period/)).toBeInTheDocument();

    // The rail break is positioned between the two named rows.
    const items = Array.from(ol.children);
    const gapIndex = items.indexOf(gapLi);
    const row1Index = items.indexOf(closestLi(within(ol).getByText("2026-01-01")));
    const row2Index = items.indexOf(closestLi(within(ol).getByText("2026-03-01")));
    expect(row1Index).toBeGreaterThanOrEqual(0);
    expect(gapIndex).toBeGreaterThan(row1Index);
    expect(gapIndex).toBeLessThan(row2Index);

    // Row-scoped markers ARE inline, on the rows they name.
    const invertedLi = closestLi(screen.getByText("End before start"));
    expect(invertedLi.textContent).toContain("2026-04-01");

    const overlapChips = screen.getAllByText("Overlapping periods");
    expect(overlapChips).toHaveLength(2);
    const overlapRows = overlapChips.map((chip) => closestLi(chip).textContent ?? "");
    expect(overlapRows.some((t) => t.includes("2026-03-01"))).toBe(true);
    expect(overlapRows.some((t) => t.includes("2026-04-01"))).toBe(true);

    // The overlap note shows when any row overlaps.
    expect(
      screen.getAllByText(
        /both cover 2026-04-01 to 2026-04-30\. Transactions in that range are counted twice\./,
      ).length,
    ).toBeGreaterThan(0);
  });

  // ── Test 29 ────────────────────────────────────────────────────────
  it("renders both ends on every row and distinguishes divergence only when they differ", async () => {
    mockUser("admin");
    const converged = period({
      id: 1,
      start_date: "2026-05-01",
      end_date: "2026-05-31",
      effective_end: "2026-05-31",
      counting_through: "2026-05-31",
      status: "past",
      length_days: 31,
    });
    // ⚠ The diverged row MUST be OPEN: a closed row cannot diverge (§2.1).
    const diverged = period({
      id: 2,
      start_date: "2026-06-01",
      end_date: null,
      effective_end: "2026-06-30",
      counting_through: "2026-08-01",
      status: "open",
      length_days: 30,
    });
    serve(response({ periods: [converged, diverged] }));
    renderWithSWR(<BillingPeriodRosterPage />);

    await waitFor(() => expect(timeline()).toBeInTheDocument());
    const ol = timeline();

    // Converged: both facts, ONE line, identical styling. The repetition IS
    // the "these agree" signal; a fused label fails here.
    const convergedLi = closestLi(within(ol).getByText("2026-05-01"));
    const convergedLine = Array.from(convergedLi.querySelectorAll("p")).find((p) =>
      /^Period ends 2026-05-31 · Counting through 2026-05-31$/.test(
        (p.textContent ?? "").replace(/\s+/g, " ").trim(),
      ),
    );
    expect(convergedLine).toBeDefined();
    // Identical styling for both facts: one <p>, one class list, and no chip.
    expect(convergedLi.querySelector(".bg-warning-dim")).toBeNull();

    // Diverged: the second fact moves to its OWN line, in `badgeWarning`, with
    // the divergence stated in words inside the chip.
    const divergedLi = closestLi(within(ol).getByText("2026-06-01"));
    expect(divergedLi.textContent).toContain("Period ends 2026-06-30");
    const chip = divergedLi.querySelector(".bg-warning-dim");
    expect(chip).not.toBeNull();
    expect((chip as HTMLElement).className).toContain("text-warning");
    expect((chip!.textContent ?? "").replace(/\s+/g, " ")).toContain(
      "Counting through 2026-08-01, past this period's end",
    );
    // The two facts are NOT on the same line on a diverged row.
    const fusedLine = Array.from(divergedLi.querySelectorAll("p")).find((p) =>
      /Period ends .* · Counting through/.test(p.textContent ?? ""),
    );
    expect(fusedLine).toBeUndefined();
  });

  // ── Test 30 ────────────────────────────────────────────────────────
  it("renders off-window markers in the summary band from referenced_periods", async () => {
    mockUser("admin");
    const shown = period({
      id: 9,
      start_date: "2026-06-01",
      end_date: "2026-06-30",
      effective_end: "2026-06-30",
      counting_through: "2026-06-30",
      length_days: 30,
    });
    serve(
      response({
        periods: [shown],
        roster: { period_count: 30, first_start: "2023-01-01", last_start: "2026-06-01", analyzed: true },
        anomalies: [
          {
            kind: "overlap",
            from_period_id: 12,
            to_period_id: 17,
            from_date: "2023-04-01",
            to_date: "2023-09-30",
            off_window: true,
          },
        ],
        referenced_periods: {
          "12": {
            id: 12,
            start_date: "2023-01-01",
            end_date: "2023-09-30",
            effective_end: "2023-09-30",
            status: "past",
          },
          "17": {
            id: 17,
            start_date: "2023-07-01",
            end_date: "2023-07-31",
            effective_end: "2023-07-31",
            status: "past",
          },
        },
      }),
    );
    renderWithSWR(<BillingPeriodRosterPage />);

    await waitFor(() =>
      expect(screen.getByText("Issues not shown on the timeline")).toBeInTheDocument(),
    );
    expect(screen.getByText("Overlapping periods")).toBeInTheDocument();
    expect(
      screen.getByText(
        /The period starting 2023-01-01 and the period starting 2023-07-01 both cover 2023-04-01 to 2023-09-30/,
      ),
    ).toBeInTheDocument();
    // Rendered from `referenced_periods`, ids the timeline does not carry.
    const bandEntry = closestLi(screen.getByText("Overlapping periods"));
    expect(bandEntry.textContent).toContain("Period starting 2023-01-01");
    expect(bandEntry.textContent).toContain("Period starting 2023-07-01");
    // Neither referenced id is on the timeline.
    expect(within(timeline()).queryByText("2023-01-01")).toBeNull();
    expect(within(timeline()).queryByText("2023-07-01")).toBeNull();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  // ── Test 30, roster-scoped extension (F5) ──────────────────────────
  it("renders roster-scoped markers in the band even though off_window is false", async () => {
    mockUser("admin");
    // The exact org this page exists for: 400 periods, all older than the
    // window, none open. `periods` is empty and `off_window` is FALSE.
    serve(
      response({
        periods: [],
        roster: {
          period_count: 400,
          first_start: "2010-01-01",
          last_start: "2024-01-01",
          analyzed: false,
        },
        anomalies: [
          { kind: "no_open", period_ids: [], off_window: false },
          {
            kind: "overlap_analysis_skipped",
            period_count: 400,
            cap: 300,
            off_window: false,
          },
        ],
        referenced_periods: {},
      }),
    );
    renderWithSWR(<BillingPeriodRosterPage />);

    await waitFor(() =>
      expect(screen.getByText("Issues not shown on the timeline")).toBeInTheDocument(),
    );
    // ⚠ The fence. `anomalies.filter((a) => a.off_window)` renders neither.
    expect(screen.getByText("No open period")).toBeInTheDocument();
    expect(screen.getByText("Overlap check skipped")).toBeInTheDocument();
    expect(
      screen.getByText(
        /This roster has 400 periods, over the 300 limit for the overlap check/,
      ),
    ).toBeInTheDocument();

    // The empty-window state, NOT the empty-roster state, and the guarantee
    // sentence swapped for the skipped overlap check.
    expect(
      screen.getByText(/None of this organization's 400 periods start in the last 12 months/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/The overlap check was skipped because this roster is too large/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No billing periods yet/)).toBeNull();
  });

  // ── Test 31 ────────────────────────────────────────────────────────
  it("redirects a non-admin deep-linking the page", async () => {
    mockUser("member");
    serve(response({ periods: [] }));
    renderWithSWR(<BillingPeriodRosterPage />);

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/settings"));
    expect(screen.queryByText("Roster health")).toBeNull();
    expect(vi.mocked(apiFetch)).not.toHaveBeenCalled();
  });
});
