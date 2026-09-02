"use client";

/**
 * The widget-editor mutation closures, extracted verbatim from the original
 * widget config rail into one shared plain factory so ``DataTab`` /
 * ``StyleTab`` / the popover all call the identical logic. It calls no React
 * hooks (callers invoke it unconditionally at render), so it is named
 * ``build*`` rather than ``use*`` to keep the rules-of-hooks linter off a
 * non-hook. Each setter early-returns on the same type guards it enforced
 * inline (these guards are load-bearing — e.g. ``setSingleMeasure``
 * early-returns on ``isMultiSeries`` and ``setSecondaryDimension``
 * early-returns on ``kpi`` / ``line`` / ``area`` / ``isSingleAggLocked``).
 */
import {
  isMultiSeries,
  isSingleAggLocked,
} from "@/components/reports/config/controlConstants";
import { asTxnTypeArray, pruneFiltersToSource } from "@/lib/reports/resolve";
import type {
  AreaConfig,
  BarConfig,
  Dataset,
  Dimension,
  KPIConfig,
  LineConfig,
  Measure,
  PieConfig,
  SeriesConfig,
  SourceCatalogEntry,
  SparklineConfig,
  StackedBarConfig,
  TableConfig,
  Widget,
  WidgetFilters,
} from "@/lib/reports/types";


/**
 * TBD-381: `resolveFormat` lived here and is GONE. Format is no longer written
 * at mutation time -- it derives at render from the catalog
 * (`lib/reports/widget-format.ts`), so there is nothing to keep in sync here.
 *
 * The comment it carried ("MATCH ON FIELD ONLY, do NOT add the agg conjunct")
 * described a mutation-time hazard: on a lookup miss the resolver had to
 * PRESERVE a stale previous format. At render there is no previous value, so
 * the exact (agg, field) pair is matched instead -- which is also what makes a
 * legacy `count(amount)` render as a count rather than as currency.
 */

