"use client";

/**
 * CreditUtilizationBar — one horizontal banded bar for a single credit card,
 * reusing the BudgetBars idiom (BudgetSpentBarShape + chartColor tokens; see
 * BudgetBarsWidget.tsx / app/budgets/BudgetOverviewChart.tsx). The numeric
 * domain is clamped to 100 so the fill maxes at the track; the overage is
 * surfaced in the text label, never by letting the bar exceed the track.
 * Bands (color earned only at the risky end; color never the sole signal,
 * every band pairs a color with text). "Over limit" means strictly over
 * (over > 0, i.e. utilization past 100%); exactly 100% is fully used, not
 * over, and reads as High — matching the accounts-page subline, which also
 * gates its "over" copy on over > 0:
 *   over > 0                     -> over (danger), "Over limit · {over} {ccy} over"
 *   util >= 75 && over <= 0       -> warning, "{util}% · High"
 *   util < 75                     -> neutral watch, "{util}%" (the % carries it)
 *
 * Note: the row payload intentionally omits `over` (unlike the Budgets
 * stacked chart, which renders a separate over-budget segment past the
 * track). Here `remaining` alone drives BudgetSpentBarShape's right-corner
 * rounding — leaving `over` unset keeps that math correct at >=100%
 * utilization, where `remaining` collapses to 0 and the spent segment
 * should round both corners.
 */
import { BarChart, Bar, XAxis, YAxis, Cell, ResponsiveContainer } from "recharts";
import { chartColor } from "@/lib/chart-colors";
import { creditUtilization } from "@/lib/credit";
import { formatAmount } from "@/lib/format";
import { BudgetSpentBarShape, type BudgetSpentBarShapeProps } from "@/lib/chart-shapes";

export interface CreditUtilizationBarProps {
  name: string;
  balance: number;
  creditLimit: number;
  currency: string;
}

export default function CreditUtilizationBar({ name, balance, creditLimit, currency }: CreditUtilizationBarProps) {
  const { utilizationPct, over } = creditUtilization(balance, creditLimit);
  const util = Math.round(utilizationPct);
  const spent = Math.min(utilizationPct, 100);
  const remaining = Math.max(0, 100 - utilizationPct);
  const isOver = over > 0;
  const isHigh = utilizationPct >= 75 && !isOver;
  const fill = isOver ? chartColor.over : isHigh ? "var(--color-warning)" : chartColor.watch;
  const data = [{ name, spent, remaining }];
  const label = isOver
    ? `Over limit · ${formatAmount(over)} ${currency} over`
    : isHigh
      ? `${util}% · High`
      : `${util}%`;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between text-xs text-text-secondary">
        <span className="truncate">{name}</span>
        <span className="tabular-nums">{label}</span>
      </div>
      <div className="h-4 w-full">
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 1, height: 1 }}>
          <BarChart data={data} layout="vertical" margin={{ left: 0, right: 0, top: 0, bottom: 0 }}>
            <XAxis type="number" domain={[0, 100]} hide />
            <YAxis type="category" dataKey="name" hide />
            <Bar
              dataKey="spent"
              stackId="a"
              shape={(props: BudgetSpentBarShapeProps) => <BudgetSpentBarShape {...props} />}
            >
              <Cell fill={fill} />
            </Bar>
            <Bar dataKey="remaining" stackId="a" fill={chartColor.remaining} radius={[0, 4, 4, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
