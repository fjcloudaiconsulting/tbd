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
          formatter={(v, name) => [
            formatAmount(Number(v)),
            name === "planned" ? <span style={{ color: chartColor.planned }}>Planned</span> : <span style={{ color: chartColor.actual }}>Actual</span>,
          ]}
          contentStyle={{ fontSize: "11px" }}
        />
        <Bar
          dataKey="planned"
          fill={chartColor.planned}
          radius={[4, 4, 4, 4]}
          animationDuration={600}
          cursor="pointer"
          onClick={(data) => onBarClick(data?.name || data?.payload?.name)}
        />
        <Bar
          dataKey="actual"
          fill={chartColor.actual}
          radius={[4, 4, 4, 4]}
          animationDuration={600}
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
