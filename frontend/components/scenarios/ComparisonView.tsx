"use client";

/**
 * Comparison view for the Plans simulation sandbox (PR3 of the Plans
 * train).
 *
 * Architect-locked invariants:
 * - Up to 3 scenarios overlaid on a single chart. The endpoint
 *   enforces the cap; this component renders whatever the API
 *   hands back (1, 2, or 3 series).
 * - Shared Y-axis across all scenarios: the y-domain stretches to
 *   the max range across every plan so amounts are visually
 *   comparable.
 * - One color per scenario (NOT per account — comparison is the
 *   plan, not the underlying accounts). Each scenario's series is
 *   the TOTAL projected balance across all accounts at each month.
 * - Verdict matrix below the chart: a grid of name / verdict /
 *   end-balance / dip alerts per scenario.
 * - Read-only — the editor lives on /plans/[id], not here.
 */

import { useMemo } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
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

export interface AffordabilityVerdict {
  color: "green" | "yellow" | "red";
  headline: string;
  reason: string;
}

export interface ProjectionResult {
  engine_name: string;
  horizon_months: number;
  currency: string;
  per_account_series: AccountSeries[];
  alerts: DipAlert[];
  verdict: AffordabilityVerdict;
}

export interface CompareProjection {
  scenario_id: number;
  name: string;
  scenario_type: "trip" | "purchase" | "retirement" | "custom";
  projection: ProjectionResult;
}

const SCENARIO_COLORS = [
  "var(--color-accent)",
  "var(--color-info)",
  "var(--color-success)",
];

function pickColor(index: number): string {
  return SCENARIO_COLORS[index % SCENARIO_COLORS.length];
}

