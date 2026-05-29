// landing-page.test.tsx — integration coverage for the L5.1 landing
// surface. Tests render each long-form section directly (not the
// async server component in app/page.tsx) so we can assert content
// and interaction without a server runtime.
//
// Per-section a11y / mark-up correctness lives in
// tests/components/landing/*; this file is the surface-level safety
// net for the L5.1 scope: pricing / FAQ / screenshots /
// animation discipline.
//
// Testimonials are intentionally not covered here. The component is
// stubbed to render nothing until we have real, consented quotes from
// named customers. See components/landing/Testimonials.tsx.

import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Faq from "@/components/landing/Faq";
import ScreenshotShowcase from "@/components/landing/ScreenshotShowcase";

const EM_DASH = "—";

function allText(container: HTMLElement): string {
  return container.textContent ?? "";
}

describe("L5.1 landing — Faq", () => {
  it("renders 6 FAQ items inside <details>", () => {
    const { container } = render(<Faq />);
    const detailsEls = container.querySelectorAll("details");
    expect(detailsEls.length).toBe(6);
  });

  it("each FAQ item is collapsed by default (no `open` attr)", () => {
    const { container } = render(<Faq />);
    const open = container.querySelectorAll("details[open]");
    expect(open.length).toBe(0);
  });

  it("clicking a summary opens the surrounding details element", () => {
    const { container } = render(<Faq />);
    const first = container.querySelector("details");
    expect(first).not.toBeNull();
    if (!first) return;
    const summary = first.querySelector("summary");
    expect(summary).not.toBeNull();
    summary?.click();
    expect(first.open).toBe(true);
  });

  it("each summary is keyboard-focusable via native <summary> semantics", () => {
    const { container } = render(<Faq />);
    const summaries = container.querySelectorAll("summary");
    // Native <summary> is keyboard-operable out of the box; assert
    // we did not break it by adding a tabIndex={-1}.
    summaries.forEach((s) => {
      expect(s.getAttribute("tabindex")).not.toBe("-1");
    });
  });

  it("anchor target #faq is on the section element", () => {
    const { container } = render(<Faq />);
    expect(container.querySelector("section#faq")).not.toBeNull();
  });

  it("contains zero em-dashes (locked policy)", () => {
    const { container } = render(<Faq />);
    expect(allText(container)).not.toContain(EM_DASH);
  });
});

describe("L5.1 landing — ScreenshotShowcase", () => {
  it("renders three product previews with aria-labels", () => {
    render(<ScreenshotShowcase />);
    expect(
      screen.getByRole("img", { name: /Transactions preview/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /Reports preview/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /Plans preview/ }),
    ).toBeInTheDocument();
  });

  it("each preview has a short product caption visible to users", () => {
    const { container } = render(<ScreenshotShowcase />);
    // Captions sit alongside the framed preview in the same grid
    // cell pair. We assert each surface kicker ("Transactions" /
    // "Reports" / "Plans") shows up as an uppercase eyebrow.
    const text = allText(container);
    expect(text).toMatch(/Transactions/);
    expect(text).toMatch(/Reports/);
    expect(text).toMatch(/Plans/);
  });

  it("preview frames use animation classes guarded by motion-safe", () => {
    const { container } = render(<ScreenshotShowcase />);
    const animated = container.querySelectorAll(
      "[class*='motion-safe:animate-']",
    );
    // 3 previews × (1 caption + 1 frame) = 6 animated nodes.
    expect(animated.length).toBeGreaterThanOrEqual(6);
  });

  it("contains zero em-dashes (locked policy)", () => {
    const { container } = render(<ScreenshotShowcase />);
    expect(allText(container)).not.toContain(EM_DASH);
  });
});

describe("L5.1 landing — em-dash discipline across all new sections", () => {
  it("all new sections together contain zero em-dashes", () => {
    const { container } = render(
      <div>
        <ScreenshotShowcase />
        <Faq />
      </div>,
    );
    expect(allText(container)).not.toContain(EM_DASH);
  });
});
