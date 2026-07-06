/**
 * Smoke test for /docs/plans, the in-app Plans guide added with the
 * UX polish PR (2026-05-22). Verifies the page renders, all five
 * documented sections are present, and the link from /docs back to
 * the guide is wired.
 */
import { render, screen } from "@testing-library/react";

import PlansDocsPage from "@/app/docs/plans/page";
import DocsPage from "@/app/docs/page";

vi.mock("@/components/ui/ThemeToggle", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("@/components/ui/BackLink", () => ({
  __esModule: true,
  default: () => null,
}));

// The docs pages are async Server Components that read the per-request
// CSP nonce. next/headers is unavailable in Vitest; return "" so the
// inline JSON-LD scripts render without a nonce attribute.
vi.mock("@/lib/nonce", () => ({
  readNonce: async () => "",
}));

describe("/docs/plans", () => {
  it("renders the page heading and all five sections", async () => {
    render(await PlansDocsPage());
    expect(
      screen.getByRole("heading", { name: /plans guide/i, level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("plans-docs-section-what")).toBeInTheDocument();
    expect(screen.getByTestId("plans-docs-section-verdict")).toBeInTheDocument();
    expect(screen.getByTestId("plans-docs-section-math")).toBeInTheDocument();
    expect(screen.getByTestId("plans-docs-section-howto")).toBeInTheDocument();
    expect(screen.getByTestId("plans-docs-section-curve")).toBeInTheDocument();
  });

  it("includes the contribution curve worked example", async () => {
    render(await PlansDocsPage());
    // The math example mentions specific figures that the product
    // owner approved in the spec; pin them so they don't drift.
    const curveSection = screen.getByTestId("plans-docs-section-curve");
    expect(curveSection.textContent).toMatch(/age 30/i);
    expect(curveSection.textContent).toMatch(/age 40/i);
    expect(curveSection.textContent).toMatch(/800/);
    expect(curveSection.textContent).toMatch(/1,200/);
  });

  it("explains the verdict colors", async () => {
    render(await PlansDocsPage());
    const section = screen.getByTestId("plans-docs-section-verdict");
    expect(section.textContent).toMatch(/green/i);
    expect(section.textContent).toMatch(/yellow/i);
    expect(section.textContent).toMatch(/red/i);
    expect(section.textContent).toMatch(/80 percent/i);
  });

  it("explains the smoothed-with-regression option in the math section", async () => {
    render(await PlansDocsPage());
    const section = screen.getByTestId("plans-docs-section-math");
    expect(section.textContent).toMatch(/regression/i);
  });

  it("links back to the Plans page and the docs home from the footer", async () => {
    render(await PlansDocsPage());
    const plansLink = screen.getByRole("link", { name: /^plans$/i });
    expect(plansLink).toHaveAttribute("href", "/plans");
    const docsHome = screen.getByRole("link", { name: /docs home/i });
    expect(docsHome).toHaveAttribute("href", "/docs");
  });
});

describe("/docs has a link to /docs/plans", () => {
  it("renders the Plans guide link in core concepts", async () => {
    render(await DocsPage());
    const link = screen.getByTestId("docs-plans-link");
    expect(link).toHaveAttribute("href", "/docs/plans");
  });
});

describe("docs structured data (JSON-LD)", () => {
  function parseLd(container: HTMLElement) {
    return Array.from(
      container.querySelectorAll('script[type="application/ld+json"]'),
    ).map((s) => JSON.parse(s.textContent ?? "{}"));
  }

  // Same node @ids the landing page declares, so the docs entities link
  // into the one canonical site graph. isPartOf → WebSite (a CreativeWork,
  // per schema.org); publisher → Organization.
  const orgId = "https://thebetterdecision.com/#organization";
  const websiteId = "https://thebetterdecision.com/#website";

  it("/docs emits a TechArticle + BreadcrumbList linked to the Organization @id", async () => {
    const { container } = render(await DocsPage());
    const blocks = parseLd(container);
    const types = blocks.map((b) => b["@type"]);
    expect(types).toContain("TechArticle");
    expect(types).toContain("BreadcrumbList");

    const article = blocks.find((b) => b["@type"] === "TechArticle");
    expect(article.url).toBe("https://thebetterdecision.com/docs/");
    expect(article.inLanguage).toBe("en");
    expect(article.isPartOf).toEqual({ "@id": websiteId });
    expect(article.publisher).toEqual({ "@id": orgId });
    // No fabricated authorship fields.
    expect(article.author).toBeUndefined();
    expect(article.datePublished).toBeUndefined();

    const crumb = blocks.find((b) => b["@type"] === "BreadcrumbList");
    expect(crumb.itemListElement.map((i: { name: string }) => i.name)).toEqual([
      "Home",
      "Docs",
    ]);
  });

  it("/docs/plans emits a TechArticle + 3-level BreadcrumbList", async () => {
    const { container } = render(await PlansDocsPage());
    const blocks = parseLd(container);
    const article = blocks.find((b) => b["@type"] === "TechArticle");
    expect(article.url).toBe("https://thebetterdecision.com/docs/plans/");
    expect(article.isPartOf).toEqual({ "@id": websiteId });
    expect(article.publisher).toEqual({ "@id": orgId });

    const crumb = blocks.find((b) => b["@type"] === "BreadcrumbList");
    expect(crumb.itemListElement.map((i: { name: string }) => i.name)).toEqual([
      "Home",
      "Docs",
      "Plans",
    ]);
  });
});
