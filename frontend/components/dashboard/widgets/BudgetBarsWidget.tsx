"use client";

/**
 * BudgetBarsWidget — "Budget Progress" chart tile for the custom dashboard
 * canvas.
 *
 * Reads all data from DashboardDataProvider; the widget carries no data
 * itself. JSX is ported verbatim from LegacyDashboard (page.tsx lines
 * 1067–1124) — do NOT sync the two manually; keep the legacy page as the
 * authoritative copy until the canvas fully replaces it.
 */
import Link from "next/link";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Cell,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

import { useDashboard } from "@/components/dashboard/DashboardDataProvider";
import { formatAmount } from "@/lib/format";
import { card, cardHeader, cardTitle } from "@/lib/styles";
import { chartColor } from "@/lib/chart-colors";
import { SeriesTooltip } from "@/components/charts/SeriesTooltip";
import { resolveBudgetSeries } from "@/lib/reports/chart-series-tooltip";
import {
  BudgetSpentBarShape,
  type BudgetSpentBarShapeProps,
} from "@/lib/chart-shapes";

export default function BudgetBarsWidget() {
  const {
    budgets,
    dashBudgets,
    budgetChartData,
    chartFilter,
    setChartFilter,
    isPastSelectedPeriod,
    isFutureSelectedPeriod,
  } = useDashboard();

  return (
    <div className={`${card} flex flex-col overflow-hidden`}>
      <div className={`flex items-center justify-between ${cardHeader}`}>
        <h2 className={cardTitle}>Budget Progress</h2>
        <Link href="/budgets" className="text-xs text-text-secondary underline underline-offset-2 hover:text-text-primary">Manage</Link>
      </div>
      {budgets.length > 0 ? (
        <>
        {/* Flex-fill the space between header and legend so every category
            fits the resizable tile — more categories = thinner bars. */}
        <div className="w-full min-w-0 flex-1 min-h-0 p-4">
          <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
            <BarChart data={budgetChartData} layout="vertical" margin={{ left: 0, right: 20, top: 0, bottom: 0 }}>
              <XAxis type="number" hide />
              <YAxis type="category" dataKey="name" width={100} tick={{ fill: chartColor.axisTick, fontSize: 11 }} />
              <Tooltip
                content={
                  <SeriesTooltip format={formatAmount} resolve={resolveBudgetSeries} />
                }
              />
              {/* D5 follow-up: shared BudgetSpentBarShape so
                  the spent bar rounds its right edge at >=100%
                  utilization (when the trailing remaining
                  segment collapses to zero). Static
                  radius={[4,0,0,4]} left those rows squared. */}
              <Bar dataKey="spent" stackId="a" animationDuration={220}
                cursor="pointer"
                shape={(props: BudgetSpentBarShapeProps) => (
                  <BudgetSpentBarShape {...props} />
                )}
                onClick={(_, idx) => {
                  const name = dashBudgets[idx]?.category_name;
                  if (name) setChartFilter(chartFilter === name ? null : name);
                }}
              >
                {dashBudgets.map((b) => (
                  <Cell key={b.category_id} fill={b.percent_used > 100 ? chartColor.over : b.percent_used > 80 ? chartColor.watch : chartColor.spent} />
                ))}
              </Bar>
              <Bar dataKey="remaining" stackId="a" fill={chartColor.remaining} radius={[0, 4, 4, 0]} animationDuration={220} />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="flex flex-wrap gap-3 px-4 pb-3 text-[10px] text-text-secondary">
          <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.spent }} /> Spent</span>
          <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.watch }} /> &gt;80%</span>
          <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.over }} /> Over budget</span>
          <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.remaining }} /> Remaining</span>
        </div>
        </>
      ) : (
        <div className="px-5 py-6 text-center text-sm text-text-muted">
          {isPastSelectedPeriod
            ? <>No budgets were set for this period.</>
            : isFutureSelectedPeriod
              ? <>Future budgets live in Forecasts. <Link href="/forecast-plans" className="text-text-primary underline underline-offset-2 hover:text-text-secondary">Plan ahead →</Link></>
              : <>No budgets for this period. <Link href="/budgets" className="text-text-primary underline underline-offset-2 hover:text-text-secondary">Add one</Link></>
          }
        </div>
      )}
    </div>
  );
}
