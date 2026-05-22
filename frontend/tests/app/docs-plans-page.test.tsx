/**
 * Smoke test for /docs/plans, the in-app Plans guide added with the
 * UX polish PR (2026-05-22). Verifies the page renders, all five
 * documented sections are present, and the link from /docs back to
 * the guide is wired.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

import PlansDocsPage from "@/app/docs/plans/page";
import DocsPage from "@/app/docs/page";

vi.mock("@/components/ui/ThemeToggle", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("@/components/ui/BackLink", () => ({
  __esModule: true,
  default: () => null,
}));

describe("/docs/plans", () => {
  it("renders the page heading and all five sections", () => {
    render(<PlansDocsPage />);
    expect(
      screen.getByRole("heading", { name: /plans guide/i, level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("plans-docs-section-what")).toBeInTheDocument();
    expect(screen.getByTestId("plans-docs-section-verdict")).toBeInTheDocument();
    expect(screen.getByTestId("plans-docs-section-math")).toBeInTheDocument();
    expect(screen.getByTestId("plans-docs-section-howto")).toBeInTheDocument();
    expect(screen.getByTestId("plans-docs-section-curve")).toBeInTheDocument();
  });

  it("includes the contribution curve worked example", () => {
    render(<PlansDocsPage />);
    // The math example mentions specific figures that the product
    // owner approved in the spec; pin them so they don't drift.
    const curveSection = screen.getByTestId("plans-docs-section-curve");
    expect(curveSection.textContent).toMatch(/age 30/i);
    expect(curveSection.textContent).toMatch(/age 40/i);
    expect(curveSection.textContent).toMatch(/800/);
    expect(curveSection.textContent).toMatch(/1,200/);
  });

  it("explains the verdict colors", () => {
    render(<PlansDocsPage />);
    const section = screen.getByTestId("plans-docs-section-verdict");
    expect(section.textContent).toMatch(/green/i);
    expect(section.textContent).toMatch(/yellow/i);
    expect(section.textContent).toMatch(/red/i);
    expect(section.textContent).toMatch(/80 percent/i);
  });

  it("explains the smoothed-with-regression option in the math section", () => {
    render(<PlansDocsPage />);
    const section = screen.getByTestId("plans-docs-section-math");
    expect(section.textContent).toMatch(/regression/i);
  });

  it("links back to the Plans page and the docs home from the footer", () => {
    render(<PlansDocsPage />);
    const plansLink = screen.getByRole("link", { name: /^plans$/i });
    expect(plansLink).toHaveAttribute("href", "/plans");
    const docsHome = screen.getByRole("link", { name: /docs home/i });
    expect(docsHome).toHaveAttribute("href", "/docs");
  });
});

describe("/docs has a link to /docs/plans", () => {
  it("renders the Plans guide link in core concepts", () => {
    render(<DocsPage />);
    const link = screen.getByTestId("docs-plans-link");
    expect(link).toHaveAttribute("href", "/docs/plans");
  });
});
