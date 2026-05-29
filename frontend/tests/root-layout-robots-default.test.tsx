import { describe, it, expect } from "vitest";
import { metadata } from "@/app/layout";

describe("root layout default robots", () => {
  it("defaults to noindex, nofollow (safer for an auth-walled app)", () => {
    expect(metadata.robots).toEqual({ index: false, follow: false });
  });
});
