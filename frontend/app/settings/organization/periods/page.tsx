"use client";

/**
 * `/settings/organization/periods` — the read-only billing period roster.
 *
 * Spec: `specs/2026-07-29-billing-period-roster-design.md` §1.1, §2.1, §2.5.
 *
 * ⚠ **This page is a RAIL, not a grid.** §2.5's payload maps 1:1 onto a
 * nine-column table and that shape is explicitly forbidden: `DESIGN.md` names
 * it an anti-reference and `PRODUCT.md` asks for hierarchy-without-grids. One
 * vertical hairline, one alignment axis, deliberately non-uniform row heights
 * so roster health is legible from the silhouette. **No column headers, no
 * zebra striping, no cell borders, and no `overflow-x-auto` anywhere.** Adding
 * horizontal scrolling means the design has been rebuilt as the table §1.1
 * forbids.
 *
 * Three further rules, each with a fence in
 * `tests/app/settings-organization-periods-page.test.tsx`:
 *
 * * `activeTab` is the **literal parent route** `/settings/organization`, not
 *   this route and not `usePathname()`. `SettingsLayout` compares it against
 *   its own tab hrefs, so the real pathname un-highlights every tab.
 * * The summary band unions `ROSTER_SCOPED` with `off_window === true`. A
 *   plain `off_window` filter erases `no_open` on the exact org this page
 *   exists for.
 * * `effective_end` and `counting_through` are BOTH rendered on every row.
 *   Collapsing them is the defect §2.1 exists to prevent.
 *
 * Read-only by construction: no write path, no repair affordance (TBD-235
 * owns repair), no sort/filter/search controls (sorting off chronological
 * order destroys the adjacency that makes a rail break legible).
 */

import { useEffect, useState, type ReactNode } from "react";
import { selectCurrentPeriod } from "@/lib/billingPeriodStatus";
import Link from "next/link";
import { useRouter } from "next/navigation";
import useSWR from "swr";
import { CircleAlert, Ellipsis, Info, Loader2, TriangleAlert } from "lucide-react";

import SettingsLayout from "@/components/SettingsLayout";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isAdmin } from "@/lib/auth";
import { formatAmount } from "@/lib/format";
import {
  badgeError,
  badgeInfo,
  badgeNeutral,
  badgeWarning,
  btnLink,
  card,
  cardHeader,
  cardTitle,
  error as errorCls,
  label as labelCls,
  input as inputCls,
  success as successCls,
  warning as warningCls,
} from "@/lib/styles";
import {
  anomalyPeriodIds,
  bandAnomalies,
  describeAnomaly,
  highestTier,
  inlineAnomaliesFor,
  railBreakGaps,
  statusWord,
  type MarkerCopy,
  type ReferencedPeriod,
  type RosterAnomaly,
  type RosterPeriod,
  type RosterResponse,
  type Tier,
} from "./rosterMarkers";

const MONTH_OPTIONS = [3, 6, 12, 24, 60];

/** One glyph per TIER, four in total. Never one per kind. */
const TIER_ICON = {
  error: CircleAlert,
  warning: TriangleAlert,
  info: Info,
  neutral: Ellipsis,
} as const;

const TIER_BADGE: Record<Tier, string> = {
  error: badgeError,
  warning: badgeWarning,
  info: badgeInfo,
  neutral: badgeNeutral,
};

// A brass focus ring on every pressable surface (DESIGN.md, the
// Pressable-Surfaces Rule). `btnLink` carries colour only.
const FOCUSABLE =
  "rounded-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30";

function MarkerChip({ copy }: { copy: MarkerCopy }) {
  const Icon = TIER_ICON[copy.tier];
  return (
    <span className={TIER_BADGE[copy.tier]}>
      {/* So the chip is not heard as another metadata value. */}
      <span className="sr-only">Issue: </span>
      <Icon className="h-3 w-3" aria-hidden="true" />
      {copy.label}
    </span>
  );
}

function DateText({ iso }: { iso: string }) {
  return (
    <time dateTime={iso} className="tabular-nums">
      {iso}
    </time>
  );
}

