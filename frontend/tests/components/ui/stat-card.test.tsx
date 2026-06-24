import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import StatCard from "@/components/ui/StatCard";

describe("StatCard", () => {
  it("renders label, value, sub and applies valueClassName", () => {
    render(<StatCard label="TOTAL BUDGET" value="2,300.00" sub="Actual: 0.00" valueClassName="text-success" />);
    expect(screen.getByText("TOTAL BUDGET")).toBeInTheDocument();
    const val = screen.getByText("2,300.00");
    expect(val).toBeInTheDocument();
    expect(val.className).toContain("text-success");
    expect(screen.getByText("Actual: 0.00")).toBeInTheDocument();
  });

  it("omits sub/badge structurally: value is a bare <p>, no sub element", () => {
    render(<StatCard label="X" value="1" />);
    expect(screen.getByText("X")).toBeInTheDocument();
    const val = screen.getByText("1");
    // No flex wrapper div around the value when badge is absent
    expect(val.tagName).toBe("P");
    expect(val.closest(".flex.flex-wrap.items-center")).toBeNull();
    // Sub element must not exist
    expect(screen.queryByTestId("stat-card-sub")).toBeNull();
  });

  it("applies valueClassName in the badge-present branch and renders badge", () => {
    render(<StatCard label="X" value="9" valueClassName="text-danger" badge={<span>OVER</span>} />);
    const val = screen.getByText("9");
    expect(val.className).toContain("text-danger");
    expect(screen.getByText("OVER")).toBeInTheDocument();
  });

  it("renders badge when provided", () => {
    render(<StatCard label="Y" value="42" badge={<span>BADGE</span>} />);
    expect(screen.getByText("BADGE")).toBeInTheDocument();
  });
});
