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
  it("renders SoftwareApplication and FAQPage blocks", async () => {
    const ui = await LandingPage();
    const { container } = render(ui as React.ReactElement);
    const scripts = Array.from(
      container.querySelectorAll('script[type="application/ld+json"]'),
    );
    expect(scripts.length).toBeGreaterThanOrEqual(2);

    const parsed = scripts.map((s) => JSON.parse(s.textContent ?? "{}"));
    const types = parsed.map((p) => p["@type"]);
    expect(types).toContain("SoftwareApplication");
    expect(types).toContain("FAQPage");

    const software = parsed.find((p) => p["@type"] === "SoftwareApplication");
    expect(software.author).toBeDefined();
    expect(software.publisher).toBeDefined();
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
    expect(faq.mainEntity.length).toBe(8);  // matches faqData.ts entries
    // Each entry has the canonical Question/Answer shape.
    for (const q of faq.mainEntity) {
      expect(q["@type"]).toBe("Question");
      expect(typeof q.name).toBe("string");
      expect(q.acceptedAnswer?.["@type"]).toBe("Answer");
      expect(typeof q.acceptedAnswer?.text).toBe("string");
    }
  });
});