export default function BillingPeriodRosterPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const admin = user ? isAdmin(user) : false;
  const [months, setMonths] = useState(12);

  useEffect(() => {
    if (!loading && !admin) router.replace("/settings");
  }, [loading, admin, router]);

  const key = `/api/v1/settings/billing-periods/roster?months=${months}`;
  // ⚠ `keepPreviousData` is NOT a nicety here, it is what makes two of §1.1's
  // contracts hold. The window `<select>` changes `key`, and SWR returns
  // `data: undefined` for a never-fetched key, so `{data && <RosterView/>}`
  // would UNMOUNT the whole subtree on every window change. That subtree
  // contains (a) the one `role="status"` live region this page is allowed —
  // and a live region re-inserted into the DOM already populated never
  // announces, so the mandated announcement would provably never fire — and
  // (b) the `<select>` the keyboard user is operating, which would be removed
  // mid-interaction with focus dumped to `<body>`: a change of context on
  // input. Precedent: `app/forecast-plans/ForecastPlansClient.tsx`.
  const { data, error, isLoading } = useSWR<RosterResponse>(
    admin ? key : null,
    () => apiFetch<RosterResponse>(key),
    { revalidateOnFocus: false, keepPreviousData: true },
  );

  if (loading || !user || !admin) {
    return (
      <SettingsLayout activeTab="/settings/organization">
        <div className="flex justify-center py-12">
          <Loader2
            className="h-6 w-6 animate-spin motion-reduce:animate-none text-text-muted"
            aria-hidden="true"
          />
          <span className="sr-only">Loading</span>
        </div>
      </SettingsLayout>
    );
  }

  return (
    <SettingsLayout activeTab="/settings/organization">
      {/* The Organization tab is highlighted but this is not that page, so a
          quiet way back is part of the contract. */}
      <div className="mb-4">
        <Link href="/settings/organization" className={`${btnLink} ${FOCUSABLE}`}>
          Back to Organization settings
        </Link>
      </div>

      {error && (
        <p role="alert" className={errorCls}>
          {extractErrorMessage(error, "Could not load the billing period roster")}
        </p>
      )}

      {isLoading && !data && (
        <div className="flex justify-center py-12">
          <Loader2
            className="h-6 w-6 animate-spin motion-reduce:animate-none text-text-muted"
            aria-hidden="true"
          />
          <span className="sr-only">Loading billing periods</span>
        </div>
      )}

      {data && <RosterView data={data} months={months} onMonthsChange={setMonths} />}
    </SettingsLayout>
  );
}

