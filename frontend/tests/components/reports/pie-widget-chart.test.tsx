import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

// Recharts uses ResizeObserver which is not available in jsdom
global.ResizeObserver = vi.fn().mockImplementation(() => ({
  observe: vi.fn(),
  unobserve: vi.fn(),
  disconnect: vi.fn(),
}));

import PieWidgetChart from "@/components/reports/widgets/PieWidgetChart";

describe("PieWidgetChart", () => {
  it("renders a center total label summing all slice values (currency format)", () => {
    const rows = [
      { label: "Food", value: 300 },
      { label: "Transport", value: 200 },
    ];
    render(
      <PieWidgetChart rows={rows} format="currency" currency="EUR" />,
    );
    // Total = 500, formatted as currency EUR → "€500.00"
    expect(screen.getByTestId("pie-center-total")).toBeInTheDocument();
    expect(screen.getByTestId("pie-center-total").textContent).toMatch(/500/);
  });

  it("renders a center total label with number format (no currency symbol)", () => {
    const rows = [
      { label: "A", value: 1000 },
      { label: "B", value: 2000 },
    ];
    render(<PieWidgetChart rows={rows} format="number" />);
    // Total = 3000
    expect(screen.getByTestId("pie-center-total").textContent).toMatch(/3,000|3000/);
  });
});
