import { describe, expect, it } from "vitest";

import { pivotBySecondaryDimension } from "@/lib/reports/series";
import type { QueryRow } from "@/lib/reports/types";

describe("pivotBySecondaryDimension", () => {
  it("pivots rows into stable generated series keys paired with labels", () => {
    const rows: QueryRow[] = [
      { month: "Jan", account: "Checking", value: 10 },
      { month: "Jan", account: "Savings", value: 5 },
      { month: "Feb", account: "Checking", value: 7 },
    ];

    const out = pivotBySecondaryDimension(rows, "month", "account");

    // Display labels keep first-seen order.
    expect(out.secondaryValues).toEqual(["Checking", "Savings"]);
    // Series keys are generated (s0, s1, …), NOT the raw labels.
    expect(out.seriesKeys).toEqual(["s0", "s1"]);

    // Rows keyed by the generated keys, with missing combos backfilled 0.
    const jan = out.rows.find((r) => r.label === "Jan")!;
    const feb = out.rows.find((r) => r.label === "Feb")!;
    expect(jan.s0).toBe(10);
    expect(jan.s1).toBe(5);
    expect(feb.s0).toBe(7);
    expect(feb.s1).toBe(0); // Savings had no Feb row → backfilled.
  });

  it("does not break on a secondary value containing a dot", () => {
    // A raw value like "Acme Inc." used as a Recharts dataKey would be
    // parsed as a nested path. Generated keys sidestep that entirely.
    const rows: QueryRow[] = [
      { month: "Jan", vendor: "Acme Inc.", value: 3 },
    ];
    const out = pivotBySecondaryDimension(rows, "month", "vendor");
    expect(out.secondaryValues).toEqual(["Acme Inc."]);
    expect(out.seriesKeys).toEqual(["s0"]);
    expect(out.rows[0].s0).toBe(3);
  });

  it("is immune to prototype pollution from a __proto__ secondary value", () => {
    const rows: QueryRow[] = [
      { month: "Jan", k: "__proto__", value: 9 },
      { month: "Jan", k: "constructor", value: 4 },
    ];
    const out = pivotBySecondaryDimension(rows, "month", "k");

    // The malicious values become ordinary generated data keys.
    expect(out.secondaryValues).toEqual(["__proto__", "constructor"]);
    expect(out.seriesKeys).toEqual(["s0", "s1"]);
    expect(out.rows[0].s0).toBe(9);
    expect(out.rows[0].s1).toBe(4);

    // Nothing leaked onto Object.prototype.
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
    expect(Object.prototype.hasOwnProperty.call({}, "9")).toBe(false);
  });
});