// X-axis tick formatter: same logic as ProjectionChart for visual
// consistency. Year-only for horizons > 36 months, month-year otherwise.
function tickFormatter(monthsTotal: number) {
  return (label: string) => {
    if (!/^\d{4}-\d{2}$/.test(label)) return label;
    const [yyyy, mm] = label.split("-");
    if (monthsTotal > 36) {
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
  // Per-scenario totals are keyed by ``scen_<id>`` so multiple lines
  // can share a single chart row.
  [scenarioKey: string]: number | string;
}

function totalBalanceAtMonth(
  projection: ProjectionResult,
  month: string,
): number {
  let total = 0;
  for (const series of projection.per_account_series) {
    const point = series.points.find((p) => p.month === month);
    if (point) total += Number(point.projected_balance);
  }
  return total;
}

function endingBalance(projection: ProjectionResult): number {
  let total = 0;
  for (const series of projection.per_account_series) {
    if (series.points.length === 0) continue;
    const last = series.points[series.points.length - 1];
    total += Number(last.projected_balance);
  }
  return total;
}

/**
 * Build the union of months across every projection. Each projection
 * is expected to have the same horizon (the compare endpoint enforces
 * a single horizon for the whole comparison), but the union guards
 * against any drift.
 */
function unionMonths(projections: CompareProjection[]): string[] {
  const seen = new Set<string>();
  for (const cp of projections) {
    const firstSeries = cp.projection.per_account_series[0];
    if (!firstSeries) continue;
    for (const point of firstSeries.points) {
      seen.add(point.month);
    }
  }
  return Array.from(seen).sort();
}

/**
 * Compute the y-domain max across ALL scenarios so the shared y-axis
 * doesn't truncate any series. Returns [min, max] with a small padding.
 *
 * Min is set to min(0, observedMin) so a scenario that dips below zero
 * still shows the dip; otherwise the axis floor is 0.
 */
export function sharedYDomain(
  projections: CompareProjection[],
  months: string[],
): [number, number] {
  let observedMin = 0;
  let observedMax = 0;
  for (const cp of projections) {
    for (const month of months) {
      const value = totalBalanceAtMonth(cp.projection, month);
      if (value < observedMin) observedMin = value;
      if (value > observedMax) observedMax = value;
    }
  }
  const padding = Math.max(1, (observedMax - observedMin) * 0.05);
  return [
    observedMin < 0 ? observedMin - padding : 0,
    observedMax + padding,
  ];
}

const VERDICT_BADGE: Record<AffordabilityVerdict["color"], string> = {
  green: "bg-success-dim text-success",
  yellow: "bg-accent/15 text-accent",
  red: "bg-danger-dim text-danger",
};

export function ComparisonView({
  projections,
  onOpen,
  testId = "comparison-view",
}: {
  projections: CompareProjection[];
  onOpen?: (scenarioId: number) => void;
  testId?: string;
}) {
  const months = useMemo(() => unionMonths(projections), [projections]);
  const rows = useMemo<ChartRow[]>(() => {
    return months.map((month) => {
      const row: ChartRow = { month };
      for (const cp of projections) {
        row[`scen_${cp.scenario_id}`] = totalBalanceAtMonth(
          cp.projection,
          month,
        );
      }
      return row;
    });
  }, [months, projections]);

  const yDomain = useMemo(
    () => sharedYDomain(projections, months),
    [projections, months],
  );
  const currency =
    projections[0]?.projection.currency ?? "EUR";

  if (projections.length === 0) {
    return (
      <p
        className="text-sm text-text-muted"
        data-testid={`${testId}-empty`}
      >
        Pick at least one plan to compare.
      </p>
    );
  }

  return (
    <section data-testid={testId}>
      <div
        className="mb-4 h-80 w-full"
        data-testid={`${testId}-chart`}
        role="img"
        aria-label="Projected balances across the selected plans"
      >
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={rows}
            margin={{ top: 16, right: 16, left: 0, bottom: 0 }}
          >
            <CartesianGrid
              stroke="var(--color-border)"
              strokeDasharray="3 3"
            />
            <XAxis
              dataKey="month"
              tick={{ fill: chartColor.axisTick, fontSize: 11 }}
              tickFormatter={tickFormatter(months.length)}
              interval="preserveStartEnd"
              minTickGap={20}
            />
            <YAxis
              domain={yDomain}
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
                const num =
                  typeof value === "number" ? value : Number(value);
                return [`${formatAmount(num)} ${currency}`, String(name)];
              }}
            />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            {projections.map((cp, idx) => (
              <Line
                key={cp.scenario_id}
                type="monotone"
                dataKey={`scen_${cp.scenario_id}`}
                name={cp.name}
                stroke={pickColor(idx)}
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div
        className="overflow-x-auto"
        data-testid={`${testId}-verdict-matrix`}
      >
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-text-muted">
              <th className="py-2 pr-3">Plan</th>
              <th className="py-2 pr-3">Verdict</th>
              <th className="py-2 pr-3">End balance</th>
              <th className="py-2 pr-3">Alerts</th>
              {onOpen && <th className="py-2 pr-3"></th>}
            </tr>
          </thead>
          <tbody>
            {projections.map((cp, idx) => {
              const ending = endingBalance(cp.projection);
              const verdict = cp.projection.verdict;
              const dipCount = cp.projection.alerts.length;
              return (
                <tr
                  key={cp.scenario_id}
                  className="border-b border-border last:border-0"
                  data-testid={`${testId}-row-${cp.scenario_id}`}
                >
                  <td className="py-2 pr-3">
                    <span
                      className="mr-2 inline-block h-2 w-2 rounded-full align-middle"
                      style={{ backgroundColor: pickColor(idx) }}
                      aria-hidden="true"
                    />
                    <span className="font-medium text-text-primary">
                      {cp.name}
                    </span>
                  </td>
                  <td className="py-2 pr-3">
                    <span
                      className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${VERDICT_BADGE[verdict.color]}`}
                      data-testid={`${testId}-verdict-${cp.scenario_id}`}
                    >
                      {verdict.color.toUpperCase()}
                    </span>
                  </td>
                  <td className="py-2 pr-3 tabular-nums text-text-primary">
                    {formatAmount(ending)} {cp.projection.currency}
                  </td>
                  <td className="py-2 pr-3 text-text-primary">
                    {dipCount > 0 ? (
                      <span
                        className="text-danger"
                        data-testid={`${testId}-alerts-${cp.scenario_id}`}
                      >
                        {dipCount} dip{dipCount === 1 ? "" : "s"}
                      </span>
                    ) : (
                      <span className="text-text-muted">None</span>
                    )}
                  </td>
                  {onOpen && (
                    <td className="py-2 pr-3 text-right">
                      <button
                        type="button"
                        className="text-xs text-accent underline-offset-2 hover:underline"
                        onClick={() => onOpen(cp.scenario_id)}
                        data-testid={`${testId}-open-${cp.scenario_id}`}
                      >
                        Open
                      </button>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
