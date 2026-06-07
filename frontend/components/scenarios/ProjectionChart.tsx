"use client";

/**
 * Projection chart for the Plans simulation sandbox (PR2 of the
 * Plans train).
 *
 * Architect-locked invariants:
 * - Stacked area for per-account balances over the horizon.
 * - Tooltip shows the month, total balance, and per-account split.
 * - Y-axis is balance, currency-formatted via the existing
 *   `formatAmount` helper.
 * - X-axis labels adapt to horizon: "MMM 'YY" for short horizons,
 *   year-only ("YYYY") for long ones.
 * - Red dots mark months where any account dipped below its
 *   minimum (zero in v1).
 * - Retirement plans get a second overlaid line for the inflation-
 *   adjusted real-terms balance.
 *
 * The component is intentionally presentational: the projection
 * blob comes from the parent, which is what owns the simulate
 * call. That keeps the chart reusable for a future "compare
 * scenarios" view (PR3) without coupling to API shape.
 */

import { useEffect, useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Dot,
  Line,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Legend,
} from "recharts";

import { formatAmount } from "@/lib/format";
import { chartColor } from "@/lib/chart-colors";

export interface ProjectionPoint {
  month: string;
  projected_balance: string;
}

export interface AccountSeries {
  account_id: number;
  account_name: string;
  currency: string;
  points: ProjectionPoint[];
}

export interface DipAlert {
  account_id: number;
  month: string;
  projected_balance: string;
  trigger: string;
  severity: "info" | "warn" | "critical";
}

export interface RealTermsSeries {
  points: ProjectionPoint[];
  inflation_pct: string;
}

export interface ProjectionInput {
  per_account_series: AccountSeries[];
  alerts: DipAlert[];
  real_terms_series?: RealTermsSeries | null;
  currency: string;
}

// Canonical categorical chart palette (theme tokens, mirrors the
// dashboard) so per-account areas don't drift across surfaces. chart-5
// (danger/red) sits last; genuine negative semantics (the real-terms
// line, dip alert dots) keep their own explicit danger color below.
const SERIES_COLORS = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
  "var(--color-chart-5)",
];

function pickColor(index: number): string {
  return SERIES_COLORS[index % SERIES_COLORS.length];
}

// X-axis tick formatter. Months come in as "YYYY-MM"; for horizons
// > 36 months we collapse to year-only so the axis doesn't overflow.
function tickFormatter(monthsTotal: number) {
  return (label: string) => {
    if (!/^\d{4}-\d{2}$/.test(label)) return label;
    const [yyyy, mm] = label.split("-");
    if (monthsTotal > 36) {
      // Year-only labels, but only on January ticks so the axis
      // stays evenly spaced.
      if (mm === "01") return yyyy;
      return "";
    }
    const monthIndex = Number(mm) - 1;
    const monthShort = new Date(Number(yyyy), monthIndex, 1).toLocaleString(
      undefined,
      { month: "short" },
    );
    return `${monthShort} '${yyyy.slice(2)}`;
  };
}

interface ChartRow {
  month: string;
  total: number;
  real?: number;
  // Per-account-id keyed balances. We use the account_id as the data key.
  [accountKey: string]: number | string | undefined;
}

