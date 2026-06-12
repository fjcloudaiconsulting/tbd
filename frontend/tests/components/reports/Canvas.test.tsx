import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";

// Capture the props react-grid-layout receives + expose its onLayoutChange.
let captured: any = null;
vi.mock("react-grid-layout", () => {
  const React = require("react");
  const Responsive = (props: any) => {
    captured = props;
    return React.createElement("div", { "data-testid": "rgl" }, props.children);
  };
  return { Responsive, WidthProvider: (C: any) => C, default: { Responsive } };
});

import Canvas from "@/components/reports/Canvas";
import type { LayoutJson } from "@/lib/reports/types";

const layout: LayoutJson = {
  version: 1,
  widgets: [
    { id: "a", type: "kpi", title: "A", grid: { x: 0, y: 0, w: 4, h: 4 }, config: {} },
  ] as any,
};

function renderCanvas(onLayoutChange = vi.fn()) {
  render(
    <Canvas layout={layout} editMode onLayoutChange={onLayoutChange} renderWidget={() => <div>w</div>} />,
  );
  return onLayoutChange;
}

describe("Canvas literal layout", () => {
  it("passes compactType=null and preventCollision to react-grid-layout", () => {
    renderCanvas();
    expect(captured.compactType).toBeNull();
    expect(captured.preventCollision).toBe(true);
  });

  it("does NOT call onLayoutChange for a mount/no-op emission (same grid)", () => {
    const cb = renderCanvas();
    captured.onLayoutChange([{ i: "a", x: 0, y: 0, w: 4, h: 4 }]); // identical → ignored
    expect(cb).not.toHaveBeenCalled();
  });

  it("DOES call onLayoutChange when a widget actually moves", () => {
    const cb = renderCanvas();
    captured.onLayoutChange([{ i: "a", x: 3, y: 0, w: 4, h: 4 }]); // moved → propagate
    expect(cb).toHaveBeenCalledTimes(1);
    expect(cb.mock.calls[0][0].widgets[0].grid.x).toBe(3);
  });
});
