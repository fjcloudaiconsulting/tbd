"use client";

/**
 * Bar widget — vertical bars over a primary dimension. Pulls rows
 * through ``useReportQuery``; treats the first dimension as the x-axis
 * label and ``value`` as the measure axis.
 *
 * When a SECONDARY dimension is set (``config.dimensions[1]``, e.g.
 * "account" or "category"), each total bar is sliced into segments — one
 * per distinct secondary value, each a distinct color from the
 * categorical palette — with a legend mapping color → secondary value.
 * The backend AST supports up to two dimensions, so this is a single
 * query grouped by ``[primary, secondary]`` that we pivot and rank
 * client-side via ``lib/reports/breakdown``. With no secondary dimension
 * the widget keeps its original single-color behavior.
 *
 * TBD-382: this component now renders BOTH the ``bar`` and
 * ``stacked_bar`` widget types. ``stacked_bar`` lost its measure-stacking
 * axis — across all five report sources there is no pair of published
 * measures whose sum is meaningful, so a stack of measures always
 * asserted a part/whole relationship that does not exist — and its only
 * stacking axis is now the same secondary dimension ``bar`` already
 * breaks down by. The two types differ by exactly one boolean,
 * ``StackedBarConfig.stacked``, which flips the break-down between
 * stacked and grouped (side-by-side).
 *
 * ⚠ The two types persist their measure under DIFFERENT keys and that is
 * deliberate, not drift: ``bar`` writes ``config.measure`` while
 * ``stacked_bar`` writes ``config.measures`` — a length-1 array, bound to
 * the backend's ``_MultiSeriesConfig`` where ``measures`` carries
 * ``Field(min_length=1)`` (backend/app/schemas/report_layout.py) and is
 * shared with ``dashboard.py``. NEVER write a singular ``measure`` key
 * onto a stacked_bar config and never dual-write both:
 * ``validate_layout_json`` returns its input VERBATIM, so a stray key
 * would live in the DB forever as a second source of truth. Read through
 * ``barMeasure`` below.
 *
 * Recharts is the canvas chart engine across the app (Dashboard,
 * Budgets, Forecast Plans); reusing it here keeps visual register
 * consistent. The recharts subtree is code-split via ``next/dynamic``
 * (ssr:false) into ``BarWidgetChart`` so recharts loads only when a
 * chart mounts, keeping it out of the route's initial JS.
 */
import dynamic from "next/dynamic";
import { useMemo } from "react";

import { useReportQuery } from "@/lib/reports/useReportQuery";
import { buildBreakdown, EMPTY_BREAKDOWN } from "@/lib/reports/breakdown";
import { dimensionHeader, measureFieldLabel } from "@/lib/reports/series";
import { useWidgetFormat } from "@/lib/reports/widget-format";
import type {
  BarWidget as BarWidgetType,
  CanvasFilters,
  Measure,
  StackedBarWidget as StackedBarWidgetType,
} from "@/lib/reports/types";
import WidgetCsvButton from "./WidgetCsvButton";
import type { CsvCell } from "@/lib/reports/csv";

/** The two widget types this component renders. */
type BarLikeWidget = BarWidgetType | StackedBarWidgetType;

/**
 * ``next/dynamic``'s ``loading`` element is built at MODULE scope and is
 * handed no props, so it cannot read ``widget.type`` to pick a testid
 * prefix. Minting one wrapper per prefix keeps both placeholders
 * independently locatable; the import specifier stays a literal in each,
 * so the bundler still emits a single shared chunk.
 */
function dynamicBarChart(testidPrefix: string) {
  return dynamic(() => import("./BarWidgetChart"), {
    ssr: false,
    loading: () => (
      <div
        data-testid={`${testidPrefix}-chart-loading`}
        className="h-full w-full animate-pulse rounded bg-border/40"
      />
    ),
  });
}

const BarChartDynamic = dynamicBarChart("bar-widget");
const StackedBarChartDynamic = dynamicBarChart("stacked-bar-widget");

interface Props {
  widget: BarLikeWidget;
  canvasFilters?: CanvasFilters;
  editMode?: boolean;
  /** Org currency ISO code; prefixes the symbol when format is "currency". */
  currency?: string;
}