function RosterView({
  data,
  months,
  onMonthsChange,
}: {
  data: RosterResponse;
  months: number;
  onMonthsChange: (next: number) => void;
}) {
  const { roster, window: win, periods, anomalies, referenced_periods: refs } = data;
  const band = bandAnomalies(anomalies);
  const gaps = railBreakGaps(anomalies);
  const worst = highestTier(anomalies, refs);

  // The single brass moment: the anchored open row, the greatest `start_date`
  // among open rows, matching `get_current_period`'s ordering and the kernel's
  // straddle anchor. Under `duplicate_open` the other open rows get
  // `badgeError`, never brass.
  // Carries the ROW, not the id: an id would have to be resolved back through
  // `periods.find` on every step, making an O(n) scan O(n²).
  // TBD-242: the shared selector, not a fourth hand-rolled copy. It keys off
  // `end_date === null`, which on this page is exactly the rows the SERVER
  // marked `status === "open"` (the backend derives that status from the same
  // column), so the brass anchor is unchanged — but the tie-break is now the
  // app-wide one instead of this file's private `>=` last-wins.
  //
  // ⚠ That tie-break change is provably INERT through this endpoint, and no
  // test pins it because the branch is UNREACHABLE here: two open rows can
  // never share a `start_date` (`uq_billing_period_org_start`), and an open
  // row can never be `invalid` (the backend's branch 1 requires a non-null
  // `end_date`). Recorded rather than fenced — minting a test for an
  // unreachable branch is how a vacuous-by-construction fence gets written.
  const anchoredOpen = selectCurrentPeriod(periods);
  const anchoredOpenId = anchoredOpen?.id ?? null;

  return (
    <div className="space-y-6">
      <section className={card}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Roster health</h2>
        </div>
        <div className="space-y-4 p-6">
          <Verdict worst={worst} count={anomalies.length} />

          <dl className="flex flex-wrap gap-x-10 gap-y-4">
            <div>
              <dt className={labelCls}>Periods</dt>
              <dd className="text-sm tabular-nums text-text-primary">
                {roster.period_count}
              </dd>
            </div>
            <div>
              <dt className={labelCls}>First start</dt>
              <dd className="text-sm tabular-nums text-text-primary">
                {roster.first_start ? <DateText iso={roster.first_start} /> : "None"}
              </dd>
            </div>
            <div>
              <dt className={labelCls}>Last start</dt>
              <dd className="text-sm tabular-nums text-text-primary">
                {roster.last_start ? <DateText iso={roster.last_start} /> : "None"}
              </dd>
            </div>
            <div>
              <dt className={labelCls}>Overlap check</dt>
              <dd className="text-sm text-text-primary">
                {roster.analyzed ? "Ran" : "Skipped"}
              </dd>
            </div>
          </dl>

          {/*
            ⚠ The guarantee sentence SWAPS when the overlap check was skipped.
            The unconditional version is a lie in that state, and it is the
            sentence this page's credibility rests on.
          */}
          <p className="text-sm text-text-secondary">
            {roster.analyzed
              ? `Checks cover your entire roster. The timeline below shows the last ${months} months.`
              : `The overlap check was skipped because this roster is too large. Every other check still covered your entire roster. The timeline below shows the last ${months} months.`}
          </p>
          <p className="text-xs text-text-muted">
            This page only reads. Nothing here changes a period.
          </p>
        </div>
      </section>

      {band.length > 0 && (
        <section className={card}>
          <div className={cardHeader}>
            <h2 className={cardTitle}>Issues not shown on the timeline</h2>
          </div>
          <div className="space-y-5 p-6">
            <p className="text-sm text-text-secondary">
              These concern periods outside the window below, or the roster as a
              whole.
            </p>
            <ul className="space-y-5">
              {band.map((anomaly, i) => (
                <BandEntry
                  key={`${anomaly.kind}-${i}`}
                  anomaly={anomaly}
                  refs={refs}
                />
              ))}
            </ul>
          </div>
        </section>
      )}

      <section className={card}>
        <div className={cardHeader}>
          <h2 className={cardTitle}>Timeline</h2>
        </div>
        <div className="space-y-5 p-6">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <label htmlFor="roster-months" className={labelCls}>
                Window
              </label>
              <select
                id="roster-months"
                value={months}
                onChange={(e) => onMonthsChange(Number(e.target.value))}
                className={`${inputCls} w-full sm:w-48`}
              >
                {MONTH_OPTIONS.map((m) => (
                  <option key={m} value={m}>
                    Last {m} months
                  </option>
                ))}
              </select>
            </div>
            {/*
              ⚠ `role="status"` lives HERE and nowhere else on the page: the
              caption is the only thing that changes with the window. The
              verdict above does not, so live-regioning it would announce an
              unchanged sentence on every interaction.
            */}
            <p
              role="status"
              aria-live="polite"
              className="text-sm text-text-secondary"
            >
              <WindowCaption
                roster={roster}
                win={win}
                periodCount={periods.length}
                months={months}
              />
            </p>
          </div>

          {periods.length > 0 && (
            <>
              <p className="text-xs text-text-muted">
                Opening a period in Transactions replaces your saved transaction
                filters.
              </p>
              <ol
                aria-label="Billing periods, oldest first"
                className="mt-2"
              >
                {renderRail({ periods, gaps, anomalies, refs, anchoredOpenId })}
              </ol>
            </>
          )}
        </div>
      </section>
    </div>
  );
}

function Verdict({ worst, count }: { worst: Tier | null; count: number }) {
  if (worst === null) {
    return (
      <p className={successCls}>
        No issues found in this organization&apos;s billing periods.
      </p>
    );
  }
  const noun = count === 1 ? "issue" : "issues";
  if (worst === "error") {
    return (
      <p className={errorCls}>
        {count} {noun} found, at least one serious. A serious issue means
        transactions can be counted twice, or counted in no period at all.
      </p>
    );
  }
  if (worst === "warning") {
    return (
      <p className={warningCls}>
        {count} {noun} found. None are serious, but they change which period a
        transaction lands in.
      </p>
    );
  }
  return (
    <p className="rounded-md border border-border bg-surface-raised px-4 py-3 text-sm text-text-secondary">
      {count} {noun} found. Nothing here changes your numbers.
    </p>
  );
}

