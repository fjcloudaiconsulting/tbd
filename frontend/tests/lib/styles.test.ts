import { describe, it, expect } from "vitest";
import { btnPrimary, btnSecondary } from "@/lib/styles";

describe("btnPrimary token", () => {
  it("bakes in the 44px touch-target floor so callers don't have to", () => {
    // DESIGN.md requires a 44px minimum touch target on every primary
    // button. Baking this into the shared token removes the per-call
    // `min-h-[44px]` overrides that PR #64 had to scatter through the
    // app. If anyone removes the floor here, dozens of buttons quietly
    // fall below WCAG. This is the systemic-level check that protects
    // that contract.
    expect(btnPrimary).toMatch(/(^|\s)min-h-\[44px\](\s|$)/);
  });

  it("still carries the brand accent surface + label tokens", () => {
    // Sanity guard so the touch-target fix can't accidentally clobber
    // the rest of the primary button styling.
    expect(btnPrimary).toContain("bg-accent");
    expect(btnPrimary).toContain("text-accent-text");
    expect(btnPrimary).toContain("hover:bg-accent-hover");
  });

  it("keeps btnSecondary independent of the floor change", () => {
    // btnSecondary intentionally does not bake in min-h-[44px]; per-call
    // sites still apply it where mobile touch targets are required.
    expect(btnSecondary).not.toMatch(/(^|\s)min-h-\[44px\](\s|$)/);
  });
});
