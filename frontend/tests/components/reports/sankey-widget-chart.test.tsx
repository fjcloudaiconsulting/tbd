import { render } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import React from "react";

/**
 * SankeyWidgetChart renders Nivo ResponsiveSankey which depends on browser
 * layout APIs (ResizeObserver, SVG measurement) unavailable in jsdom.
 *
 * Strategy: mock next/dynamic so the component is imported synchronously,
 * then mock @nivo/sankey to capture the props passed to ResponsiveSankey.
 * This lets us assert on theming/color props without a live DOM layout.
 */

// Capture the last set of props passed to ResponsiveSankey
let capturedProps: Record<string, unknown> = {};

vi.mock("@nivo/sankey", () => ({
  ResponsiveSankey: (props: Record<string, unknown>) => {
    capturedProps = props;
    return <div data-testid="mock-responsive-sankey" />;
  },
}));

// next/dynamic with ssr:false needs to be unwrapped — mock it so the inner
// import runs synchronously in tests.
vi.mock("next/dynamic", () => ({
  default: (
    loader: () => Promise<unknown>,
    _opts?: unknown,
  ) => {
    // Return a component that renders the dynamically loaded component.
    // We capture it during the test render cycle.
    let Loaded: React.ComponentType<Record<string, unknown>> | null = null;
    loader().then((mod) => {
      // The loader returns the module; Nivo mock returns the component directly
      // via `.then((m) => m.ResponsiveSankey)` in SankeyWidgetChart.
      Loaded = mod as React.ComponentType<Record<string, unknown>>;
    });
    return function DynamicWrapper(props: Record<string, unknown>) {
      if (!Loaded) return null;
      return <Loaded {...props} />;
    };
  },
}));

import SankeyWidgetChart from "@/components/reports/widgets/SankeyWidgetChart";
import type { SankeyLink } from "@/lib/reports/types";

const SAMPLE_LINKS: SankeyLink[] = [
  { source: "Income", target: "Housing", value: 1000 },
  { source: "Income", target: "Food", value: 400 },
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
});

describe("SankeyWidgetChart — data wiring", () => {
  it("derives unique nodes from link source/target pairs", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    const data = capturedProps.data as {
      nodes: { id: string }[];
      links: unknown[];
    };
    const nodeIds = data.nodes.map((n) => n.id).sort();
    expect(nodeIds).toEqual(["Food", "Housing", "Income"]);
  });

  it("passes links through with source/target/value", () => {
    render(<SankeyWidgetChart links={SAMPLE_LINKS} />);
    const data = capturedProps.data as {
      nodes: unknown[];
      links: { source: string; target: string; value: number }[];
    };
    expect(data.links).toHaveLength(2);
    expect(data.links[0]).toEqual({
      source: "Income",
      target: "Housing",
      value: 1000,
    });
  });
});
