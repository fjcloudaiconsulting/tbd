import { describe, expect, it } from "vitest";
import { creditUtilization } from "@/lib/credit";

describe("creditUtilization", () => {
  it("computes outstanding, util%, available, over for a mid-utilization card", () => {
    const r = creditUtilization(-500, 2000);
    expect(r.outstanding).toBe(500);
    expect(r.utilizationPct).toBe(25);
    expect(r.available).toBe(1500);
    expect(r.over).toBe(-1500);
  });
  it("treats a positive (in-credit) balance as zero outstanding", () => {
    const r = creditUtilization(120, 2000);
    expect(r.outstanding).toBe(0);
    expect(r.utilizationPct).toBe(0);
    expect(r.available).toBe(2120);
    expect(r.over).toBe(-2000);
  });
  it("reports over-limit with an uncapped util% and positive over", () => {
    const r = creditUtilization(-2500, 2000);
    expect(r.outstanding).toBe(2500);
    expect(r.utilizationPct).toBe(125);
    expect(r.available).toBe(-500);
    expect(r.over).toBe(500);
  });
});
