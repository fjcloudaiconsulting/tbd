"use client";

/**
 * Style tab of the widget editor: title plus the widget-type-specific
 * knobs (KPI compare-to-prior, Pie top-N, Area stack toggle, StackedBar
 * bar layout).
 *
 * TBD-382 split the once-shared area/stacked_bar branch in two. They no
 * longer describe the same thing: ``area`` still stacks N MEASURES, while
 * a ``stacked_bar`` now carries exactly one measure and the flag flips its
 * SECONDARY-DIMENSION break-down between stacked and side by side. The
 * defaults still differ (``Boolean(stacked)`` for area vs
 * ``stacked !== false`` for stacked_bar), and the stacked arm is gated on
 * the break-down existing.
 *
 * The shared ``aria-label="Stack series"`` is GONE, deliberately: it did
 * not contain either arm's visible text, so the accessible name failed
 * WCAG 2.5.3 Label in Name (Level A) and a voice-control user saying the
 * words on screen missed the control. Each ``<input>`` is already wrapped
 * in its ``<label>``, so the visible text IS the accessible name.
 *
 * Mutations come from ``buildWidgetMutations``.
 */
import Section from "@/components/reports/config/Section";
import { buildWidgetMutations } from "@/components/reports/config/useWidgetMutations";
import type {
  AreaConfig,
  KPIConfig,
  PieConfig,
  StackedBarConfig,
  Widget,
} from "@/lib/reports/types";

export default function StyleTab({
  widget,
  onUpdate,
}: {
  widget: Widget;
  onUpdate: (next: Widget) => void;
}) {
  const { setTitle, setComparePrior, setTopN, setStacked } =
    buildWidgetMutations(widget, onUpdate);

  return (
    <>
      <Section label="Title">
        <input
          type="text"
          value={widget.title}
          onChange={(e) => setTitle(e.target.value)}
          aria-label="Widget title"
          className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
        />
      </Section>

      {widget.type === "kpi" && (
        <Section label="Compare to prior period">
          <label className="flex items-center gap-2 text-sm text-text-primary">
            <input
              type="checkbox"
              checked={Boolean(
                (widget.config as KPIConfig).compare_prior_period,
              )}
              onChange={(e) => setComparePrior(e.target.checked)}
              aria-label="Compare to prior period"
            />
            <span>Show delta vs prior period</span>
          </label>
        </Section>
      )}

      {widget.type === "pie" && (
        <Section label="Top N slices">
          <input
            type="number"
            min={2}
            max={20}
            value={(widget.config as PieConfig).top_n ?? 8}
            onChange={(e) => setTopN(Number(e.target.value) || 8)}
            aria-label="Top N slices"
            className="w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
          />
        </Section>
      )}

      {widget.type === "area" && (
        <Section label="Stack series">
          <label className="flex items-center gap-2 text-sm text-text-primary">
            <input
              type="checkbox"
              checked={Boolean((widget.config as AreaConfig).stacked)}
              onChange={(e) => setStacked(e.target.checked)}
            />
            <span>Stack multiple series</span>
          </label>
        </Section>
      )}

      {/* TBD-382: a stacked_bar now carries exactly ONE measure, so this no
          longer stacks "series" -- it flips the SECONDARY-DIMENSION break-down
          between stacked and side by side. Gated on that break-down existing:
          with no dimensions[1] the flag is inert (BarWidgetChart ignores
          `stacked` when `sliced` is false), and a control describing a mode the
          widget does not have is the same false assertion this ticket removed
          from the chart. Subtractive, per FilterEditor's TBD-381 rule. */}
      {widget.type === "stacked_bar" &&
        Boolean(((widget.config as StackedBarConfig).dimensions ?? [])[1]) && (
          <Section label="Bar layout" help="reports.bar-layout">
            <label className="flex items-center gap-2 text-sm text-text-primary">
              <input
                type="checkbox"
                checked={(widget.config as StackedBarConfig).stacked !== false}
                onChange={(e) => setStacked(e.target.checked)}
              />
              <span>Stack the break-down into one bar</span>
            </label>
          </Section>
        )}
    </>
  );
}
