"use client";

/**
 * Style tab of the widget editor: title plus the widget-type-specific
 * knobs (KPI compare-to-prior, Pie top-N, Area/StackedBar stack toggle).
 * The stacked branch keeps ConfigRail's exact label split ("Stack mode"
 * for stacked_bar vs "Stack series" for area), the default split
 * (``stacked !== false`` vs ``Boolean(stacked)``), and the shared
 * ``aria-label="Stack series"``. Mutations come from ``useWidgetMutations``.
 */
import Section from "@/components/reports/config/Section";
import { useWidgetMutations } from "@/components/reports/config/useWidgetMutations";
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
  const { setTitle, setComparePrior, setTopN, setStacked } = useWidgetMutations(
    widget,
    onUpdate,
  );

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

      {(widget.type === "area" || widget.type === "stacked_bar") && (
        <Section label={widget.type === "stacked_bar" ? "Stack mode" : "Stack series"}>
          <label className="flex items-center gap-2 text-sm text-text-primary">
            <input
              type="checkbox"
              checked={
                widget.type === "stacked_bar"
                  ? (widget.config as StackedBarConfig).stacked !== false
                  : Boolean((widget.config as AreaConfig).stacked)
              }
              onChange={(e) => setStacked(e.target.checked)}
              aria-label="Stack series"
            />
            <span>Stack multiple series</span>
          </label>
        </Section>
      )}
    </>
  );
}