/**
 * Read the widget's single measure through its per-type key. Defensive on
 * the array index to match ``buildQueryAst``'s own fallback: the backend
 * guarantees ``min_length=1`` but a hand-built config in a test or an
 * older persisted layout need not. Legacy entries beyond index 0 are
 * ignored, never rewritten at render.
 */
function barMeasure(widget: BarLikeWidget): Measure {
  if (widget.type === "stacked_bar") {
    return widget.config.measures[0]?.measure ?? { agg: "sum", field: "amount" };
  }
  return widget.config.measure;
}

export default function BarWidget({
  widget,
  canvasFilters,
  editMode,
  currency,
}: Props) {
  const { data, error, isLoading: dataLoading } = useReportQuery(
    widget,
    canvasFilters,
  );

  const isStackedType = widget.type === "stacked_bar";
  // Testid prefix is parameterized off the widget type so both surfaces
  // stay independently locatable after the merge.
  const tid = isStackedType ? "stacked-bar-widget" : "bar-widget";
  const defaultTitle = isStackedType ? "Stacked bar chart" : "Bar chart";
  const title = widget.title || defaultTitle;
  const Chart = isStackedType ? StackedBarChartDynamic : BarChartDynamic;
  // ``stacked`` lives only on StackedBarConfig. A plain ``bar`` has no
  // grouped mode: its break-down has always stacked, and nothing in the
  // editor or the templates offers to turn that off. Hoisting the flag
  // onto BarConfig would hand ``bar`` a mode nobody asked for.
  const stacked = isStackedType ? widget.config.stacked !== false : true;

  const primaryKey = widget.config.dimensions[0] ?? "dimension";
  const secondaryKey = widget.config.dimensions[1];
  const sliced = Boolean(secondaryKey);
  const configLimit = widget.config.limit;
  const sort = widget.config.sort;

  // Wrap in useMemo so the `?? []` fallback doesn't mint a fresh array
  // every render, which would destabilize the simpleRows/breakdown memos.
  const queryRows = useMemo(() => data?.rows ?? [], [data]);

  // Single-series shape (no break-down): one ``value`` per label.
  // Memoized on the query rows + primary key so unrelated parent renders
  // don't rebuild the array reference and force Recharts to re-layout.
  const simpleRows = useMemo(
    () =>
      queryRows.map((r) => ({
        label: String(r[primaryKey] ?? "—"),
        value: typeof r.value === "number" ? r.value : Number(r.value ?? 0),
      })),
    [queryRows, primaryKey],
  );

  // Sliced shape: pivot [primary, secondary], cap the primaries, fold the
  // secondary tail into "Other", then colour from a stable label ordering.
  // Memoized like simpleRows so the O(n) pipeline doesn't rerun (and force
  // a Recharts re-layout) on unrelated parent renders.
  const breakdown = useMemo(
    () =>
      sliced
        ? buildBreakdown(queryRows, primaryKey, secondaryKey!, {
            limit: configLimit,
            sort,
          })
        : EMPTY_BREAKDOWN,
    [sliced, queryRows, primaryKey, secondaryKey, configLimit, sort],
  );

  const rows = sliced ? breakdown.rows : simpleRows;
  const hasRows = rows.length > 0;
  const measure = barMeasure(widget);
  // TBD-381: derived from the source catalog at render, never read from
  // config. `format` is no longer persisted -- see lib/reports/widget-format.ts.
  const { format: derivedFormat, isLoading: catalogLoading } = useWidgetFormat(
    widget.config.dataset,
    [measure],
  );
  // Hold the skeleton until the catalog resolves: rendering an
  // unformatted value that then flips is worse than one more frame of
  // skeleton, and /query is in flight over the same window anyway.
  const isLoading = dataLoading || catalogLoading;
  const format = derivedFormat ?? "number";

  const primaryHeader = dimensionHeader(primaryKey);
  const secondaryHeader = secondaryKey ? dimensionHeader(secondaryKey) : "";
  const measureLabel = measureFieldLabel(measure.field);

  // R11 — the chart had no text alternative at all, and the legend <ul>
  // had no accessible name and no relationship to it, so a screen-reader
  // user met a bare list of category names after nothing. (The old
  // StackedBarWidget put an aria-label on a role-less <div>, which is
  // invalid ARIA-in-HTML and silently a no-op.)
  const chartLabel = sliced
    ? `${title}: ${measureLabel} by ${primaryHeader}, broken down by ${secondaryHeader}`
    : `${title}: ${measureLabel} by ${primaryHeader}`;
  const legendLabel = `${secondaryHeader} break-down of ${measureLabel} by ${primaryHeader}`;

  // CSV export. Single-series: [dimension, measure]. Sliced: [primary
  // dimension, ...one column per secondary value] — the RAW, UNFOLDED
  // columns, so "Other" always has a drill path back to its rows.
  const csvDataset = sliced
    ? {
        headers: [primaryHeader, ...breakdown.csvValues],
        rows: breakdown.rows.map((r) => [
          String(r.label),
          ...breakdown.csvKeys.map((sk) =>
            typeof r[sk] === "number" ? (r[sk] as number) : 0,
          ),
        ]) as CsvCell[][],
      }
    : {
        headers: [primaryHeader, measureLabel],
        rows: simpleRows.map((r) => [r.label, r.value]) as CsvCell[][],
      };

  return (
    <div
      data-testid={tid}
      data-widget-id={widget.id}
      // R10 — `meta.truncated` is PLUMBED but not yet rendered. Surfacing
      // it is a new inline surface (a design change) and is deferred; this
      // keeps the follow-up a render change rather than a re-plumb, and
      // MAX_LIMIT 500 is not infinity.
      data-truncated={data?.meta?.truncated ? "true" : "false"}
      className="flex h-full flex-col rounded-lg border border-border bg-surface p-4"
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-sm font-semibold text-text-primary">{title}</div>
        <WidgetCsvButton title={title} dataset={csvDataset} editMode={editMode} />
      </div>
      <div className="flex-1">
        {isLoading ? (
          <div
            data-testid={`${tid}-loading`}
            className="h-full w-full animate-pulse rounded bg-border/40"
          />
        ) : error ? (
          <div
            role="alert"
            data-testid={`${tid}-error`}
            className="text-sm text-danger"
          >
            Couldn&apos;t load
          </div>
        ) : !hasRows ? (
          <div
            data-testid={`${tid}-empty`}
            className="flex h-full items-center justify-center text-sm text-text-muted"
          >
            No data
          </div>
        ) : (
          <div
            role="img"
            aria-label={chartLabel}
            data-testid={`${tid}-chart-region`}
            className="h-full w-full"
          >
            <Chart
              rows={rows}
              sliced={sliced}
              stacked={stacked}
              secondaryValues={breakdown.secondaryValues}
              seriesKeys={breakdown.seriesKeys}
              sliceColors={breakdown.sliceColors}
              valueName={measureLabel}
              format={format}
              currency={currency}
            />
          </div>
        )}
      </div>

      {/* DOM legend (outside the SVG) maps each color → secondary value.
          Rendered ourselves rather than via Recharts ``<Legend>`` so it
          stays visible in headless layouts (jsdom collapses the chart)
          and so swatch colors stay theme-token driven. */}
      {sliced && !isLoading && !error && hasRows && (
        <ul
          data-testid={`${tid}-legend`}
          aria-label={legendLabel}
          className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-text-secondary"
        >
          {breakdown.secondaryValues.map((sv, i) => (
            <li
              key={breakdown.seriesKeys[i]}
              data-testid={`${tid}-legend-item`}
              className="flex items-center gap-1"
            >
              <span
                data-testid={`${tid}-legend-swatch`}
                data-color={breakdown.sliceColors[i]}
                aria-hidden="true"
                // ring-1: the light-theme swatch/surface contrast measures
                // 3.13–3.30, passing with no margin at 10×10px unbordered,
                // so the swatch's shape is bounded independently of its fill.
                className="inline-block h-2.5 w-2.5 rounded-sm ring-1 ring-border"
                style={{ backgroundColor: breakdown.sliceColors[i] }}
              />
              <span>{sv}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
