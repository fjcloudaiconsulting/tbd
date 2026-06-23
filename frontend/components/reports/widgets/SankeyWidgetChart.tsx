"use client";

/**
 * Nivo ResponsiveSankey inner for SankeyWidget. Split out so @nivo/sankey is
 * code-split (ssr:false) only when a chart mounts — the single dynamic()
 * boundary lives in SankeyWidget.tsx; here we import ResponsiveSankey
 * statically (matching BarWidgetChart / LineWidgetChart).
 *
 * Conversion: the backend returns ``SankeyLink[]`` (source/target/value).
 * Nivo expects ``{ nodes: [{id}], links: [{source, target, value}] }``. We
 * derive unique node ids from the union of all source and target strings so
 * Nivo never sees an undeclared node id.
 *
 * Hub/sentinel ids: the backend may return hub sentinel ids
 * (``__hub_income__``, ``__hub_savings__``, ``__hub_other__``) instead of
 * the display strings "Income" / "Savings" / "Other". The Nivo node ``id``
 * MUST stay the sentinel so link resolution works, but the displayed label
 * must be the friendly text — achieved via the ``label`` accessor.
 *
 * Colors: ``SANKEY_COLORS`` — stable module-level reference so Nivo's
 * ordinal-scale memo is not defeated by a new array every render.
 */
import { useMemo } from "react";
import { ResponsiveSankey } from "@nivo/sankey";

import { CHART_SERIES } from "@/lib/chart-colors";
import { formatMeasureValue } from "@/lib/reports/series";
import type { SankeyLink } from "@/lib/reports/types";

// Nivo's sankey types
interface NivoNode {
  id: string;
}
interface NivoLink {
  source: string;
  target: string;
  value: number;
}
interface NivoSankeyData {
  nodes: NivoNode[];
  links: NivoLink[];
}

/**
 * Hub/sentinel id → friendly display label mapping.
 * Real category node ids are not present here; their label === id.
 */
export const HUB_LABELS: Record<string, string> = {
  __hub_income__: "Income",
  __hub_savings__: "Savings",
  __hub_other__: "Other",
};

/** Stable color array ref — defeats Nivo's ordinal-scale memo when spread
 *  as a new array each render. */
const SANKEY_COLORS = [...CHART_SERIES];

function buildNivoData(links: SankeyLink[]): NivoSankeyData {
  // Collect unique node ids from the union of all source and target values,
  // preserving first-seen order so the layout is deterministic across renders.
  const seen = new Set<string>();
  const nodes: NivoNode[] = [];
  for (const { source, target } of links) {
    if (!seen.has(source)) {
      seen.add(source);
      nodes.push({ id: source });
    }
    if (!seen.has(target)) {
      seen.add(target);
      nodes.push({ id: target });
    }
  }
  return {
    nodes,
    links: links.map(({ source, target, value }) => ({ source, target, value })),
  };
}

export interface SankeyWidgetChartProps {
  links: SankeyLink[];
  currency?: string;
  title?: string;
}

export default function SankeyWidgetChart({ links, currency, title }: SankeyWidgetChartProps) {
  const data = useMemo(() => buildNivoData(links), [links]);

  return (
    <ResponsiveSankey
      data={data}
      colors={SANKEY_COLORS}
      margin={{ top: 8, right: 80, bottom: 8, left: 80 }}
      nodeOpacity={1}
      nodeHoverOpacity={1}
      nodeThickness={18}
      nodeSpacing={10}
      nodeBorderWidth={0}
      linkOpacity={0.35}
      linkHoverOpacity={0.6}
      linkContract={1}
      // Link ribbons already default to source-node color (Nivo sets
      // link.color = link.source.color internally). Keeping
      // enableLinkGradient={false} (the default) preserves that solid
      // source-color fill without a gradient.
      enableLinkGradient={false}
      enableLabels={true}
      labelPosition="outside"
      labelOrientation="horizontal"
      labelPadding={12}
      // Map hub sentinel ids to friendly display labels; real category ids
      // pass through unchanged (HUB_LABELS[id] is undefined → fall back to id).
      label={(node) => HUB_LABELS[node.id] ?? node.id}
      // labelTextColor is the authoritative knob Nivo uses for outside node
      // labels (via getLabelTextColor). A plain string is a valid
      // InheritedColorConfigStaticColor; CSS vars resolve in SVG fill.
      labelTextColor="var(--color-text-primary)"
      theme={{
        text: { fill: "var(--color-text-primary)", fontSize: 11 },
        labels: { text: { fill: "var(--color-text-primary)", fontSize: 11 } },
        tooltip: {
          container: {
            background: "var(--color-surface)",
            color: "var(--color-text-primary)",
            fontSize: 12,
            border: "1px solid var(--color-border)",
          },
        },
      }}
      // Currency-formatted node tooltip: shows friendly hub label + amount.
      nodeTooltip={({ node }) => (
        <div
          style={{
            background: "var(--color-surface)",
            color: "var(--color-text-primary)",
            border: "1px solid var(--color-border)",
            borderRadius: 4,
            padding: "6px 10px",
            fontSize: 12,
          }}
        >
          <strong>{HUB_LABELS[node.id] ?? node.id}</strong>
          {": "}
          {formatMeasureValue(node.value, "currency", currency)}
        </div>
      )}
      // Currency-formatted link tooltip: shows source→target + amount.
      linkTooltip={({ link }) => (
        <div
          style={{
            background: "var(--color-surface)",
            color: "var(--color-text-primary)",
            border: "1px solid var(--color-border)",
            borderRadius: 4,
            padding: "6px 10px",
            fontSize: 12,
          }}
        >
          <strong>{HUB_LABELS[link.source.id] ?? link.source.id}</strong>
          {" → "}
          <strong>{HUB_LABELS[link.target.id] ?? link.target.id}</strong>
          {": "}
          {formatMeasureValue(link.value, "currency", currency)}
        </div>
      )}
      // Disable animation for consistency with every other widget.
      animate={false}
      ariaLabel={title ?? "Cash flow Sankey chart"}
    />
  );
}
