"use client";

/**
 * ForecastBarsWidget — "Forecast by Category" chart tile for the custom
 * dashboard canvas.
 *
 * Reads all data from DashboardDataProvider; the widget carries no data
 * itself. JSX is ported verbatim from LegacyDashboard (page.tsx lines
 * 1127–1182) — do NOT sync the two manually; keep the legacy page as the
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
import { card, cardTitle } from "@/lib/styles";
import { chartColor } from "@/lib/chart-colors";
import { SeriesTooltip } from "@/components/charts/SeriesTooltip";
import { resolveForecastSeries } from "@/lib/reports/chart-series-tooltip";

export default function ForecastBarsWidget() {
  const {
    forecast,
    forecastExpenseItems,
    forecastChartRows,
    chartFilter,
    setChartFilter,
    isPastSelectedPeriod,
    isFutureSelectedPeriod,
  } = useDashboard();

  return (
    <div className={`${card} flex flex-col overflow-hidden p-5`}>
      <h2 className={`mb-3 ${cardTitle}`}>Forecast by Category</h2>
      {(() => {
        if (forecast && forecastExpenseItems.length > 0) {
          return (
            // Flex-fill so every expense category fits the resizable tile —
            // more categories = thinner bars.
            <div className="w-full min-w-0 flex-1 min-h-0">
              <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
                <BarChart
                  data={forecastChartRows}
                  layout="vertical"
                  margin={{ left: 0, right: 20, top: 0, bottom: 0 }}
                >
                  <XAxis type="number" hide />
                  <YAxis type="category" dataKey="name" width={90} tick={{ fill: chartColor.axisTick, fontSize: 10 }} />
                  <Tooltip
                    content={
                      <SeriesTooltip format={formatAmount} resolve={resolveForecastSeries} />
                    }
                  />
                  <Bar dataKey="planned" fill={chartColor.planned} radius={[4, 4, 4, 4]} animationDuration={220}
                    cursor="pointer"
                    onClick={(_, idx) => {
                      const name = forecastExpenseItems[idx]?.category_name;
                      if (name) setChartFilter(chartFilter === name ? null : name);
                    }}
                  />
                  <Bar dataKey="actual" fill={chartColor.actual} radius={[4, 4, 4, 4]} animationDuration={220}>
                    {forecastChartRows.map((d) => (
                      <Cell
                        key={d.categoryId}
                        fill={d.actual > d.planned ? chartColor.over : chartColor.actual}
                      />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          );
        }
        return (
          <p className="text-sm text-text-muted py-6 text-center">
            {isPastSelectedPeriod
              ? <>No forecast was set for this period.</>
              : isFutureSelectedPeriod
                ? <>No forecast for this future period. <Link href="/forecast-plans" className="text-text-primary underline underline-offset-2 hover:text-text-secondary">Plan ahead</Link>.</>
                : <>No forecast for this period. <Link href="/forecast-plans" className="text-text-primary underline underline-offset-2 hover:text-text-secondary">Set one up</Link>.</>
            }
          </p>
        );
      })()}
      <div className="mt-2 flex flex-wrap gap-3 text-[10px] text-text-secondary">
        <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.planned }} /> Planned</span>
        <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.actual }} /> Under plan</span>
        <span className="flex items-center gap-1"><span className="inline-block h-2 w-2 rounded-full" style={{ background: chartColor.over }} /> Over plan</span>
      </div>
    </div>
  );
}
