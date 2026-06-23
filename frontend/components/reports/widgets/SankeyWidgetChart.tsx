"use client";

/**
 * Nivo ResponsiveSankey inner for SankeyWidget. Split out so @nivo/sankey is
 * dynamically imported (ssr:false) only when a chart mounts — consistent with
 * how the other widgets code-split their chart engines via next/dynamic.
 *
 * Conversion: the backend returns ``SankeyLink[]`` (source/target/value).
 * Nivo expects ``{ nodes: [{id}], links: [{source, target, value}] }``. We
 * derive unique node ids from the union of all source and target strings so
 * Nivo never sees an undeclared node id.
 *
 * Colors: ``CHART_SERIES`` — the same categorical palette every other
 * report widget uses, so the Sankey's node colors register with the rest
 * of the canvas.
 */
import { useMemo } from "react";
import dynamic from "next/dynamic";

import { CHART_SERIES } from "@/lib/chart-colors";
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

// Dynamic import with ssr:false so Nivo never tries to run in Node/jsdom
// (it depends on browser-only layout APIs).
const ResponsiveSankey = dynamic(
  () => import("@nivo/sankey").then((m) => m.ResponsiveSankey),
  {
    ssr: false,
    loading: () => (
      <div
        data-testid="sankey-widget-chart-loading"
        className="h-full w-full animate-pulse rounded bg-border/40"
      />
    ),
  },
);

export interface SankeyWidgetChartProps {
  links: SankeyLink[];
}

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

export default function SankeyWidgetChart({ links }: SankeyWidgetChartProps) {
  const data = useMemo(() => buildNivoData(links), [links]);

  return (
    <ResponsiveSankey
      data={data}
      colors={[...CHART_SERIES]}
      margin={{ top: 8, right: 16, bottom: 8, left: 16 }}
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
      animate={true}
      motionConfig="gentle"
    />
  );
}
