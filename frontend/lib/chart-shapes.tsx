// Shared recharts shape primitives. Centralized so Dashboard and the
// dedicated Budgets surface render the same rounded-edge behavior at
// high utilization (D5 follow-up, 2026-05-08).
//
// The "spent" segment in a stacked horizontal budget bar should round
// its right edge ONLY when there is no follow-on segment (remaining or
// over) to its right; otherwise the bars wouldn't butt up cleanly. A
// static `radius={[4, 0, 0, 4]}` on the Bar leaves the right corners
// squared even at >=100% utilization (when the trailing segment has
// collapsed to zero width), which becomes visually obvious. This shape
// computes per-row corner radii from the row payload so both surfaces
// pick up the rounding rule once.

import type { ReactElement } from "react";

export interface BudgetSpentBarPayload {
  spent?: number;
  remaining?: number;
  over?: number;
}

export interface BudgetSpentBarShapeProps {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  fill?: string;
  payload?: BudgetSpentBarPayload;
}

/**
 * Decide the per-corner radius for a stacked "spent" bar segment.
 * Exposed for unit testing; consumers should use `BudgetSpentBarShape`.
 *
 * Rule: left corners always rounded (this segment always starts at the
 * row origin). Right corners rounded only when no trailing segment
 * (remaining > 0 or over > 0) would render to the right of this one.
 */
export function budgetSpentBarRadii(
  payload: BudgetSpentBarPayload | undefined,
  width: number,
  height: number,
): { left: number; right: number } {
  const remaining = payload?.remaining ?? 0;
  const over = payload?.over ?? 0;
  const isFullRow = remaining <= 0 && over <= 0;
  const r = Math.min(4, height / 2, width / 2);
  return { left: r, right: isFullRow ? r : 0 };
}

/**
 * Recharts custom shape for the "spent" Bar in a stacked horizontal
 * budget chart. Use as `<Bar shape={(props) => <BudgetSpentBarShape {...props} />} />`.
 *
 * Works for surfaces that stack {spent, remaining} (Dashboard) and
 * {spent, remaining, over} (Budgets) — surfaces without an `over`
 * segment simply have `over=0` in the row payload.
 */
export function BudgetSpentBarShape(
  props: BudgetSpentBarShapeProps,
): ReactElement | null {
  const { x = 0, y = 0, width = 0, height = 0, fill, payload } = props;
  if (width <= 0 || height <= 0) return null;
  const { left, right } = budgetSpentBarRadii(payload, width, height);
  // Clockwise rounded rect with per-side radius. Left corners always
  // round (this bar starts at the row origin); right corners pick up
  // rounding only when no follow-on segment exists.
  const path = [
    `M ${x + left} ${y}`,
    right > 0
      ? `H ${x + width - right} Q ${x + width} ${y} ${x + width} ${y + right}`
      : `H ${x + width}`,
    right > 0
      ? `V ${y + height - right} Q ${x + width} ${y + height} ${x + width - right} ${y + height}`
      : `V ${y + height}`,
    `H ${x + left} Q ${x} ${y + height} ${x} ${y + height - left}`,
    `V ${y + left} Q ${x} ${y} ${x + left} ${y}`,
    "Z",
  ].join(" ");
  return <path d={path} fill={fill} style={{ cursor: "pointer" }} />;
}
