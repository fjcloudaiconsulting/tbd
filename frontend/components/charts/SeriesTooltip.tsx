"use client";

/**
 * Shared recharts tooltip for category bar charts (forecast / budget).
 *
 * recharts' default item template (and a `formatter` that returns a
 * coloured `name` node) does NOT reliably render the series name in
 * recharts 3.x, so these charts showed bare numbers with no indication
 * of which value was Planned vs Actual/Spent/Remaining. This component
 * takes full control of `content` and renders, per series row:
 *
 *     ▢ <label>            <value>
 *
 * The swatch colour matches the bar (including dynamic per-row colours
 * like green-under / red-over) because the caller resolves it from the
 * hovered row. Colours come in from the caller as theme tokens
 * (`var(--color-*)`), so this file holds no off-token literals.
 */
import type { ReactNode } from "react";

export interface TooltipSeries {
  /** Human label, e.g. "Planned" / "Spent". */
  label: string;
  /** Swatch colour — a CSS colour string, normally a `var(--color-*)` token. */
  color: string;
}

/** One entry recharts hands us in `payload`. */
export interface SeriesTooltipEntry {
  dataKey?: string | number;
  value?: number | string;
  name?: string;
  payload?: Record<string, unknown>;
}

export interface SeriesTooltipProps {
  active?: boolean;
  payload?: SeriesTooltipEntry[];
  /** Category title (recharts passes the x/y category here). */
  label?: ReactNode;
  /**
   * Map a hovered series entry → its label + swatch colour. Return
   * `null` to omit the row (e.g. a zero-height stacked segment).
   */
  resolve: (entry: SeriesTooltipEntry) => TooltipSeries | null;
  /** Value formatter (e.g. the page's `formatAmount`). */
  format: (value: number) => string;
}

export function SeriesTooltip({
  active,
  payload,
  label,
  resolve,
  format,
}: SeriesTooltipProps) {
  if (!active || !payload || payload.length === 0) return null;

  const rows = payload
    .map((entry) => ({ entry, series: resolve(entry) }))
    .filter((r): r is { entry: SeriesTooltipEntry; series: TooltipSeries } =>
      r.series !== null,
    );

  if (rows.length === 0) return null;

  return (
    <div
      style={{
        background: "var(--color-surface)",
        border: "1px solid var(--color-border)",
        borderRadius: 8,
        padding: "8px 10px",
        fontSize: 11,
        boxShadow: "var(--shadow-card)",
        minWidth: 120,
      }}
    >
      {!!label && (
        <div
          style={{
            fontWeight: 600,
            marginBottom: 4,
            color: "var(--color-text-primary)",
          }}
        >
          {label}
        </div>
      )}
      {rows.map(({ entry, series }, i) => (
        <div
          key={String(entry.dataKey ?? i)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "1px 0",
          }}
        >
          <span
            aria-hidden="true"
            style={{
              width: 9,
              height: 9,
              borderRadius: 2,
              background: series.color,
              flex: "0 0 auto",
            }}
          />
          <span style={{ color: "var(--color-text-secondary)" }}>
            {series.label}
          </span>
          <span
            style={{
              marginLeft: "auto",
              paddingLeft: 12,
              fontVariantNumeric: "tabular-nums",
              color: "var(--color-text-primary)",
            }}
          >
            {format(Number(entry.value))}
          </span>
        </div>
      ))}
    </div>
  );
}
