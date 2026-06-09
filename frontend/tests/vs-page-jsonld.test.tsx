// frontend/tests/vs-page-jsonld.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, within } from "@testing-library/react";
import VsPageLayout from "@/components/landing/VsPageLayout";

vi.mock("@/lib/links", () => ({
  signupHref: () => "/register",
  ctaHref: (p: string) => p,
  IS_APEX_BUILD: false,
}));

describe("VsPageLayout", () => {
  const faq = [
    { q: "Is it a good YNAB alternative?", a: "Yes, if you want forecasting." },
    { q: "Does it sync with my bank?", a: "No, it imports CSV or OFX." },
  ];

  it("emits a FAQPage and BreadcrumbList JSON-LD mirroring the faq prop", () => {
    const { container } = render(
      <VsPageLayout
        slug="ynab"
        competitor="ynab"
        title="The Better Decision vs YNAB"
        intro={<p>Intro copy.</p>}
        faq={faq}
        nonce=""
      />,
    );
    const parsed = Array.from(
      container.querySelectorAll('script[type="application/ld+json"]'),
    ).map((s) => JSON.parse(s.textContent ?? "{}"));
    const types = parsed.map((p) => p["@type"]);
    expect(types).toContain("FAQPage");
    expect(types).toContain("BreadcrumbList");
    const faqLd = parsed.find((p) => p["@type"] === "FAQPage");
    expect(faqLd.mainEntity.length).toBe(2);
    expect(faqLd.mainEntity[0]["@type"]).toBe("Question");
    expect(faqLd.mainEntity[0].acceptedAnswer["@type"]).toBe("Answer");
  });

  it("renders the honest 'where they win' points from comparison data", () => {
    const { getByRole } = render(
      <VsPageLayout
        slug="ynab"
        competitor="ynab"
        title="The Better Decision vs YNAB"
        intro={<p>Intro copy.</p>}
        faq={faq}
        nonce=""
      />,
    );
    const region = getByRole("region", { name: /where ynab wins/i });
    expect(within(region).getByText(/live bank sync/i)).toBeTruthy();
  });
});
