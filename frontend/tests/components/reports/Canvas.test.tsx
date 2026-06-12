import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";

import type { LayoutJson } from "@/lib/reports/types";

// The props the mocked react-grid-layout captures so tests can drive its
// callbacks directly.
interface CapturedProps {
  compactType: unknown;
  preventCollision?: boolean;
  onLayoutChange: (
    layout: Array<{ i: string; x: number; y: number; w: number; h: number }>,
  ) => void;
  onBreakpointChange: (breakpoint: string, cols: number) => void;
  children?: ReactNode;
}

// WidthProvider is stubbed as identity and Responsive is replaced with a
// prop-capturing stub: jsdom can't measure container width, so we can't
// exercise RGL's real ResizeObserver mount emission. Instead we drive
// `props.onLayoutChange` / `props.onBreakpointChange` manually to
// unit-test the guard logic in isolation.
let captured: CapturedProps | null = null;
vi.mock("react-grid-layout", () => ({
  Responsive: (props: CapturedProps) => {
    captured = props;
    return <div data-testid="rgl">{props.children}</div>;
  },
  WidthProvider: <T,>(c: T): T => c,
}));

import Canvas from "@/components/reports/Canvas";

const layout: LayoutJson = {
  version: 1,
  widgets: [
    { id: "a", type: "kpi", title: "A", grid: { x: 0, y: 0, w: 4, h: 4 }, config: {} },
  ] as unknown as LayoutJson["widgets"],
};

function renderCanvas(editMode = true) {
  const onLayoutChange = vi.fn();
  render(
    <Canvas
      layout={layout}
      editMode={editMode}
      onLayoutChange={onLayoutChange}
      renderWidget={() => <div>w</div>}
    />,
  );
  if (!captured) throw new Error("react-grid-layout stub did not capture props");
  return { props: captured, onLayoutChange };
}

describe("Canvas literal layout", () => {
  beforeEach(() => {
    captured = null; // reset so a test that forgets to render fails loudly
  });

  it("ignores layout emissions when not in edit mode", () => {
    const { props, onLayoutChange } = renderCanvas(false);
    props.onLayoutChange([{ i: "a", x: 3, y: 0, w: 4, h: 4 }]); // genuine move
    expect(onLayoutChange).not.toHaveBeenCalled();
  });

  it("passes compactType=null and preventCollision to react-grid-layout", () => {
    const { props } = renderCanvas();
    expect(props.compactType).toBeNull();
    expect(props.preventCollision).toBe(true);
  });

  it("does NOT call onLayoutChange for a mount/no-op emission (same grid)", () => {
    const { props, onLayoutChange } = renderCanvas();
    props.onLayoutChange([{ i: "a", x: 0, y: 0, w: 4, h: 4 }]); // identical → ignored
    expect(onLayoutChange).not.toHaveBeenCalled();
  });

  it("DOES call onLayoutChange when a widget actually moves", () => {
    const { props, onLayoutChange } = renderCanvas();
    props.onLayoutChange([{ i: "a", x: 3, y: 0, w: 4, h: 4 }]); // moved → propagate
    expect(onLayoutChange).toHaveBeenCalledTimes(1);
    expect(onLayoutChange.mock.calls[0][0].widgets[0].grid.x).toBe(3);
  });

  it("DOES call onLayoutChange on a resize-only change (h only)", () => {
    const { props, onLayoutChange } = renderCanvas();
    props.onLayoutChange([{ i: "a", x: 0, y: 0, w: 4, h: 6 }]); // resized → propagate
    expect(onLayoutChange).toHaveBeenCalledTimes(1);
    expect(onLayoutChange.mock.calls[0][0].widgets[0].grid.h).toBe(6);
  });

  it("does NOT persist a clamped layout emitted at a narrow (<12 col) breakpoint", () => {
    const { props, onLayoutChange } = renderCanvas();
    // RGL switches to the sm breakpoint (6 cols) and clamps the widget's
    // width — a responsive view, not an edit. onBreakpointChange fires
    // synchronously before onLayoutChange in the same cycle.
    props.onBreakpointChange("sm", 6);
    props.onLayoutChange([{ i: "a", x: 0, y: 0, w: 2, h: 4 }]); // clamped → ignored
    expect(onLayoutChange).not.toHaveBeenCalled();
  });

  it("resumes persisting after returning to a 12-col breakpoint", () => {
    const { props, onLayoutChange } = renderCanvas();
    props.onBreakpointChange("sm", 6);
    props.onLayoutChange([{ i: "a", x: 0, y: 0, w: 2, h: 4 }]); // ignored (narrow)
    props.onBreakpointChange("lg", 12);
    props.onLayoutChange([{ i: "a", x: 1, y: 0, w: 4, h: 4 }]); // real edit at lg
    expect(onLayoutChange).toHaveBeenCalledTimes(1);
    expect(onLayoutChange.mock.calls[0][0].widgets[0].grid.x).toBe(1);
  });
});
