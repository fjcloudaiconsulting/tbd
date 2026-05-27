/**
 * HelpTooltip render tests (L5.3).
 *
 * The wrapper delegates to the base Tooltip, so we only verify:
 *   - It looks up content from the map by key.
 *   - It forwards triggerLabel + learnMoreSection through.
 *   - It renders a trigger element (the underlying button).
 */
import { describe, expect, it } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

import HelpTooltip from "@/components/help/HelpTooltip";
import { HELP_TOOLTIPS } from "@/lib/help/tooltips";

describe("HelpTooltip", () => {
  it("renders a trigger button by default", () => {
    render(<HelpTooltip k="tx.type" />);
    const trigger = screen.getByTestId("tooltip-trigger");
    expect(trigger).toBeInTheDocument();
    expect(trigger.tagName).toBe("BUTTON");
  });

  it("uses the content map entry on open", async () => {
    render(<HelpTooltip k="tx.amount" />);
    const trigger = screen.getByTestId("tooltip-trigger");
    act(() => {
      fireEvent.focus(trigger);
    });
    const bubble = await screen.findByRole("tooltip");
    // Match against the actual map entry so a copy edit there does
    // not break this test as long as it stays non-empty.
    expect(bubble).toHaveTextContent(HELP_TOOLTIPS["tx.amount"].content);
  });

  it("uses the per-entry triggerLabel as the ARIA label", () => {
    render(<HelpTooltip k="tx.type" />);
    const trigger = screen.getByTestId("tooltip-trigger");
    expect(trigger).toHaveAttribute(
      "aria-label",
      HELP_TOOLTIPS["tx.type"].triggerLabel,
    );
  });

  it("renders a Learn more link when the entry sets learnMoreSection", async () => {
    render(<HelpTooltip k="cat.subcategory" />);
    const trigger = screen.getByTestId("tooltip-trigger");
    act(() => {
      fireEvent.focus(trigger);
    });
    await screen.findByRole("tooltip");
    const link = screen.getByTestId("tooltip-learn-more");
    expect(link).toHaveAttribute(
      "data-section",
      HELP_TOOLTIPS["cat.subcategory"].learnMoreSection!,
    );
  });

  it("generates unique DOM ids when the same key is rendered twice on the page", async () => {
    // The transactions page (edit row) and the floating quick-add
    // form both render <HelpTooltip k="tx.amount" />. If HelpTooltip
    // hashed the id from the key alone, the second mount would
    // collide with the first and break aria-describedby wiring.
    render(
      <div>
        <HelpTooltip k="tx.amount" />
        <HelpTooltip k="tx.amount" />
      </div>,
    );
    const triggers = screen.getAllByTestId("tooltip-trigger");
    expect(triggers).toHaveLength(2);
    // Open the first tooltip and capture its id, then the second.
    // The base Tooltip wires aria-describedby imperatively on focus.
    act(() => fireEvent.focus(triggers[0]));
    const id0 = triggers[0].getAttribute("aria-describedby");
    act(() => fireEvent.blur(triggers[0]));
    act(() => fireEvent.focus(triggers[1]));
    const id1 = triggers[1].getAttribute("aria-describedby");
    expect(id0).toBeTruthy();
    expect(id1).toBeTruthy();
    expect(id0).not.toBe(id1);
    // The stable key prefix still appears in both ids so dev tools
    // and audits can grep for the field name.
    expect(id0).toContain("help-tx-amount");
    expect(id1).toContain("help-tx-amount");
  });
});
