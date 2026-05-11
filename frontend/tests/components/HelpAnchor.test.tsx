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
});
