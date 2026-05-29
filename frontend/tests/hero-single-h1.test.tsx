import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import Hero from "@/components/landing/Hero";

describe("Hero", () => {
  it("renders exactly one <h1>", () => {
    const { container } = render(<Hero />);
    expect(container.querySelectorAll("h1").length).toBe(1);
  });

  it("hero above-the-fold text contains an SEO keyword", () => {
    // The h1 itself is brand-locked to the tagline ("There's no best
    // decision. Only better ones.") — see hero-brand-lock test. Search
    // engines weight the full above-the-fold block, not just the h1 tag.
    // This assertion guards that at least one SEO keyword appears
    // somewhere in the rendered Hero section.
    const { container } = render(<Hero />);
    const text = container.textContent ?? "";
    expect(text).toMatch(/finance|money|budget|plan/i);
  });
});
