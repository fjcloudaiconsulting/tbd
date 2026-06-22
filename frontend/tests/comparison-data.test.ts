// frontend/tests/comparison-data.test.ts
import { describe, it, expect } from "vitest";
import {
  competitorOrder,
  dimensionOrder,
  comparisonMatrix,
  competitorMeta,
} from "@/lib/comparison";

describe("comparison data", () => {
  it("matrix is dense: every dimension x competitor has a cell", () => {
    for (const dim of dimensionOrder) {
      for (const comp of competitorOrder) {
        const cell = comparisonMatrix[dim]?.[comp];
        expect(cell, `${dim}.${comp}`).toBeDefined();
        expect(typeof cell.value).toBe("string");
        expect(cell.value.length).toBeGreaterThan(0);
        expect(["yes", "no", "partial"]).toContain(cell.supported);
      }
    }
  });

  it("tbd price is the founders 'free while we grow' string, never a hard price", () => {
    expect(comparisonMatrix.price.tbd.value).toBe("Free while we grow");
  });

  it("every competitor has a name and at least one honest 'where they win' point", () => {
    for (const comp of competitorOrder) {
      expect(competitorMeta[comp].name.length).toBeGreaterThan(0);
      if (comp !== "tbd") {
        expect(competitorMeta[comp].whereTheyWin.length).toBeGreaterThan(0);
      }
    }
  });

  it("contains no em-dashes in any customer-facing string", () => {
    const strings: string[] = [];
    for (const dim of dimensionOrder)
      for (const comp of competitorOrder)
        strings.push(comparisonMatrix[dim][comp].value);
    for (const comp of competitorOrder) {
      strings.push(competitorMeta[comp].name);
      strings.push(...competitorMeta[comp].whereTheyWin);
    }
    for (const s of strings) expect(s).not.toContain("—");
  });
});