export function ProjectionChart({
  projection,
  testId = "projection-chart",
}: {
  projection: ProjectionInput;
  testId?: string;
}) {
  // ResponsiveContainer measures its parent on mount. When the chart
  // lives in a freshly painted flex/grid pane (the right column of the
  // Plans editor), the parent's width can come back as -1 on the first
  // synchronous read, which logs the loud "width(-1) and height(-1) of
  // chart should be greater than 0" warning. Deferring the
  // ResponsiveContainer render by one effect tick gives the layout
  // engine a chance to commit before Recharts measures.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  const months = useMemo(
    () =>
      projection.per_account_series.length > 0
        ? projection.per_account_series[0].points.map((p) => p.month)
        : [],
    [projection],
  );

  const rows = useMemo<ChartRow[]>(() => {
    const realByMonth = new Map<string, number>();
    projection.real_terms_series?.points.forEach((p) => {
      realByMonth.set(p.month, Number(p.projected_balance));
    });
    return months.map((month) => {
      const row: ChartRow = { month, total: 0 };
      projection.per_account_series.forEach((series) => {
        const point = series.points.find((p) => p.month === month);
        const value = point ? Number(point.projected_balance) : 0;
        row[`acc_${series.account_id}`] = value;
        row.total += value;
      });
      const real = realByMonth.get(month);
      if (real !== undefined) row.real = real;
      return row;
    });
  }, [months, projection]);

  // Reference dots: red markers on every alert month for any account.
  const alertDots = useMemo(
    () =>
      projection.alerts.map((a) => ({
        month: a.month,
        value: Number(a.projected_balance),
        trigger: a.trigger,
        account_id: a.account_id,
      })),
    [projection.alerts],
  );

  if (months.length === 0) {
    return (
      <p className="text-sm text-text-muted" data-testid={`${testId}-empty`}>
        No projected months yet.
      </p>
    );
  }

  return (
    <div
      // `min-w-0` is required because the chart sits in a CSS-grid
      // column (`minmax(0, 2fr)`). Without it, a wide chart row can
      // bully its grid track wider than the column's intrinsic min and
      // ResponsiveContainer's parent read goes negative on the first
      // measurement. `min-h-0` does the same for the vertical axis.
      className="h-72 w-full min-w-0 min-h-0"
      data-testid={testId}
      role="img"
      aria-label="Projected account balances over the horizon"
    >
      {!mounted ? null : (
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart
          data={rows}
          margin={{ top: 16, right: 16, left: 0, bottom: 0 }}
        >
          <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" />
          <XAxis
            dataKey="month"
            tick={{ fill: chartColor.axisTick, fontSize: 11 }}
            tickFormatter={tickFormatter(months.length)}
            interval="preserveStartEnd"
            minTickGap={20}
          />
          <YAxis
            tick={{ fill: chartColor.axisTick, fontSize: 11 }}
            tickFormatter={(v) =>
              formatAmount(typeof v === "number" ? v : Number(v))
            }
            width={80}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "var(--color-surface)",
              border: "1px solid var(--color-border)",
              fontSize: 12,
            }}
            formatter={(value, name) => {
              const num = typeof value === "number" ? value : Number(value);
              return [
                `${formatAmount(num)} ${projection.currency}`,
                String(name),
              ];
            }}
          />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          {projection.per_account_series.map((series, idx) => (
            <Area
              key={series.account_id}
              type="monotone"
              dataKey={`acc_${series.account_id}`}
              name={series.account_name}
              stackId="balances"
              stroke={pickColor(idx)}
              fill={pickColor(idx)}
              fillOpacity={0.3}
              isAnimationActive={false}
            />
          ))}
          {projection.real_terms_series && (
            <Line
              type="monotone"
              dataKey="real"
              name={`Real terms (${projection.real_terms_series.inflation_pct}% infl.)`}
              stroke="var(--color-danger)"
              strokeDasharray="4 2"
              dot={false}
              isAnimationActive={false}
            />
          )}
          {alertDots.map((d, i) => (
            <ReferenceDot
              key={`alert-${i}-${d.account_id}-${d.month}`}
              x={d.month}
              y={d.value}
              r={5}
              fill="var(--color-danger)"
              stroke="var(--color-danger)"
              ifOverflow="extendDomain"
              data-testid={`projection-alert-dot-${i}`}
            />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
      )}
    </div>
  );
}

// Tiny helper consumed in tests: returns a custom Dot factory for the
// alert layer when the parent wants to render markers inline with a
// series rather than as ReferenceDots. Kept exported so the same
// rendering helper can be reused by PR3's comparison view.
export function alertDotFactory(
  alerts: DipAlert[],
  color: string = "var(--color-danger)",
) {
  return function AlertDot(props: { cx?: number; cy?: number; payload?: ChartRow }) {
    const month = props.payload?.month;
    if (!month) return null;
    const isAlert = alerts.some((a) => a.month === month);
    if (!isAlert) return null;
    return (
      <Dot
        cx={props.cx}
        cy={props.cy}
        r={4}
        fill={color}
        stroke={color}
      />
    );
  };
}
