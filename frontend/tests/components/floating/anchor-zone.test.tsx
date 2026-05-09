import { render, screen } from "@testing-library/react";

import AnchorZone, { AnchorZoneSlot } from "@/components/floating/AnchorZone";

describe("AnchorZone", () => {
  it("renders the cluster pinned to bottom-right with the correct z-index", () => {
    render(
      <AnchorZone>
        <AnchorZoneSlot slot="primary">
          <button type="button">Primary</button>
        </AnchorZoneSlot>
      </AnchorZone>,
    );
    const zone = screen.getByTestId("anchor-zone");
    expect(zone).toBeInTheDocument();
    expect(zone.className).toMatch(/fixed/);
    expect(zone.className).toMatch(/bottom-4/);
    expect(zone.className).toMatch(/right-4/);
    expect(zone.className).toMatch(/z-40/);
    expect(zone.className).toMatch(/items-end/);
    expect(zone.className).toMatch(/flex-col/);
  });

  it("stacks multiple slots predictably with primary at the bottom (last in DOM order)", () => {
    render(
      <AnchorZone>
        <AnchorZoneSlot slot="primary">
          <button type="button">Primary FAB</button>
        </AnchorZoneSlot>
        <AnchorZoneSlot slot="secondary">
          <button type="button">Secondary widget</button>
        </AnchorZoneSlot>
      </AnchorZone>,
    );
    const buttons = screen.getAllByRole("button");
    // Visual order top-to-bottom is secondary then primary, so DOM order
    // (which equals tab order) reads: secondary first, primary last.
    expect(buttons[0]).toHaveTextContent("Secondary widget");
    expect(buttons[1]).toHaveTextContent("Primary FAB");
  });

  it("renders mounting order independent of slot priority", () => {
    // Mount primary FIRST in source order, secondary second. The cluster
    // must still place secondary above primary in the visual stack
    // (= DOM-order: secondary before primary).
    render(
      <AnchorZone>
        <AnchorZoneSlot slot="primary">
          <button type="button">P</button>
        </AnchorZoneSlot>
        <AnchorZoneSlot slot="secondary">
          <button type="button">S</button>
        </AnchorZoneSlot>
      </AnchorZone>,
    );
    const buttons = screen.getAllByRole("button");
    expect(buttons[0]).toHaveTextContent("S");
    expect(buttons[1]).toHaveTextContent("P");
  });
});