function WindowCaption({
  roster,
  win,
  periodCount,
  months,
}: {
  roster: RosterResponse["roster"];
  win: RosterResponse["window"];
  periodCount: number;
  months: number;
}) {
  // ⚠ TWO empty states, and conflating them is a bug.
  if (roster.period_count === 0) {
    return (
      <>
        No billing periods yet. One is created the first time a screen asks for
        the current period.
      </>
    );
  }
  if (periodCount === 0) {
    return (
      <>
        None of this organization&apos;s {roster.period_count} periods start in
        the last {months} months. Widen the window to see them. Every check
        above still covered all {roster.period_count}.
      </>
    );
  }
  return (
    <>
      Showing {win.displayed_count} of {roster.period_count} periods
      {win.from ? (
        <>
          {" "}
          from <DateText iso={win.from} />
        </>
      ) : null}
      .
      {win.truncated
        ? ` More periods start in the last ${months} months than fit here, so only the newest are shown.`
        : ""}
    </>
  );
}

function BandEntry({
  anomaly,
  refs,
}: {
  anomaly: RosterAnomaly;
  refs: Record<string, ReferencedPeriod>;
}) {
  const copy = describeAnomaly(anomaly, refs);
  const named = anomalyPeriodIds(anomaly)
    .map((id) => refs[String(id)])
    .filter((p): p is ReferencedPeriod => Boolean(p));
  return (
    <li className="space-y-1.5">
      <MarkerChip copy={copy} />
      <p className="text-sm text-text-secondary">{copy.explanation}</p>
      {named.length > 0 && (
        <p className="text-xs text-text-muted">
          {named.map((p, i) => (
            <span key={p.id}>
              {i > 0 ? " · " : ""}
              Period starting <DateText iso={p.start_date} />, {statusWord(p.status)}
              , ends{" "}
              {p.effective_end ? <DateText iso={p.effective_end} /> : "not set yet"}
            </span>
          ))}
        </p>
      )}
    </li>
  );
}

function renderRail({
  periods,
  gaps,
  anomalies,
  refs,
  anchoredOpenId,
}: {
  periods: RosterPeriod[];
  gaps: ReturnType<typeof railBreakGaps>;
  anomalies: RosterAnomaly[];
  refs: Record<string, ReferencedPeriod>;
  anchoredOpenId: number | null;
}): ReactNode[] {
  const items: ReactNode[] = [];
  let lastYear: string | null = null;

  periods.forEach((period, index) => {
    const previous = periods[index - 1];
    if (previous) {
      const gap = gaps.find(
        (g) => g.from_period_id === previous.id && g.to_period_id === period.id,
      );
      if (gap) {
        const copy = describeAnomaly(gap, refs);
        // ⚠ A gap is a BREAK IN THE RAIL, not a badge on a row: no `border-l`
        // here, so the spine visibly stops and restarts.
        items.push(
          <li key={`gap-${gap.from_period_id}-${gap.to_period_id}`} className="py-2">
            <div className="border-t border-dashed border-border-strong" />
            <div className="space-y-1.5 py-3 pl-5 sm:pl-6">
              <MarkerChip copy={copy} />
              <p className="text-xs text-text-muted">{copy.explanation}</p>
            </div>
            <div className="border-t border-dashed border-border-strong" />
          </li>,
        );
        lastYear = null;
      }
    }

    const year = period.start_date.slice(0, 4);
    if (year !== lastYear) {
      lastYear = year;
      items.push(
        <li
          key={`year-${year}-${period.id}`}
          className="border-l border-border-subtle pb-2 pl-5 pt-5 first:pt-0 sm:pl-6"
        >
          <span className="text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">
            {year}
          </span>
        </li>,
      );
    }

    items.push(
      <RailRow
        key={period.id}
        period={period}
        anomalies={anomalies}
        refs={refs}
        isAnchoredOpen={period.id === anchoredOpenId}
      />,
    );
  });

  return items;
}

