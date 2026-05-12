import React from "react";
import { render } from "@testing-library/react";

import HeroDashboard from "@/components/landing/HeroDashboard";

describe("<HeroDashboard />", () => {
  it("renders as decorative (aria-hidden) per spec", () => {
    const { container } = render(<HeroDashboard />);
    const root = container.firstChild as HTMLElement;
    expect(root).toHaveAttribute("aria-hidden", "true");
  });

  it("renders the schematic dashboard pieces (balance, bars, budget rows)", () => {
    const { container, getByText } = render(<HeroDashboard />);
    expect(getByText(/april balance/i)).toBeInTheDocument();
    expect(getByText(/€4,283\.12/)).toBeInTheDocument();
    expect(getByText(/groceries/i)).toBeInTheDocument();
    expect(getByText(/dining/i)).toBeInTheDocument();
    expect(getByText(/transport/i)).toBeInTheDocument();
    // 12 weekly-spend bars
    const bars = container.querySelectorAll(
      "div.bg-success\\/80, div.bg-danger\\/80",
    );
    expect(bars.length).toBe(12);
  });

  it("never contains an em-dash", () => {
    const { container } = render(<HeroDashboard />);
    expect(container.textContent).not.toMatch(/—/);
  });
});
