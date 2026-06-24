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

  it("defaults value size to text-2xl and applies a custom valueSize", () => {
    const { rerender } = render(<StatCard label="A" value="1" />);
    expect(screen.getByText("1").className).toContain("text-2xl");
    rerender(<StatCard label="A" value="2" valueSize="text-xl" />);
    const v = screen.getByText("2");
    expect(v.className).toContain("text-xl");
    expect(v.className).not.toContain("text-2xl");
    // Invariants: always present regardless of size
    expect(v.className).toContain("font-semibold");
    expect(v.className).toContain("tabular-nums");
  });

  it("valueSize badge-present branch: resolvedValueSize applies and default text-2xl is absent", () => {
    render(<StatCard label="X" value="9" valueSize="text-xl" badge={<span>B</span>} />);
    const val = screen.getByText("9");
    expect(val.className).toContain("text-xl");
    expect(val.className).not.toContain("text-2xl");
    // Structural invariants in badge branch
    expect(val.className).toContain("font-semibold");
    expect(val.className).toContain("tabular-nums");
  });

  it("applies a custom subClassName when sub is provided", () => {
    render(<StatCard label="A" value="1" sub="Actual: 0.00" subClassName="mt-0.5 text-xs text-text-muted" />);
    const sub = screen.getByTestId("stat-card-sub");
    expect(sub.className).toContain("text-xs");
  });
});
