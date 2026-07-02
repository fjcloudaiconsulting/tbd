"use client";

/**
 * DashboardPeriodNav — fixed period-navigation chrome for the CustomDashboard.
 *
 * Reproduces the LegacyDashboard period bar (◀ / month label / ▶ / CURRENT
 * badge or "Today" button / "View All Transactions" link), reading
 * period state from the nearest DashboardDataProvider via useDashboard().
 *
 * Token-only: no raw hex / no raw Tailwind palette colors.
 */
import Link from "next/link";

import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import TourAnchor from "@/components/tour/TourAnchor";

export default function DashboardPeriodNav() {
  const {
    periods,
    periodIdx,
    setPeriodIdx,
    selectedPeriod,
    monthFrom,
    monthTo,
    jumpToCurrentPeriod,
  } = useDashboard();

  const isCurrentPeriod = selectedPeriod?.end_date === null;
  const isOldest = periodIdx >= periods.length - 1;
  const isNewest = periodIdx <= 0;

  return (
    // Tour anchor: `as="child"` keeps the DOM shape (the flex row is a direct
    // child of CustomDashboard's flex-column, so a wrapper <span> would break
    // the layout). Only CustomDashboard renders this component — LegacyDashboard
    // has its own inline period nav carrying the same id — so the two
    // `dashboard.period-nav` anchors never coexist in the DOM (one dashboard
    // renders at a time), keeping the id unique whenever the tour looks it up.
    <TourAnchor id="dashboard.period-nav" as="child">
    <div
      data-testid="dashboard-period-nav"
      className="mb-4 flex flex-wrap items-center justify-between gap-y-2"
    >
      <div className="flex items-center gap-2">
        {/* ◀ Previous period (higher index = older) */}
        <button
          type="button"
          aria-label="Previous period"
          disabled={isOldest}
          onClick={() => setPeriodIdx(periodIdx + 1)}
          className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-text-muted hover:bg-surface-raised disabled:opacity-30 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M15.75 19.5 8.25 12l7.5-7.5"
            />
          </svg>
        </button>

        {/* Month label */}
        <span className="text-sm font-medium text-text-primary">
          {monthFrom}
          {monthTo ? ` – ${monthTo}` : ""}
        </span>

        {/* ▶ Next period (lower index = newer) */}
        <button
          type="button"
          aria-label="Next period"
          disabled={isNewest}
          onClick={() => setPeriodIdx(periodIdx - 1)}
          className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded text-text-muted hover:bg-surface-raised disabled:opacity-30 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="m8.25 4.5 7.5 7.5-7.5 7.5"
            />
          </svg>
        </button>

        {/* CURRENT badge (open period) or "Today" jump button */}
        {isCurrentPeriod ? (
          <span
            data-testid="period-nav-current-badge"
            className="ml-1 rounded bg-success-dim px-2 py-0.5 text-[10px] font-semibold text-success"
          >
            CURRENT
          </span>
        ) : (
          <button
            type="button"
            data-testid="period-nav-today-btn"
            onClick={jumpToCurrentPeriod}
            className="ml-1 inline-flex min-h-[44px] items-center rounded-md px-3 text-xs font-medium text-text-muted hover:bg-surface-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
          >
            Today
          </button>
        )}
      </div>

      {/* View All Transactions */}
      <Link
        href="/transactions"
        className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary"
      >
        View All Transactions
      </Link>
    </div>
    </TourAnchor>
  );
}
