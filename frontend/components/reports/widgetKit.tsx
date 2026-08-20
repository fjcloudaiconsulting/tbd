"use client";

/**
 * Reports v2 — shared widget kit.
 *
 * Holds the pieces that both the saved-report editor (`/reports/[id]`)
 * and the unsaved-draft editor (`/reports/new`) need: the per-type
 * "empty widget" factory, the type → component renderer, a widget-id
 * minter, and the mobile stack ordering. Extracted so the draft editor
 * can reuse them without duplicating the editor's body or threading a
 * "no id" mode through the big saved-report page.
 */
import type { CanvasFilters, Widget, WidgetType } from "@/lib/reports/types";
import type { BarConfig, KPIConfig, SankeyConfig } from "@/lib/reports/types";
import KPIWidget from "@/components/reports/widgets/KPIWidget";
import BarWidget from "@/components/reports/widgets/BarWidget";
import LineWidget from "@/components/reports/widgets/LineWidget";
import AreaWidget from "@/components/reports/widgets/AreaWidget";
import PieWidget from "@/components/reports/widgets/PieWidget";
import SparklineWidget from "@/components/reports/widgets/SparklineWidget";
import TableWidget from "@/components/reports/widgets/TableWidget";
import SankeyWidget from "@/components/reports/widgets/SankeyWidget";

function emptyKPI(id: string): Widget {
  const config: KPIConfig = {
    dataset: "transactions",
    measure: { agg: "sum", field: "amount" },
    compare_prior_period: false,
  };
  return {
    id,
    type: "kpi",
    title: "New KPI",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config,
  };
}

function emptyBar(id: string): Widget {
  const config: BarConfig = {
    dataset: "transactions",
    measure: { agg: "sum", field: "amount" },
    dimensions: ["category"],
    sort: { by: "value", dir: "desc" },
    limit: 10,
  };
  return {
    id,
    type: "bar",
    title: "New bar chart",
    grid: { x: 0, y: 0, w: 6, h: 4 },
    config,
  };
}

function emptyMultiSeries(
  id: string,
  type: "line" | "area" | "stacked_bar" | "table",
): Widget {
  const isTable = type === "table";
  // ⚠ TBD-382 R13: a TIME primary is never seeded in spend order. Under R3
  // branch 3 a `sort: {by:"value"}` on a `month` primary would rank the
  // client-side primary cap by total and render every newly created chart's
  // month axis in spend order — the arrangement R9 calls out as never the
  // intended reading of a time axis. `table` keeps value-desc: its primary is
  // `category`, where spend order IS the intended reading.
  const baseConfig = {
    dataset: "transactions" as const,
    measures: [{ measure: { agg: "sum" as const, field: "amount" as const } }],
    dimensions: [isTable ? ("category" as const) : ("month" as const)],
    sort: isTable
      ? { by: "value" as const, dir: "desc" as const }
      : { by: "dimension" as const, dir: "asc" as const },
    limit: isTable ? 50 : 100,
  };
  const baseGrid = isTable
    ? { x: 0, y: 0, w: 12, h: 6 }
    : { x: 0, y: 0, w: 6, h: 4 };
  return {
    id,
    type,
    title:
      type === "line"
        ? "New line chart"
        : type === "area"
          ? "New area chart"
          : type === "stacked_bar"
            ? "New stacked bar chart"
            : "New table",
    grid: baseGrid,
    config: baseConfig,
  } as Widget;
}

function emptyPie(id: string): Widget {
  return {
    id,
    type: "pie",
    title: "New pie chart",
    grid: { x: 0, y: 0, w: 4, h: 4 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["category"],
      sort: { by: "value", dir: "desc" },
      limit: 50,
      top_n: 8,
    },
  };
}

function emptySparkline(id: string): Widget {
  return {
    id,
    type: "sparkline",
    title: "New sparkline",
    grid: { x: 0, y: 0, w: 3, h: 2 },
    config: {
      dataset: "transactions",
      measure: { agg: "sum", field: "amount" },
      dimensions: ["month"],
      sort: { by: "dimension", dir: "asc" },
      limit: 50,
    },
  };
}

function emptySankey(id: string): Widget {
  const config: SankeyConfig = {
    dataset: "transactions",
    measure: { agg: "sum", field: "amount" },
    spending_granularity: "category",
  };
  return {
    id,
    type: "sankey",
    title: "Cash flow",
    grid: { x: 0, y: 0, w: 8, h: 5 },
    config,
  };
}

export function emptyWidget(type: WidgetType, id: string): Widget {
  switch (type) {
    case "kpi":
      return emptyKPI(id);
    case "bar":
      return emptyBar(id);
    case "line":
      return emptyMultiSeries(id, "line");
    case "area":
      return emptyMultiSeries(id, "area");
    case "stacked_bar":
      return emptyMultiSeries(id, "stacked_bar");
    case "table":
      return emptyMultiSeries(id, "table");
    case "pie":
      return emptyPie(id);
    case "sparkline":
      return emptySparkline(id);
    case "sankey":
      return emptySankey(id);
  }
}

export function renderWidgetByType(
  w: Widget,
  canvasFilters: CanvasFilters,
  editMode: boolean,
) {
  switch (w.type) {
    case "kpi":
      return (
        <KPIWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} />
      );
    case "bar":
      return (
        <BarWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} />
      );
    case "line":
      return (
        <LineWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} />
      );
    case "area":
      return (
        <AreaWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} />
      );
    case "pie":
      return (
        <PieWidget widget={w} canvasFilters={canvasFilters} editMode={editMode} />
      );
    case "sparkline":
      return (
        <SparklineWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
        />
      );
    // TBD-382: stacked_bar renders through BarWidget. It lost its
    // measure-stacking axis, so it is a bar chart whose break-down by
    // dimensions[1] stacks (or, with config.stacked === false, groups).
    case "stacked_bar":
      return (
        <BarWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
        />
      );
    case "table":
      return (
        <TableWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
        />
      );
    case "sankey":
      return (
        <SankeyWidget
          widget={w}
          canvasFilters={canvasFilters}
          editMode={editMode}
        />
      );
  }
}

export function newWidgetId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `w_${crypto.randomUUID().slice(0, 8)}`;
  }
  return `w_${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Order widgets for the mobile single-column stack: top-to-bottom by
 * grid ``y``, then left-to-right by grid ``x`` so the vertical reading
 * order matches what the desktop grid shows.
 */
export function orderWidgetsForStack(widgets: Widget[]): Widget[] {
  return [...widgets].sort((a, b) => {
    if (a.grid.y !== b.grid.y) return a.grid.y - b.grid.y;
    return a.grid.x - b.grid.x;
  });
}
