import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import FeaturesPage from "@/app/features/page";
import LandingPage from "@/app/page";

vi.mock("@/components/landing/LandingAuthRedirect", () => ({ default: () => null }));
vi.mock("@/lib/nonce", () => ({ readNonce: async () => "" }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

const ROADMAP_TERMS = [/\bMCP\b/i, /assistant/i, /hosted AI/i, /roadmap/i];

async function featureListStrings(Page: () => Promise<React.ReactElement>) {
  const { container } = render((await Page()) as React.ReactElement);
  const blocks = Array.from(
    container.querySelectorAll('script[type="application/ld+json"]'),
  )
    .map((s) => JSON.parse(s.textContent ?? "{}"))
    .filter((b) => Array.isArray(b.featureList));
  return blocks.flatMap((b) => b.featureList as string[]);
}

describe("featureList JSON-LD never advertises roadmap items", () => {
  it("on /features", async () => {
    const list = await featureListStrings(FeaturesPage);
    expect(list.length).toBeGreaterThan(0);
    for (const item of list)
      for (const term of ROADMAP_TERMS) expect(item).not.toMatch(term);
  });
  it("on the homepage", async () => {
    const list = await featureListStrings(LandingPage as () => Promise<React.ReactElement>);
    for (const item of list)
      for (const term of ROADMAP_TERMS) expect(item).not.toMatch(term);
  });
});
