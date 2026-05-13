import React from "react";
import { render, screen } from "@testing-library/react";

import HelpAnchor from "@/components/HelpAnchor";

describe("HelpAnchor", () => {
  it("renders an anchor pointing at the given /docs section", () => {
    render(<HelpAnchor section="dashboard" />);

    const anchor = screen.getByTestId("help-anchor");
    expect(anchor).toBeInTheDocument();
    expect(anchor).toHaveAttribute("href", "/docs#dashboard");
    expect(anchor).toHaveAttribute("data-section", "dashboard");
  });

  it("opens in a new tab with secure rel attributes", () => {
    render(<HelpAnchor section="transactions" />);

    const anchor = screen.getByTestId("help-anchor");
    expect(anchor).toHaveAttribute("target", "_blank");
    expect(anchor).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("uses the section as the aria-label fallback", () => {
    render(<HelpAnchor section="accounts" />);

    expect(
      screen.getByRole("link", { name: "Help: accounts" }),
    ).toBeInTheDocument();
  });

  it("prefers the explicit label in the aria-label when provided", () => {
    render(<HelpAnchor section="forecast-plans" label="Forecast Plans" />);

    expect(
      screen.getByRole("link", { name: "Help: Forecast Plans" }),
    ).toBeInTheDocument();
  });

  it("marks the inner icon as decorative for screen readers", () => {
    const { container } = render(<HelpAnchor section="budgets" />);

    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  // Placement uniformity — PR fix/help-anchor-placement-uniform.
  // The default `variant="inline-title"` is for HelpAnchors that sit
  // next to a page heading. Tested by checking that the anchor's class
  // list includes the top-aligned positioning hook so a parent flex row
  // with `items-start` (or our wrapper below) lifts the `?` to align
  // with the TOP of the heading text instead of its baseline.
  it("defaults to the inline-title variant and self-aligns to the top of adjacent text", () => {
    render(<HelpAnchor section="dashboard" label="Dashboard" />);

    const anchor = screen.getByTestId("help-anchor");
    expect(anchor).toHaveAttribute("data-variant", "inline-title");
    // `self-start` lifts this single child to the top of the flex row,
    // regardless of the parent's items alignment. That is the load-
    // bearing geometric promise of inline-title.
    expect(anchor.className).toMatch(/\bself-start\b/);
    // Margin pulls the icon up to align with the cap height of a large
    // heading without inflating layout height.
    expect(anchor.className).toMatch(/\bmt-1\b/);
  });

  // The card-corner variant is for HelpAnchors that live inside a
  // card / tile. The geometric contract is `absolute top-3 right-3`
  // so a `relative` card parent docks the `?` in its top-right.
  it("supports a card-corner variant that absolutely positions in the top-right of a relative parent", () => {
    render(<HelpAnchor section="on-track" variant="card-corner" />);

    const anchor = screen.getByTestId("help-anchor");
    expect(anchor).toHaveAttribute("data-variant", "card-corner");
    expect(anchor.className).toMatch(/\babsolute\b/);
    expect(anchor.className).toMatch(/\btop-3\b/);
    expect(anchor.className).toMatch(/\bright-3\b/);
  });

  it("still accepts caller-supplied className overrides on top of variant defaults", () => {
    render(
      <HelpAnchor
        section="budgets"
        variant="card-corner"
        className="z-10"
      />,
    );

    const anchor = screen.getByTestId("help-anchor");
    expect(anchor.className).toMatch(/\babsolute\b/);
    expect(anchor.className).toMatch(/\bz-10\b/);
  });
});
