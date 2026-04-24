import {
  USERNAME_MAX_LENGTH,
  USERNAME_MIN_LENGTH,
  USERNAME_PATTERN_RE,
  USERNAME_RULE_HINT,
} from "@/lib/validation";


describe("username validation constants", () => {
  it("match the documented constraints", () => {
    expect(USERNAME_MIN_LENGTH).toBe(3);
    expect(USERNAME_MAX_LENGTH).toBe(64);
    expect(USERNAME_RULE_HINT).toContain("dot");
  });

  it("accepts only the supported username characters", () => {
    expect(USERNAME_PATTERN_RE.test("alice.smith_123")).toBe(true);
    expect(USERNAME_PATTERN_RE.test("alice smith")).toBe(false);
    expect(USERNAME_PATTERN_RE.test("álîçé")).toBe(false);
  });
});
