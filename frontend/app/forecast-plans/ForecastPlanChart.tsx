"use client";

/**
 * Colocated recharts subtree for the Forecast Plans "Planned vs Actual"
 * bar chart. Extracted so the client page can dynamic-import it
 * (ssr:false) and keep recharts out of the route's initial JS. Data + the
 * bar-click navigation are passed in as props; this only renders the
 * chart.
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
import { SeriesTooltip } from "@/components/charts/SeriesTooltip";

export interface ForecastPlanChartDatum {
  categoryId: number;
  name: string;
  planned: number;
  actual: number;
}

export default function ForecastPlanChart({
  chartData,
  onBarClick,
}: {
  chartData: ForecastPlanChartDatum[];
  onBarClick: (name: string | undefined) => void;
}) {
  return (
    <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
      <BarChart
        data={chartData}
        layout="vertical"
        margin={{ left: 0, right: 20, top: 0, bottom: 0 }}
      >
        <XAxis type="number" hide />
        <YAxis
          type="category"
          dataKey="name"
          width={100}
          tick={{ fill: chartColor.axisTick, fontSize: 11 }}
        />
        <Tooltip
          content={
            <SeriesTooltip
              format={formatAmount}
              resolve={(entry) => {
                if (entry.dataKey === "planned") {
                  return { label: "Planned", color: chartColor.planned };
                }
                if (entry.dataKey === "actual") {
                  const row = entry.payload as
                    | { planned?: number; actual?: number }
                    | undefined;
                  const isOver = row
                    ? Number(row.actual) > Number(row.planned)
                    : false;
                  return {
                    label: "Actual",
                    color: isOver ? chartColor.over : chartColor.actual,
                  };
                }
                return null;
              }}
            />
          }
        />
        <Bar
          dataKey="planned"
          fill={chartColor.planned}
          radius={[4, 4, 4, 4]}
          animationDuration={220}
          cursor="pointer"
          onClick={(data) => onBarClick(data?.name || data?.payload?.name)}
        />
        <Bar
          dataKey="actual"
          fill={chartColor.actual}
          radius={[4, 4, 4, 4]}
          animationDuration={220}
        >
          {chartData.map((d) => (
            <Cell
              key={d.categoryId}
              fill={d.actual > d.planned ? chartColor.over : chartColor.actual}
            />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
