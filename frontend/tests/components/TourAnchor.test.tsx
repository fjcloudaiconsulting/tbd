import React from "react";
import { render, screen } from "@testing-library/react";
import { renderHook } from "@testing-library/react";

import TourAnchor from "@/components/tour/TourAnchor";
import { useTour } from "@/components/tour/useTour";

describe("TourAnchor", () => {
  it("wraps children in a span with data-tour-id by default", () => {
    render(
      <TourAnchor id="dashboard.balance-tile">
        <div data-testid="content">Balance</div>
      </TourAnchor>,
    );

    const anchor = screen.getByTestId("tour-anchor");
    expect(anchor.tagName).toBe("SPAN");
    expect(anchor).toHaveAttribute("data-tour-id", "dashboard.balance-tile");
    expect(anchor).toContainElement(screen.getByTestId("content"));
  });

  it('with as="child" clones the single element and adds data-tour-id', () => {
    render(
      <TourAnchor id="transactions.filter-bar" as="child">
        <div data-testid="filter-bar">Filters</div>
      </TourAnchor>,
    );

    expect(screen.queryByTestId("tour-anchor")).not.toBeInTheDocument();
    const bar = screen.getByTestId("filter-bar");
    expect(bar).toHaveAttribute("data-tour-id", "transactions.filter-bar");
  });
});

describe("useTour stub", () => {
  it("returns isActive=false and no-op handlers", () => {
    const { result } = renderHook(() => useTour());

    expect(result.current.isActive).toBe(false);
    expect(result.current.currentStep).toBeNull();
    expect(result.current.totalSteps).toBe(0);

    // Method calls should not throw and should be no-ops.
    expect(() => result.current.start(["dashboard.balance-tile"])).not.toThrow();
    expect(() => result.current.next()).not.toThrow();
    expect(() => result.current.prev()).not.toThrow();
    expect(() => result.current.close()).not.toThrow();

    // State still false after calling start (it's a stub).
    expect(result.current.isActive).toBe(false);
  });
});
