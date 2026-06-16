import { describe, it, expect } from "vitest";
import sitemap from "@/app/sitemap";

describe("sitemap.ts", () => {
  it("includes /docs and /docs/plans", () => {
    const urls = sitemap().map((entry) => new URL(entry.url).pathname);
    expect(urls).toContain("/docs");
    expect(urls).toContain("/docs/plans");
  });

  it("lists the public URLs", () => {
    const urls = sitemap().map((entry) => new URL(entry.url).pathname);
    expect(urls).toContain("/");
    expect(urls).toContain("/register");
    expect(urls).toContain("/privacy");
    expect(urls).toContain("/terms");
  });

  it("omits /login (noindex, see app/login/page.tsx)", () => {
    const urls = sitemap().map((entry) => new URL(entry.url).pathname);
    expect(urls).not.toContain("/login");
  });

  it("includes the indexable marketing pages and all four /vs pages", () => {
    const urls = sitemap().map((entry) => new URL(entry.url).pathname);
    expect(urls).toContain("/features");
    expect(urls).toContain("/compare");
    expect(urls).toContain("/vs/spreadsheets");
    expect(urls).toContain("/vs/ynab");
    // PocketSmith and Monarch are now published (staggered launch complete).
    expect(urls).toContain("/vs/pocketsmith");
    expect(urls).toContain("/vs/monarch");
  });
});