function RailRow({
  period,
  anomalies,
  refs,
  isAnchoredOpen,
}: {
  period: RosterPeriod;
  anomalies: RosterAnomaly[];
  refs: Record<string, ReferencedPeriod>;
  isAnchoredOpen: boolean;
}) {
  const inline = inlineAnomaliesFor(anomalies, period.id);
  // §2.1: divergence can only ever happen on an OPEN row, because
  // `period_spend_window_end` returns a closed row's end verbatim.
  const bothNull = period.effective_end === null && period.counting_through === null;
  const diverged =
    !bothNull && period.effective_end !== period.counting_through;

  return (
    <li className="relative border-l border-border-subtle pb-5 pl-5 sm:pl-6">
      <span
        aria-hidden="true"
        className={`absolute -left-1 top-1.5 h-2 w-2 rounded-full border border-border-strong ${
          isAnchoredOpen ? "bg-accent" : "bg-surface"
        }`}
      />

      {/* Anchor tier. */}
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <time
          dateTime={period.start_date}
          className="text-sm font-medium tabular-nums text-text-primary"
        >
          {period.start_date}
        </time>
        <span
          className={`text-sm ${isAnchoredOpen ? "text-accent" : "text-text-secondary"}`}
        >
          {statusWord(period.status)}
        </span>
      </div>

      {/* Row-scoped markers. Chip AND sentence, always: colour carries
          severity and nothing else, so a monochrome screenshot loses no
          signal. This block is why a broken row is TALL. */}
      {inline.map((anomaly, i) => {
        const copy = describeAnomaly(anomaly, refs);
        return (
          <div key={`${anomaly.kind}-${i}`} className="mt-1.5 space-y-1">
            <MarkerChip copy={copy} />
            <p className="text-xs text-text-muted">{copy.explanation}</p>
          </div>
        );
      })}

      {/* Substance tier: the two ends, §2.1. */}
      {bothNull ? (
        <>
          <p className="mt-1 text-sm tabular-nums text-text-secondary">
            Period ends not set yet · Counting through not set yet
          </p>
          <p className="mt-1 text-xs text-text-muted">
            This is the newest period on the roster, so nothing bounds it yet.
          </p>
        </>
      ) : diverged ? (
        <>
          <p className="mt-1 text-sm tabular-nums text-text-secondary">
            Period ends{" "}
            {period.effective_end ? (
              <DateText iso={period.effective_end} />
            ) : (
              "not set yet"
            )}
          </p>
          <p className="mt-1.5">
            <span className={badgeWarning}>
              <span className="sr-only">Issue: </span>
              <TriangleAlert className="h-3 w-3" aria-hidden="true" />
              Counting through {period.counting_through ?? "not set yet"}, past
              this period&apos;s end
            </span>
          </p>
          <p className="mt-1 text-xs text-text-muted">
            Spending is still counted into this period after the date it derives
            its end from, so budgets and this page disagree until it closes.
          </p>
        </>
      ) : (
        // Converged: both facts, ONE line, IDENTICAL styling. The repetition
        // is the "these agree" signal and must not be collapsed into a fused
        // label.
        <p className="mt-1 text-sm tabular-nums text-text-secondary">
          Period ends{" "}
          {period.effective_end ? (
            <DateText iso={period.effective_end} />
          ) : (
            "not set yet"
          )}{" "}
          · Counting through{" "}
          {period.counting_through ? (
            <DateText iso={period.counting_through} />
          ) : (
            "not set yet"
          )}
        </p>
      )}

      {/* Recessive tier: one wrapping line, never a column. */}
      <p className="mt-1 text-xs text-text-muted">
        <span className="tabular-nums">
          {period.length_days === null
            ? "Length not shown"
            : `${period.length_days} days`}
        </span>
        {" · "}
        <span className="tabular-nums">
          {period.transaction_count} transactions
        </span>
        {" · "}
        {/* Not colour-coded: on this page colour means severity, nothing else. */}
        <span className="tabular-nums">
          {/* ⚠ `> 0`, never `>= 0`. `Number("-0.00")` is `-0`, which satisfies
              `>= 0` while `formatAmount` still emits "-0.00" — printing
              "+-0.00". MySQL's DECIMAL normalises the sign away so this is
              unreachable in production today, but nothing guarantees that of
              the next store, and a sign prefix on a zero buys nothing. */}
          Net {Number(period.settled_net) > 0 ? "+" : ""}
          {formatAmount(period.settled_net)}
        </span>
        {period.counting_through && (
          <>
            {" · "}
            <Link
              href={`/transactions?date_from=${period.start_date}&date_to=${period.counting_through}`}
              className={`${btnLink} ${FOCUSABLE}`}
              aria-label={`Open ${period.start_date} to ${period.counting_through} in Transactions, replacing your saved transaction filters`}
            >
              Open in Transactions
            </Link>
          </>
        )}
      </p>
    </li>
  );
}