export function buildWidgetMutations(
  widget: Widget,
  onUpdate: (next: Widget) => void,
) {
  // TBD-381: the optional `entry` parameter is GONE. It existed solely to feed
  // `resolveFormat`, which derived format at mutation time; format now derives
  // at render. `setDataset` still takes its own `entry` argument -- that one is
  // live and is what prunes filters and snaps the measure on a source switch.
  function setTitle(title: string) {
    onUpdate({ ...widget, title });
  }

  function setFilters(nextFilters: WidgetFilters) {
    const next = {
      ...widget,
      config: { ...widget.config, filters: nextFilters },
    } as Widget;
    onUpdate(next);
  }

  function setSingleMeasure(measure: Measure) {
    if (isMultiSeries(widget)) return;
    const cfg = widget.config as
      | KPIConfig
      | BarConfig
      | PieConfig
      | SparklineConfig;
    // in the same picker would otherwise keep "percent" and render
    // €1,234.56 as "1234.6%".
    const next = {
      ...widget,
      config: {
        ...cfg,
        measure,
      },
    } as Widget;
    onUpdate(next);
  }

  function setSeries(measures: SeriesConfig[]) {
    if (!isMultiSeries(widget)) return;
    const next: Widget = {
      ...widget,
      config: { ...widget.config, measures },
    } as Widget;
    onUpdate(next);
  }

  function setPrimaryDimension(dim: Dimension) {
    if (widget.type === "kpi") return; // KPI has no dimensions
    const cfg = widget.config as
      | BarConfig
      | LineConfig
      | AreaConfig
      | PieConfig
      | SparklineConfig
      | StackedBarConfig
      | TableConfig;
    const dims = [...(cfg.dimensions ?? [])];
    dims[0] = dim;
    const next: Widget = {
      ...widget,
      config: { ...cfg, dimensions: dims },
    } as Widget;
    onUpdate(next);
  }

  function setSecondaryDimension(dim: Dimension | "") {
    // ⚠ TBD-486: `line` and `area` refuse too. Both render through
    // `mergeSeriesRows`, which merges on `dimensions[0]` and ASSIGNS, so a
    // second dimension makes every bucket report its LAST pair's value:
    // one arbitrary category's number, labelled as the whole month's. The
    // renderers show an explicit unsupported state for a config already
    // persisted in that shape (`SECOND_DIMENSION_UNSUPPORTED_NOTICE`); this
    // stops the editor authoring a new one.
    //
    // `DataTab` also gates the control out for both types, so today this is
    // the second lock on the same door. Keep it: `buildWidgetMutations` is
    // called by the popover and by both tabs, and the cast below USED to name
    // `LineConfig | AreaConfig`, which is how the write was persuaded to
    // type-check in the first place. Those two members are now gone from it,
    // so the cast can no longer claim a line or area config is writable here.
    //
    // ⚠ Drawing a two-dimension line / area is TBD-383, not this guard.
    if (
      widget.type === "kpi" ||
      widget.type === "line" ||
      widget.type === "area" ||
      isSingleAggLocked(widget)
    ) {
      return;
    }
    const cfg = widget.config as
      | BarConfig
      | StackedBarConfig
      | TableConfig;
    const dims = [...(cfg.dimensions ?? [])];
    if (dim === "") {
      dims.splice(1, 1);
    } else {
      dims[1] = dim;
    }
    const next: Widget = {
      ...widget,
      config: { ...cfg, dimensions: dims },
    } as Widget;
    onUpdate(next);
  }

  /**
   * Switches the widget's data source, resetting both measure(s) and
   * dimensions that the new source doesn't carry. ``entry`` is the
   * SELECTED source's catalog; a measure field or dimension not published
   * by it would 422 at query time against the backend ``validate()``.
   *
   * - Measure: if the current measure's field isn't one the new source
   *   publishes, reset to the source's FIRST measure (its agg + field), so
   *   e.g. transactions→accounts defaults to ``sum_balance``
   *   ({agg:"sum", field:"balance"}). Multi-series widgets collapse to a
   *   single series carrying that first measure.
   * - Dimensions: keep the ones the new source carries (in order); drop
   *   the rest, refilling the primary slot with the source's first
   *   dimension key. KPI widgets carry no dimensions.
   * - Per-widget filters: prune ``config.filters`` to only the filter
   *   fields the new source publishes (``pruneFiltersToSource``). A
   *   leftover ``category_ids`` / ``txn_type`` / date filter from a
   *   transactions widget would otherwise 422 against the accounts
   *   source's ``validate()`` at the next query.
   */
  function setDataset(dataset: Dataset, entry: SourceCatalogEntry) {
    const measureFields = new Set(entry.measures.map((m) => m.field));
    const firstMeasure = entry.measures[0];
    // Filter fields the NEW source publishes. ``config.filters`` is
    // pruned to these so no stale per-widget filter survives a switch.
    const publishedFilterFields = entry.filters.map((f) => f.field);

    /**
     * Prune the per-widget filters to the new source's published FIELDS,
     * then normalize any stale enum VALUE that survives the field prune
     * but is invalid for the new source. ``pruneFiltersToSource`` only
     * drops fields; it never inspects values. The ``recurring`` source
     * DOES publish a ``txn_type`` filter, so a stale ``txn_type:"transfer"``
     * survives a transactions→recurring switch — but ``transfer`` is a
     * transactions-only concept (recurring is income/expense only) and the
     * backend ``RecurringSource.validate()`` 422s it. Strip it here so the
     * widget never silently fails to render. ``txn_type`` is the only
     * enum-valued filter today, so a targeted strip suffices.
     */
    const finalizeFilters = (
      filters: WidgetFilters | undefined,
    ): WidgetFilters | undefined => {
      const pruned = pruneFiltersToSource(filters, publishedFilterFields);
      if (!pruned) return undefined;
      if (dataset !== "transactions") {
        // ``transfer`` is transactions-only; drop just that member from
        // the multi-select array (keep any income/expense), and drop the
        // whole key only when nothing valid remains.
        const kept = asTxnTypeArray(pruned.txn_type)?.filter(
          (t) => t !== "transfer",
        );
        if (kept && kept.length > 0) {
          return { ...pruned, txn_type: kept };
        }
        if (pruned.txn_type !== undefined) {
          const { txn_type: _drop, ...rest } = pruned;
          return Object.keys(rest).length > 0 ? rest : undefined;
        }
      }
      return pruned;
    };
    // First valid measure for this source (default after a field-invalid
    // switch). ``entry.measures`` is always non-empty for a real source;
    // guard for the degenerate empty-catalog case so we never write an
    // undefined measure.
    const resetMeasure: Measure | undefined = firstMeasure
      ? {
          agg: firstMeasure.agg as Measure["agg"],
          field: firstMeasure.field as Measure["field"],
        }
      : undefined;

    if (widget.type === "kpi") {
      const cfg = widget.config as KPIConfig;
      const measure =
        resetMeasure && !measureFields.has(cfg.measure.field)
          ? resetMeasure
          : cfg.measure;
      const filters = finalizeFilters(cfg.filters);
      // Derive from the RESULTING measure, not entry.measures[0]: `id` is
      // published by every source, so a retained count(id) survives the switch
      const next: Widget = {
        ...widget,
        config: { ...cfg, dataset, measure, filters },
      };
      onUpdate(next);
      return;
    }
    const cfg = widget.config as
      | BarConfig
      | LineConfig
      | AreaConfig
      | PieConfig
      | SparklineConfig
      | StackedBarConfig
      | TableConfig;
    const valid = new Set(entry.dimensions.map((d) => d.key));
    const fallback = entry.dimensions[0]?.key as Dimension | undefined;
    // Keep dimensions the new source carries, in order; drop the rest.
    let dims = (cfg.dimensions ?? []).filter((d) => valid.has(d));
    // The primary slot must always be filled with a valid dimension.
    if (dims.length === 0 && fallback) {
      dims = [fallback];
    }

    if (isMultiSeries(widget)) {
      const mcfg = cfg as LineConfig | AreaConfig | StackedBarConfig | TableConfig;
      // Reset measures when ANY series references a field the new source
      // doesn't publish. Collapse to a single series carrying the source's
      // first measure (simplest fully-valid reset).
      const allValid = mcfg.measures.every((s) =>
        measureFields.has(s.measure.field),
      );
      const measures: SeriesConfig[] =
        allValid || !resetMeasure
          ? mcfg.measures
          : [{ measure: resetMeasure }];
      const filters = finalizeFilters(mcfg.filters);
      const next: Widget = {
        ...widget,
        config: { ...mcfg, dataset, dimensions: dims, measures, filters },
      } as Widget;
      onUpdate(next);
      return;
    }

    const scfg = cfg as BarConfig | PieConfig | SparklineConfig;
    const measure =
      resetMeasure && !measureFields.has(scfg.measure.field)
        ? resetMeasure
        : scfg.measure;
    const filters = finalizeFilters(scfg.filters);
    const next: Widget = {
      ...widget,
      config: { ...scfg, dataset, dimensions: dims, measure, filters },
    } as Widget;
    onUpdate(next);
  }

  function setComparePrior(value: boolean) {
    if (widget.type !== "kpi") return;
    const next: Widget = {
      ...widget,
      config: {
        ...(widget.config as KPIConfig),
        compare_prior_period: value,
      },
    };
    onUpdate(next);
  }

  function setTopN(value: number) {
    if (widget.type !== "pie") return;
    const next: Widget = {
      ...widget,
      config: { ...(widget.config as PieConfig), top_n: value },
    };
    onUpdate(next);
  }

  function setStacked(value: boolean) {
    if (widget.type !== "area" && widget.type !== "stacked_bar") return;
    const next: Widget = {
      ...widget,
      config: {
        ...(widget.config as AreaConfig | StackedBarConfig),
        stacked: value,
      },
    } as Widget;
    onUpdate(next);
  }

  return {
    setTitle,
    setFilters,
    setSingleMeasure,
    setSeries,
    setPrimaryDimension,
    setSecondaryDimension,
    setDataset,
    setComparePrior,
    setTopN,
    setStacked,
  };
}
