import { render } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";

/**
 * SankeyWidgetChart renders Nivo ResponsiveSankey which depends on browser
 * layout APIs (ResizeObserver, SVG measurement) unavailable in jsdom.
 *
 * Strategy: mock @nivo/sankey to capture the props passed to ResponsiveSankey.
 * This lets us assert on theming/color props without a live DOM layout.
 * SankeyWidgetChart statically imports ResponsiveSankey (no next/dynamic in
 * this file — the single dynamic() boundary lives in SankeyWidget.tsx).
 */

// Capture the last set of props passed to ResponsiveSankey
let capturedProps: Record<string, unknown> = {};

vi.mock("@nivo/sankey", () => ({
  ResponsiveSankey: (props: Record<string, unknown>) => {
    capturedProps = props;
    return <div data-testid="mock-responsive-sankey" />;
  },
}));

// Reset captured props before each test so tests are independent.
beforeEach(() => {
  capturedProps = {};
});

import SankeyWidgetChart, { HUB_LABELS } from "@/components/reports/widgets/SankeyWidgetChart";
import { CHART_SERIES } from "@/lib/chart-colors";
import type { SankeyLink } from "@/lib/reports/types";

const SAMPLE_LINKS: SankeyLink[] = [
  { source: "__hub_income__", target: "Housing", value: 1000 },
  { source: "__hub_income__", target: "Food", value: 400 },
];

describe("SankeyWidgetChart — theme/color props", () => {
  it("renders the ResponsiveSankey mock", () => {
    const { getByTestId } = render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    expect(getByTestId("mock-responsive-sankey")).toBeInTheDocument();
  });

  it("passes labelTextColor referencing a design token CSS var", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    expect(typeof capturedProps.labelTextColor).toBe("string");
    expect(capturedProps.labelTextColor as string).toMatch(/^var\(--/);
  });

  it("passes theme.text.fill referencing a design token CSS var", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    const theme = capturedProps.theme as Record<string, unknown> | undefined;
    expect(theme).toBeDefined();
    const text = theme?.text as { fill?: string } | undefined;
    expect(text?.fill).toMatch(/^var\(--/);
  });

  it("passes theme.labels.text.fill referencing a design token CSS var", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    const theme = capturedProps.theme as Record<string, unknown> | undefined;
    const labels = theme?.labels as { text?: { fill?: string } } | undefined;
    expect(labels?.text?.fill).toMatch(/^var\(--/);
  });

  it("passes theme.tooltip.container with background and color tokens", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    const theme = capturedProps.theme as Record<string, unknown> | undefined;
    const tooltip = theme?.tooltip as
      | { container?: { background?: string; color?: string } }
      | undefined;
    expect(tooltip?.container?.background).toMatch(/^var\(--/);
    expect(tooltip?.container?.color).toMatch(/^var\(--/);
  });

  it("does not enable link gradient (source-color links are the default)", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    // enableLinkGradient=false keeps solid source-color ribbons
    expect(capturedProps.enableLinkGradient).toBe(false);
  });

  it("keeps linkOpacity at 0.35", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    expect(capturedProps.linkOpacity).toBe(0.35);
  });

  it("passes a stable SANKEY_COLORS array matching CHART_SERIES", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    const colors = capturedProps.colors as string[];
    expect(colors).toEqual(Array.from(CHART_SERIES));
    // Same reference across two renders (stable module-level const)
    const firstRef = capturedProps.colors;
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    expect(capturedProps.colors).toBe(firstRef);
  });

  it("disables animation", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    expect(capturedProps.animate).toBe(false);
  });

  it("uses wider margins to prevent label clipping", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    const margin = capturedProps.margin as { left?: number; right?: number } | undefined;
    expect(margin?.left).toBeGreaterThanOrEqual(80);
    expect(margin?.right).toBeGreaterThanOrEqual(80);
  });

  it("passes ariaLabel", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} title="My Sankey" />);
    expect(capturedProps.ariaLabel).toBe("My Sankey");
  });

  it("falls back to default ariaLabel when title is omitted", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    expect(capturedProps.ariaLabel).toBe("Cash flow Sankey chart");
  });
});

describe("SankeyWidgetChart — data wiring", () => {
  it("derives unique nodes from link source/target pairs (sentinel ids preserved)", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    const data = capturedProps.data as {
      nodes: { id: string }[];
      links: unknown[];
    };
    const nodeIds = data.nodes.map((n) => n.id).sort();
    // Sentinel id __hub_income__ is preserved as the node id; label mapping
    // is done via the label accessor prop, not by rewriting the node id.
    expect(nodeIds).toEqual(["Food", "Housing", "__hub_income__"]);
  });

  it("passes links through with source/target/value", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    const data = capturedProps.data as {
      nodes: unknown[];
      links: { source: string; target: string; value: number }[];
    };
    expect(data.links).toHaveLength(2);
    expect(data.links[0]).toEqual({
      source: "__hub_income__",
      target: "Housing",
      value: 1000,
    });
  });
});

describe("SankeyWidgetChart — hub label mapping", () => {
  it("HUB_LABELS maps sentinel ids to friendly display labels", () => {
    expect(HUB_LABELS["__hub_income__"]).toBe("Income");
    expect(HUB_LABELS["__hub_savings__"]).toBe("Savings");
    expect(HUB_LABELS["__hub_other__"]).toBe("Other");
  });

  it("passes a label accessor to ResponsiveSankey", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    expect(typeof capturedProps.label).toBe("function");
  });

  it("label accessor maps sentinel ids to friendly labels", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    const labelFn = capturedProps.label as (node: { id: string }) => string;
    expect(labelFn({ id: "__hub_income__" })).toBe("Income");
    expect(labelFn({ id: "__hub_savings__" })).toBe("Savings");
    expect(labelFn({ id: "__hub_other__" })).toBe("Other");
    // Real category ids pass through unchanged
    expect(labelFn({ id: "Housing" })).toBe("Housing");
    expect(labelFn({ id: "Food" })).toBe("Food");
  });

  it("passes nodeTooltip and linkTooltip render props", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    expect(typeof capturedProps.nodeTooltip).toBe("function");
    expect(typeof capturedProps.linkTooltip).toBe("function");
  });
});
