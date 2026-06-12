"use client";

/**
 * Colocated recharts subtree for the Budgets page "Budget Overview" bar
 * chart. Extracted so the page can dynamic-import it (ssr:false) and keep
 * recharts out of the budgets route's initial JS. Data + the bar-click
 * navigation are passed in as props; this only renders the chart.
 */
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatAmount } from "@/lib/format";
import { chartColor } from "@/lib/chart-colors";
import { BudgetSpentBarShape, type BudgetSpentBarShapeProps } from "@/lib/chart-shapes";
import { SeriesTooltip } from "@/components/charts/SeriesTooltip";

export interface BudgetOverviewDatum {
  name: string;
  spent: number;
  remaining: number;
  over: number;
}

export default function BudgetOverviewChart({
  budgetChartData,
  cellMeta,
  onBarClick,
}: {
  budgetChartData: BudgetOverviewDatum[];
  // Index-aligned per-row color driver (percent_used) + a stable key.
  cellMeta: Array<{ category_id: number; percent_used: number }>;
  onBarClick: (name: string | undefined) => void;
}) {
  return (
    <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
      <BarChart data={budgetChartData} layout="vertical" margin={{ left: 0, right: 0, top: 0, bottom: 0 }}>
        <XAxis type="number" hide />
        <YAxis type="category" dataKey="name" width={100} tick={{ fill: chartColor.axisTick, fontSize: 11 }} />
        <Tooltip
          content={
            <SeriesTooltip
              format={formatAmount}
              resolve={(entry) =>
                entry.dataKey === "spent"
                  ? { label: "Spent", color: chartColor.spent }
                  : entry.dataKey === "over"
                    ? { label: "Over budget", color: chartColor.over }
                    : { label: "Remaining", color: chartColor.remaining }
              }
            />
          }
        />
        {/* D5 fix: shared BudgetSpentBarShape recomputes corner radii
            per-row so a stack at >=100% utilization (no remaining
            segment, no over segment) still rounds its right edge. */}
        <Bar dataKey="spent" stackId="a" animationDuration={220}
          cursor="pointer"
          shape={(props: BudgetSpentBarShapeProps) => (
            <BudgetSpentBarShape {...props} />
          )}
          onClick={(data) => onBarClick(data?.name || data?.payload?.name)}
        >
          {cellMeta.map((b) => (
            <Cell
              key={b.category_id}
              fill={b.percent_used > 100 ? chartColor.over : b.percent_used > 80 ? chartColor.watch : chartColor.spent}
            />
          ))}
        </Bar>
        <Bar dataKey="remaining" stackId="a" fill={chartColor.remaining} radius={[0, 4, 4, 0]} animationDuration={220} />
        <Bar dataKey="over" stackId="a" fill={chartColor.over} radius={[4, 4, 4, 4]} animationDuration={220} />
      </BarChart>
    </ResponsiveContainer>
  );
}
