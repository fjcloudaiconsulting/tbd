import { describe, it, expect } from "vitest";
import sitemap from "@/app/sitemap";

describe("sitemap.ts", () => {
  it("includes /docs and /docs/plans", () => {
    const urls = sitemap().map((entry) => new URL(entry.url).pathname);
    expect(urls).toContain("/docs");
    expect(urls).toContain("/docs/plans");
  });

  it("preserves the original 5 public URLs", () => {
    const urls = sitemap().map((entry) => new URL(entry.url).pathname);
    expect(urls).toContain("/");
    expect(urls).toContain("/login");
    expect(urls).toContain("/register");
    expect(urls).toContain("/privacy");
    expect(urls).toContain("/terms");
  });
});
