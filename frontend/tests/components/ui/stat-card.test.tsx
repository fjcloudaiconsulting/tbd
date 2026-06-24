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
  it("omits sub/badge when not provided", () => {
    render(<StatCard label="X" value="1" />);
    expect(screen.getByText("X")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.queryByText("Actual: 0.00")).not.toBeInTheDocument();
    expect(screen.queryByText("BADGE")).not.toBeInTheDocument();
  });
  it("renders badge when provided", () => {
    render(<StatCard label="Y" value="42" badge={<span>BADGE</span>} />);
    expect(screen.getByText("BADGE")).toBeInTheDocument();
  });
  it("does not render badge when not provided", () => {
    render(<StatCard label="Z" value="99" />);
    expect(screen.queryByText("BADGE")).not.toBeInTheDocument();
  });
});
