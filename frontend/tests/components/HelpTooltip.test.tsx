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
});
