// frontend/tests/lib/ai-tooltips.test.ts
import { describe, it, expect } from "vitest";
import { getHelpTooltip } from "@/lib/help/tooltips";

describe("AI feature tooltips", () => {
  it("resolves the 3 AI tooltip keys and deep-links to the ai-features docs section", () => {
    for (const k of ["ai.forecast", "ai.categorize", "ai.budget"] as const) {
      const entry = getHelpTooltip(k);
      expect(entry.content.length).toBeGreaterThan(0);
      expect(entry.learnMoreSection).toBe("ai-features");
    }
  });
});
