/**
 * Smoke test for /docs/dashboard, the in-app "Customizing your dashboard"
 * guide added with the dashboard customization wave (2026-06-26). Verifies
 * the page renders, the documented sections are present, key product facts
 * are pinned, and the link from /docs to the guide is wired.
 */
import React from "react";
import { render, screen } from "@testing-library/react";

import DashboardDocsPage from "@/app/docs/dashboard/page";
import DocsPage from "@/app/docs/page";

vi.mock("@/components/ui/ThemeToggle", () => ({
  __esModule: true,
  default: () => null,
}));

vi.mock("@/components/ui/BackLink", () => ({
  __esModule: true,
  default: () => null,
}));

// The docs pages are async Server Components that read the per-request CSP
// nonce. next/headers is unavailable in Vitest; return "" so the inline
// JSON-LD scripts render without a nonce attribute.
vi.mock("@/lib/nonce", () => ({
  readNonce: async () => "",
}));

describe("/docs/dashboard", () => {
  it("renders the page heading and the documented sections", async () => {
    render(await DashboardDocsPage());
    expect(
      screen.getByRole("heading", {
        name: /customizing your dashboard/i,
        level: 1,
      }),
    ).toBeInTheDocument();
    for (const id of [
      "overview",
      "default-tiles",
      "customize",
      "rearrange",
      "add",
      "from-report",
      "recent-tx",
      "reset-save",
      "mobile",
      "report-widgets",
    ]) {
      expect(
        screen.getByTestId(`dashboard-docs-section-${id}`),
      ).toBeInTheDocument();
    }
  });

  it("documents add-from-report as an independent copy incl. sankey", async () => {
    render(await DashboardDocsPage());
    const section = screen.getByTestId("dashboard-docs-section-from-report");
    expect(section.textContent).toMatch(/independent/i);
    expect(section.textContent).toMatch(/sankey/i);
  });

  it("documents the recent-transactions page-size options (10–100)", async () => {
    render(await DashboardDocsPage());
    const section = screen.getByTestId("dashboard-docs-section-recent-tx");
    expect(section.textContent).toMatch(/10, 25, 50, or 100/);
    expect(section.textContent).toMatch(/scroll/i);
  });

  it("explains phone-style reflow/compaction in the rearrange section", async () => {
    render(await DashboardDocsPage());
    const section = screen.getByTestId("dashboard-docs-section-rearrange");
    expect(section.textContent).toMatch(/phone/i);
    expect(section.textContent).toMatch(/compact/i);
  });

  it("links to the dashboard and reports from the footer", async () => {
    render(await DashboardDocsPage());
    expect(
      screen.getByRole("link", { name: /^dashboard$/i }),
    ).toHaveAttribute("href", "/dashboard");
    expect(
      screen.getByRole("link", { name: /^reports$/i }),
    ).toHaveAttribute("href", "/reports");
  });
});

describe("/docs has a link to /docs/dashboard", () => {
  it("renders the dashboard guide link in the Dashboard section", async () => {
    render(await DocsPage());
    const link = screen.getByTestId("docs-dashboard-link");
    expect(link).toHaveAttribute("href", "/docs/dashboard");
  });
});

describe("/docs/dashboard structured data (JSON-LD)", () => {
  function parseLd(container: HTMLElement) {
    return Array.from(
      container.querySelectorAll('script[type="application/ld+json"]'),
    ).map((s) => JSON.parse(s.textContent ?? "{}"));
  }

  const orgId = "https://thebetterdecision.com/#organization";
  const websiteId = "https://thebetterdecision.com/#website";

  it("emits a TechArticle + 3-level BreadcrumbList linked to the site graph", async () => {
    const { container } = render(await DashboardDocsPage());
    const blocks = parseLd(container);
    const article = blocks.find((b) => b["@type"] === "TechArticle");
    expect(article.url).toBe("https://thebetterdecision.com/docs/dashboard/");
    expect(article.isPartOf).toEqual({ "@id": websiteId });
    expect(article.publisher).toEqual({ "@id": orgId });
    expect(article.author).toBeUndefined();

    const crumb = blocks.find((b) => b["@type"] === "BreadcrumbList");
    expect(
      crumb.itemListElement.map((i: { name: string }) => i.name),
    ).toEqual(["Home", "Docs", "Dashboard"]);
  });
});
