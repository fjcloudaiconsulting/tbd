"use client";

/**
 * KPI widget — single big number with an optional delta vs the
 * prior period. Pulls its rows through ``useReportQuery``; failure
 * renders an inline error inside this card and does not bubble to
 * sibling widgets.
 *
 * ⚠⚠ THE DELTA IS COMPUTED HERE, FROM THIS WIDGET'S OWN CONFIG (TBD-383).
 * It used to arrive as a ``priorValue`` prop that no production caller ever
 * passed — not ``renderReportWidget``, not ``widgetKit.renderWidgetByType``,
 * not the dashboard — so ``compare_prior_period`` wrote a flag nothing read
 * and the delta never rendered anywhere, for the life of the widget, while
 * its tests stayed green by injecting the prop. Do not reintroduce an
 * injected prior value: a render branch fed from outside can be forgotten by
 * a caller, and this one was.
 *
 * Visual register intentionally minimal — a label, a value, an
 * optional delta. Live in the canvas grid; the widget shell (drag
 * handle, title bar) wraps it.
 */
import {
  readMeasureValue,
  useComparisonQuery,
  useReportQuery,
} from "@/lib/reports/useReportQuery";
import { widgetDataState } from "@/lib/reports/notices";
import { formatMeasureValue } from "@/lib/reports/series";
import { useWidgetFormat } from "@/lib/reports/widget-format";
import type { CanvasFilters, KPIWidget as KPIWidgetType } from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import WidgetNotices from "@/components/reports/WidgetNotices";
import type { CsvCell } from "@/lib/reports/csv";

interface Props {
  widget: KPIWidgetType;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

export default function KPIWidget({
  widget,
  canvasFilters,
  editMode,
  currency,
}: Props) {
  const { data, error, isLoading: dataLoading } = useReportQuery(widget, canvasFilters);
  // The comparison is this widget's own second query over the same AST with
  // the resolved date window shifted back one whole length. It issues NO
  // request when `compare_prior_period` is off or the window is not
  // shiftable (unbounded, half-open, or a server-resolved relative token).
  const { prior } = useComparisonQuery(widget, canvasFilters);

  const value = readMeasureValue(data?.rows[0]);
  // TBD-381: derived from the source catalog at render, never read from
  // config. `format` is no longer persisted -- see lib/reports/widget-format.ts.
  const { format: derivedFormat, isLoading: catalogLoading } = useWidgetFormat(widget.config.dataset, [widget.config.measure]);
  // Hold the skeleton until the catalog resolves: rendering an
  // unformatted value that then flips is worse than one more frame of
  // skeleton, and /query is in flight over the same window anyway.
  const isLoading = dataLoading || catalogLoading;
  const format = derivedFormat ?? "number";
  // A KPI with no delta is the ordinary state, not an error: no comparison
  // window, a still-loading comparison, or a prior of exactly zero all render
  // the value alone, silently.
  const showDelta =
    widget.config.compare_prior_period === true &&
    prior !== null &&
    value !== null;
  const delta =
    showDelta && value !== null && prior !== null && prior !== 0
      ? ((value - prior) / Math.abs(prior)) * 100
      : null;
  // ⚠ Direction is derived from the ROUNDED percentage, never the raw one, so
  // the glyph can never disagree with the digits printed beside it. Deriving
  // it from the raw delta renders "↑ 0.0%" at +0.02 — an arrow asserting a
  // movement the number says did not happen. `(-0.02).toFixed(1)` is the
  // string "-0.0"; rounding through `Number` collapses that to -0, which is
  // neither `> 0` nor `< 0`, so it takes the no-direction arm, and
  // `(-0).toFixed(1)` is "0.0" — no minus sign on a quantity of zero.
  const deltaRounded = delta === null ? null : Number(delta.toFixed(1));
  const deltaArrow =
    deltaRounded === null ? "" : deltaRounded > 0 ? "↑" : deltaRounded < 0 ? "↓" : "→";
  const deltaText =
    deltaRounded === null
      ? ""
      : deltaRounded > 0
        ? `+${deltaRounded.toFixed(1)}`
        : deltaRounded.toFixed(1);

  // CSV export: a single label/value row (the KPI is one number).
  const measureLabel = widget.config.measure.field;
  const csvDataset = {
    headers: [widget.title || "KPI", measureLabel],
    rows:
      value === null
        ? ([] as CsvCell[][])
        : ([[widget.title || "KPI", value]] as CsvCell[][]),
  };

  return (
    <div
      data-testid="kpi-widget"
      data-widget-id={widget.id}
      className="flex h-full flex-col justify-center gap-1 rounded-lg border border-border bg-surface p-4"
    >
      <div
        data-testid="widget-header"
        // ⚠ `pr-12` is not spacing taste. `WidgetShell`'s edit overlay is
        // absolutely positioned at `right-1 top-1` and occupies
        // x ∈ [W−52, W−4]; `WidgetCsvButton` renders `null` in edit mode,
        // so without this reservation the notice glyph lands at
        // x ∈ [W−42, W−16] and its top-right corner overlaps the REMOVE
        // control. See the geometry note in `WidgetNotices.tsx`.
        className={`flex items-center justify-between gap-2${
          editMode ? " pr-12" : ""
        }`}
      >
        <div className="flex min-w-0 flex-1 items-center gap-1">
          <span className="min-w-0 truncate text-[11px] font-medium uppercase tracking-wider text-text-muted">
            {widget.title || "KPI"}
          </span>
          {/* Quiet: a KPI renders ONE group's own aggregate. Truncation of
              a one-row result set does not make that number wrong. */}
          <WidgetNotices
            metas={[data?.meta]}
            derivesCrossRowAggregate={false}
            withholdsCrossRowAggregate={false}
            widgetTitle={widget.title || "KPI"}
            state={widgetDataState(isLoading, error, value !== null)}
          />
        </div>
        <WidgetCsvButton
          title={widget.title || "KPI"}
          dataset={csvDataset}
          editMode={editMode}
        />
      </div>
      {isLoading ? (
        <div
          data-testid="kpi-widget-loading"
          className="h-7 w-24 animate-pulse rounded bg-border"
        />
      ) : error ? (
        <div
          role="alert"
          data-testid="kpi-widget-error"
          className="text-sm text-danger"
        >
          Couldn&apos;t load
        </div>
      ) : value === null ? (
        <div className="text-2xl font-semibold text-text-muted">—</div>
      ) : (
        <>
          <div
            data-testid="kpi-widget-value"
            className="text-2xl font-semibold text-text-primary"
          >
            {formatMeasureValue(value, format, currency)}
          </div>
          {showDelta && delta !== null && (
            <div
              data-testid="kpi-widget-delta"
              // ⚠ NEUTRAL BY DESIGN (TBD-383) — do not colour this by sign.
              // Polarity is not derivable here: `sum(amount)` is signed, the
              // widget may or may not carry a `txn_type` filter, and nothing
              // in `(agg, field)` says whether up is good. Painting `delta >= 0`
              // as success WOULD have rendered "+12.3% vs prior period" on a
              // SPEND KPI in Settled Green — the app congratulating the user
              // for spending more. (Would have: the branch never reached a
              // user, which is the whole premise of this change.) Direction is
              // carried by the arrow and the sign, which
              // assert a fact; hue would assert a judgement the app cannot
              // compute. A `polarity` config knob was rejected: a new
              // persisted field whose default is wrong half the time.
              className="text-xs font-medium text-text-secondary"
            >
              <span aria-hidden="true">{deltaArrow}</span>{" "}
              {deltaText}% vs prior period
            </div>
          )}
        </>
      )}
    </div>
  );
}
