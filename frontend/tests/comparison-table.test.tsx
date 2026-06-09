// frontend/tests/comparison-table.test.tsx
import { describe, it, expect } from "vitest";
import { render, within } from "@testing-library/react";
import ComparisonTable from "@/components/landing/ComparisonTable";
import { dimensionOrder } from "@/lib/comparison";

describe("ComparisonTable", () => {
  it("renders a column per requested competitor with scoped headers", () => {
    const { getByRole } = render(
      <ComparisonTable competitors={["tbd", "ynab"]} />,
    );
    const table = getByRole("table");
    const colHeaders = within(table)
      .getAllByRole("columnheader")
      .map((th) => th.getAttribute("scope"));
    // first header is the dimension column, then one per competitor
    expect(colHeaders).toEqual(["col", "col", "col"]);
    expect(within(table).getByText("The Better Decision")).toBeTruthy();
    expect(within(table).getByText("YNAB")).toBeTruthy();
  });

  it("renders a row per dimension with a row header", () => {
    const { getByRole } = render(
      <ComparisonTable competitors={["tbd", "ynab"]} />,
    );
    const rowHeaders = within(getByRole("table")).getAllByRole("rowheader");
    expect(rowHeaders.length).toBe(dimensionOrder.length);
  });

  it("each cell exposes a non-visual support label for screen readers", () => {
    const { getByRole } = render(
      <ComparisonTable competitors={["tbd", "ynab"]} />,
    );
    // sr-only text like "Yes" / "No" / "Partial" appears in cells
    expect(within(getByRole("table")).getAllByText(/^(Yes|No|Partial)$/).length)
      .toBeGreaterThan(0);
  });

  it("does not emit a yes/no/partial support label for the Price row", () => {
    const { getByRole } = render(
      <ComparisonTable competitors={["tbd", "ynab"]} />,
    );
    const priceHeader = within(getByRole("table")).getByText("Price");
    const priceRow = priceHeader.closest("tr");
    expect(priceRow).not.toBeNull();
    // Price is informational, not a capability: no glyph + sr-only Yes/No/Partial.
    expect(within(priceRow as HTMLElement).queryAllByText(/^(Yes|No|Partial)$/))
      .toHaveLength(0);
  });
});
