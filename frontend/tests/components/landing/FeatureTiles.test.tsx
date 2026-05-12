import React from "react";
import { render, screen } from "@testing-library/react";

import FeatureTiles from "@/components/landing/FeatureTiles";

describe("<FeatureTiles />", () => {
  const expectedTitles = [
    "See your money clearly",
    "Plan what's coming",
    "Shared, if you want",
    "Your data stays yours",
  ];

  it("renders the four spec tiles in the locked emotional arc order", () => {
    render(<FeatureTiles />);
    const headings = screen
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent);
    expect(headings).toEqual(expectedTitles);
  });

  it("renders each tile sub-copy and an ordinal marker", () => {
    render(<FeatureTiles />);
    expect(
      screen.getByText(/all your accounts and transactions/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/budgets, forecasts, and recurring/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/built for households/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/eu-hosted today/i),
    ).toBeInTheDocument();
    // Ordinal markers 01..04
    for (const n of ["01", "02", "03", "04"]) {
      expect(screen.getByText(n)).toBeInTheDocument();
    }
  });

  it("never contains an em-dash (locked policy)", () => {
    const { container } = render(<FeatureTiles />);
    expect(container.textContent).not.toMatch(/—/);
  });

  it("is labelled with a section aria-label", () => {
    render(<FeatureTiles />);
    expect(
      screen.getByRole("region", { name: /what you can do/i }),
    ).toBeInTheDocument();
  });
});
