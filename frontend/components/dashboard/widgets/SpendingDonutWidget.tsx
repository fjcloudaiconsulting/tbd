"use client";

/**
 * SpendingDonutWidget — "Spending by Category" chart tile for the custom
 * dashboard canvas.
 *
 * Reads all data from DashboardDataProvider; the widget carries no data
 * itself. JSX is ported verbatim from LegacyDashboard (page.tsx lines
 * 891–1064) — do NOT sync the two manually; keep the legacy page as the
 * authoritative copy until the canvas fully replaces it.
 */
import { ChevronDown, ChevronUp, ChevronsUpDown, RefreshCw } from "lucide-react";
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from "recharts";

import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import { formatAmount } from "@/lib/format";
import { btnSecondary, card, cardTitle } from "@/lib/styles";
import { CHART_SERIES } from "@/lib/chart-colors";

export default function SpendingDonutWidget() {
  const {
    donutData,
    sortedSpending,
    chartFilter,
    chartFilterName,
    setChartFilter,
    spendingSort,
    toggleSpendingSort,
    rollupFailed,
    rollupLoading,
    onRetryRollup,
  } = useDashboard();

  return (
    <div className={`${card} p-5`} data-testid="spending-donut">
      <h2 className={`mb-3 ${cardTitle}`}>Spending by Category</h2>
      {chartFilter !== null && (
        <button onClick={() => setChartFilter(null)} className="mb-2 rounded-md bg-surface-overlay px-2.5 py-1 text-xs text-text-secondary hover:bg-surface-raised focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30">
          {/* The fallback is reachable: a category can be filtered from the
              Budget or Forecast bars with no rollup row, no budget and no
              forecast item to name it. Plain language, not "selected category"
              — the user picked it, and a label that reads like system
              vocabulary tells them the app lost track. */}
          Filtering: {chartFilterName ?? "one category"} &times;
        </button>
      )}
      {/* TBD-221: the numbers on this tile come from the UNGATED
          /api/v1/transactions/spending-by-category endpoint. When that call
          failed there is no number to show, and the tile says so rather than
          falling back to a client aggregation — a silent fallback to the wrong
          figure is the defect this ticket deleted.

          ⚠ `rollupFailed`, NOT `projectionFailed`. OnTrackTile's flag belongs
          to /api/v1/forecast, a different request against a different,
          feature-gated endpoint. Reading it here makes a forecast outage — or
          simply an org with Forecast switched off — render "Spending by
          category unavailable" over real settled expense. */}
      {rollupFailed ? (
        /* role="alert": this appears asynchronously, long after the page has
           settled, so without a live region a screen-reader user is told
           nothing at all (WCAG 2.2 AA 4.1.3). The Retry button's accessible
           name says WHICH retry it is — the dashboard renders up to three
           buttons otherwise all called "Retry". */
        <div
          role="alert"
          className="flex flex-wrap items-center gap-3 py-6 text-sm text-text-muted"
        >
          <span>Spending by category unavailable.</span>
          <button
            type="button"
            onClick={onRetryRollup}
            disabled={rollupLoading}
            aria-label="Retry loading spending by category"
            className={`${btnSecondary} text-xs disabled:opacity-50`}
          >
            <RefreshCw className="mr-1 inline h-3.5 w-3.5" aria-hidden="true" />
            Retry
          </button>
        </div>
      ) : donutData.length > 0 ? (
        <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-start">
          <div className="h-40 w-40 shrink-0">
            <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
              <PieChart>
                <Pie
                  data={donutData} cx="50%" cy="50%" innerRadius={35} outerRadius={65}
                  paddingAngle={2} dataKey="value" stroke="none" cursor="pointer"
                  onClick={(_, idx) => {
                    // Keyed by category_id (the rollup's identity), never by
                    // name — names are not unique across subcategories.
                    const cid = donutData[idx]?.categoryId;
                    if (cid != null) setChartFilter(chartFilter === cid ? null : cid);
                  }}
                >
                  {donutData.map((d, i) => (
                    <Cell key={d.categoryId} fill={CHART_SERIES[i % CHART_SERIES.length]}
                      opacity={chartFilter !== null && chartFilter !== d.categoryId ? 0.3 : 1} />
                  ))}
                </Pie>
                {/* Single-series pie: recharts renders the slice
                    name itself, so a value `formatter` is enough.
                    SeriesTooltip is only needed for the multi-series
                    bar charts where the name node failed to render. */}
                <Tooltip formatter={(v) => formatAmount(Number(v))} contentStyle={{ fontSize: "12px" }} />
              </PieChart>
            </ResponsiveContainer>
          </div>
          {/* D2 (2026-05-08): fill the description-to-amount
              gap with a "% of total" column instead of leaving
              it as dead whitespace. Layout: dot + name (flex-1
              truncate) + percent (right-aligned, fixed col) +
              amount (right-aligned, fixed col). Tabular-nums on
              both numeric columns keeps digits aligned across
              rows. */}
          <div className="w-full space-y-1.5 sm:flex-1">
            {/* Item 16 (D2): sortable column headers for Category,
                %, Amount. Persists via usePersistedSort. The leading
                "auto" column is the legend dot, which has no header.
                Each header carries an aria-sort state and a lucide
                chevron icon, with a brass focus ring matching the
                Pressable-Surfaces Rule in docs/design/DESIGN.md. */}
            <div
              role="row"
              className="grid w-full grid-cols-[auto_minmax(0,1fr)_3rem_auto] items-center gap-2 px-1.5 pb-1 text-[10px] uppercase tracking-wider text-text-muted"
            >
              <span aria-hidden="true" className="h-2.5 w-2.5" />
              <div
                role="columnheader"
                aria-sort={
                  spendingSort.field === "name"
                    ? spendingSort.dir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
              >
                <button
                  type="button"
                  onClick={() => toggleSpendingSort("name")}
                  className="inline-flex items-center gap-1 text-left min-h-[32px] hover:text-text-primary rounded-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                  aria-label="Sort by category"
                >
                  <span>Category</span>
                  {spendingSort.field === "name" ? (
                    spendingSort.dir === "asc" ? (
                      <ChevronUp className="h-3 w-3" aria-hidden="true" />
                    ) : (
                      <ChevronDown className="h-3 w-3" aria-hidden="true" />
                    )
                  ) : (
                    <ChevronsUpDown className="h-3 w-3 text-text-muted/60" aria-hidden="true" />
                  )}
                  <span className="sr-only">
                    {spendingSort.field === "name"
                      ? `sorted ${spendingSort.dir === "asc" ? "ascending" : "descending"}`
                      : "click to sort"}
                  </span>
                </button>
              </div>
              <div
                role="columnheader"
                aria-sort={
                  spendingSort.field === "percent"
                    ? spendingSort.dir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
                className="text-right"
              >
                <button
                  type="button"
                  onClick={() => toggleSpendingSort("percent")}
                  className="inline-flex items-center gap-1 justify-end min-h-[32px] hover:text-text-primary rounded-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                  aria-label="Sort by percent of total"
                >
                  <span>%</span>
                  {spendingSort.field === "percent" ? (
                    spendingSort.dir === "asc" ? (
                      <ChevronUp className="h-3 w-3" aria-hidden="true" />
                    ) : (
                      <ChevronDown className="h-3 w-3" aria-hidden="true" />
                    )
                  ) : (
                    <ChevronsUpDown className="h-3 w-3 text-text-muted/60" aria-hidden="true" />
                  )}
                  <span className="sr-only">
                    {spendingSort.field === "percent"
                      ? `sorted ${spendingSort.dir === "asc" ? "ascending" : "descending"}`
                      : "click to sort"}
                  </span>
                </button>
              </div>
              <div
                role="columnheader"
                aria-sort={
                  spendingSort.field === "amount"
                    ? spendingSort.dir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
                className="text-right"
              >
                <button
                  type="button"
                  onClick={() => toggleSpendingSort("amount")}
                  className="inline-flex items-center gap-1 justify-end min-h-[32px] hover:text-text-primary rounded-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                  aria-label="Sort by amount"
                >
                  <span>Amount</span>
                  {spendingSort.field === "amount" ? (
                    spendingSort.dir === "asc" ? (
                      <ChevronUp className="h-3 w-3" aria-hidden="true" />
                    ) : (
                      <ChevronDown className="h-3 w-3" aria-hidden="true" />
                    )
                  ) : (
                    <ChevronsUpDown className="h-3 w-3 text-text-muted/60" aria-hidden="true" />
                  )}
                  <span className="sr-only">
                    {spendingSort.field === "amount"
                      ? `sorted ${spendingSort.dir === "asc" ? "ascending" : "descending"}`
                      : "click to sort"}
                  </span>
                </button>
              </div>
            </div>
            {sortedSpending.slice(0, 10).map((d) => (
              <button key={d.categoryId} onClick={() => setChartFilter(chartFilter === d.categoryId ? null : d.categoryId)}
                className={`grid w-full grid-cols-[auto_minmax(0,1fr)_3rem_auto] items-center gap-2 rounded px-1.5 py-0.5 transition-colors hover:bg-surface-raised ${chartFilter === d.categoryId ? "bg-surface-overlay" : ""}`}>
                <div className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: CHART_SERIES[d.origIdx % CHART_SERIES.length] }} />
                <span className="min-w-0 truncate text-left text-xs text-text-secondary">{d.name}</span>
                {/* %/amount carry data, so they ride text-secondary
                    rather than the dimmer text-muted. */}
                <span className="text-right text-[10px] tabular-nums text-text-secondary">{d.pct.toFixed(0)}%</span>
                <span className="text-right text-xs tabular-nums text-text-secondary">{formatAmount(d.value)}</span>
              </button>
            ))}
            {sortedSpending.length > 10 && (
              <p className="px-1.5 text-[10px] text-text-muted">+{sortedSpending.length - 10} more (click chart to filter)</p>
            )}
          </div>
        </div>
      ) : rollupLoading ? (
        /* ⚠ AHEAD OF THE EMPTY STATE, BEHIND THE CHART. Without this arm the
           tile renders "No expense data yet" over every cold load and every
           period change — the exact sentence TBD-221 exists to stop showing
           over a period holding real settled expense. It sits behind the chart
           arm so a same-period refetch keeps the last good slices on screen
           instead of blinking to a spinner. */
        <p
          role="status"
          data-testid="donut-loading"
          className="text-sm text-text-muted py-6 text-center"
        >
          Loading spending by category…
        </p>
      ) : (
        <p className="text-sm text-text-muted py-6 text-center">No expense data yet</p>
      )}
    </div>
  );
}

