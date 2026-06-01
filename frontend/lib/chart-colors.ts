// Shared chart color tokens. Centralized so Dashboard and the dedicated
// Budget / Forecast surfaces don't drift apart visually (D4, 2026-05-08).
//
// Each value is a CSS variable defined in `app/globals.css` so theme
// switches cascade automatically and we never embed raw palette hexes
// in component code.
//
// Semantic intent — keep these mappings stable across surfaces:
//   PLANNED   → accent (gold)            the user's intended commitment
//   ACTUAL    → success (green)          settled spending under plan
//   SPENT     → accent (gold)            same gold as PLANNED, intentional
//   WATCH     → text-secondary (neutral) 80%-100% utilization
//   OVER      → danger (red)             over plan / over budget
//   REMAINING → border (neutral track)   remaining headroom in a stack
export const chartColor = {
  planned: "var(--color-accent)",
  actual: "var(--color-success)",
  spent: "var(--color-accent)",
  watch: "var(--color-text-secondary)",
  over: "var(--color-danger)",
  remaining: "var(--color-border)",
  axisTick: "var(--color-text-secondary)",
} as const;

// Categorical palette for multi-series charts (e.g. a bar chart split
// into one stacked segment per account). Each entry is a theme-aware CSS
// variable so light/dark switches cascade; series index modulo the
// length picks a color so we never run out. Ordered for adjacent-segment
// contrast. The trailing fallbacks keep the list usable even if a theme
// hasn't defined every optional token.
export const categoricalColors: readonly string[] = [
  "var(--color-accent)",
  "var(--color-success)",
  "var(--color-info, var(--color-accent))",
  "var(--color-warning, var(--color-text-secondary))",
  "var(--color-danger)",
  "var(--color-text-secondary)",
  "var(--color-accent-2, var(--color-success))",
  "var(--color-text-muted)",
] as const;

/** Pick a distinct categorical color by series index (wraps around). */
export function categoricalColor(index: number): string {
  return categoricalColors[index % categoricalColors.length];
}
