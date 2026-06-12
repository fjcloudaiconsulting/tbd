"use client";

/**
 * The widget-editor mutation closures, extracted verbatim from
 * ``ConfigRail`` into one shared plain factory so ``DataTab`` / ``StyleTab``
 * / the popover and (during the extraction PR) ``ConfigRail`` itself all
 * call the identical logic. It calls no React hooks (callers invoke it
 * unconditionally at render), so it is named ``build*`` rather than
 * ``use*`` to keep the rules-of-hooks linter off a non-hook. Each setter
 * early-returns on the same type guards it did
 * inline (these guards are load-bearing — e.g. ``setSingleMeasure``
 * early-returns on ``isMultiSeries`` and ``setSecondaryDimension``
 * early-returns on ``kpi`` / ``isSingleAggLocked``).
 */
import {
  isMultiSeries,
  isSingleAggLocked,
} from "@/components/reports/config/controlConstants";
import type {
  AreaConfig,
  BarConfig,
  Dimension,
  KPIConfig,
  LineConfig,
  Measure,
  PieConfig,
  SeriesConfig,
  SparklineConfig,
  StackedBarConfig,
  TableConfig,
  Widget,
  WidgetFilters,
} from "@/lib/reports/types";

export function buildWidgetMutations(
  widget: Widget,
  onUpdate: (next: Widget) => void,
) {
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
    const next = {
      ...widget,
      config: {
        ...(widget.config as KPIConfig | BarConfig | PieConfig | SparklineConfig),
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
    if (widget.type === "kpi" || isSingleAggLocked(widget)) return;
    const cfg = widget.config as
      | BarConfig
      | LineConfig
      | AreaConfig
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
    setComparePrior,
    setTopN,
    setStacked,
  };
}
