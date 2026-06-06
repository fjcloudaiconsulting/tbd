import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import LandingPage from "@/app/page";

// Stub the client island that requires AuthProvider/useRouter — we only
// care about the JSON-LD <script> elements emitted by the RSC shell.
vi.mock("@/components/landing/LandingAuthRedirect", () => ({
  default: () => null,
}));

// readNonce uses next/headers which is unavailable in Vitest; return "".
vi.mock("@/lib/nonce", () => ({
  readNonce: async () => "",
}));

// next/navigation may be pulled in by child components.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

describe("landing JSON-LD", () => {
  it("renders Organization, WebSite, SoftwareApplication, HowTo and FAQPage blocks", async () => {
    const ui = await LandingPage();
    const { container } = render(ui as React.ReactElement);
    const scripts = Array.from(
      container.querySelectorAll('script[type="application/ld+json"]'),
    );
    expect(scripts.length).toBeGreaterThanOrEqual(5);

    const parsed = scripts.map((s) => JSON.parse(s.textContent ?? "{}"));
    const types = parsed.map((p) => p["@type"]);
    expect(types).toContain("Organization");
    expect(types).toContain("WebSite");
    expect(types).toContain("SoftwareApplication");
    expect(types).toContain("HowTo");
    expect(types).toContain("FAQPage");

    // SoftwareApplication and WebSite reference the standalone Organization
    // node by @id, so the entity resolves to a single canonical node.
    const org = parsed.find((p) => p["@type"] === "Organization");
    expect(org["@id"]).toBeDefined();
    const software = parsed.find((p) => p["@type"] === "SoftwareApplication");
    expect(software.author).toEqual({ "@id": org["@id"] });
    expect(software.publisher).toEqual({ "@id": org["@id"] });

    // HowTo steps mirror the rendered "how it works" section.
    const howTo = parsed.find((p) => p["@type"] === "HowTo");
    expect(Array.isArray(howTo.step)).toBe(true);
    expect(howTo.step.length).toBe(3);
    expect(howTo.step[0]["@type"]).toBe("HowToStep");
  });

  it("FAQPage mainEntity mirrors the rendered FAQ entries", async () => {
    const ui = await LandingPage();
    const { container } = render(ui as React.ReactElement);
    const scripts = Array.from(
      container.querySelectorAll('script[type="application/ld+json"]'),
    );
    const faq = scripts
      .map((s) => JSON.parse(s.textContent ?? "{}"))
      .find((p) => p["@type"] === "FAQPage");
    expect(faq).toBeDefined();
    expect(Array.isArray(faq.mainEntity)).toBe(true);
    expect(faq.mainEntity.length).toBe(6);  // matches faqData.ts entries (post-#378 trim)
    // Each entry has the canonical Question/Answer shape.
    for (const q of faq.mainEntity) {
      expect(q["@type"]).toBe("Question");
      expect(typeof q.name).toBe("string");
      expect(q.acceptedAnswer?.["@type"]).toBe("Answer");
      expect(typeof q.acceptedAnswer?.text).toBe("string");
    }
  });
});
