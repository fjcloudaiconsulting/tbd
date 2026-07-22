import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import CreditUtilizationBar from "@/components/dashboard/widgets/CreditUtilizationBar";

describe("CreditUtilizationBar", () => {
  it("labels a low-utilization card with just the percent (neutral band)", () => {
    render(<CreditUtilizationBar name="Visa" balance={-500} creditLimit={2000} currency="EUR" />);
    expect(screen.getByText("Visa")).toBeInTheDocument();
    expect(screen.getByText(/25%/)).toBeInTheDocument();
    expect(screen.queryByText(/Over limit/)).toBeNull();
  });
  it("labels a high-utilization card (>=75%) with High", () => {
    render(<CreditUtilizationBar name="Amex" balance={-1700} creditLimit={2000} currency="EUR" />);
    expect(screen.getByText(/85%/)).toBeInTheDocument();
    expect(screen.getByText(/High/)).toBeInTheDocument();
  });
  it("labels an over-limit card with the overage in currency", () => {
    render(<CreditUtilizationBar name="Store" balance={-2500} creditLimit={2000} currency="EUR" />);
    expect(screen.getByText(/Over limit/)).toBeInTheDocument();
    expect(screen.getByText(/500/)).toBeInTheDocument();
  });
});
