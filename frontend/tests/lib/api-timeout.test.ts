import { describe, it, expect } from "vitest";
import { timeoutForPath } from "@/lib/api";

describe("timeoutForPath", () => {
  it("gives AI dispatch paths a 90s budget", () => {
    expect(timeoutForPath("/api/v1/ai/forecast/refine")).toBe(90_000);
    expect(timeoutForPath("/api/v1/ai/forecast/refine/estimate")).toBe(90_000);
    expect(timeoutForPath("/api/v1/ai/categorize")).toBe(90_000);
  });
  it("leaves non-AI paths on the 10s default", () => {
    expect(timeoutForPath("/api/v1/transactions")).toBe(10_000);
  });
});
