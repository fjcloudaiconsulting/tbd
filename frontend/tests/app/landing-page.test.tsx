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
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Faq from "@/components/landing/Faq";
import PricingPreview from "@/components/landing/PricingPreview";
import ScreenshotShowcase from "@/components/landing/ScreenshotShowcase";

const EM_DASH = "—";

function allText(container: HTMLElement): string {
  return container.textContent ?? "";
}

describe("L5.1 landing — PricingPreview", () => {
  it("renders three tiers (Free, Pro, Team) each with a price", () => {
    render(<PricingPreview />);
    expect(
      screen.getByRole("heading", { level: 3, name: /^Free$/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 3, name: /^Pro$/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 3, name: /^Team$/ }),
    ).toBeInTheDocument();
    // The Free price is €0; paid tiers are placeholder values flagged
    // to architect in the PR body.
    expect(screen.getByText(/€0/)).toBeInTheDocument();
  });

  it("marks paid tiers as 'Coming soon' (BILLING_UI_ENABLED honored)", () => {
    render(<PricingPreview />);
    // The Coming-soon badge must appear exactly twice: Pro + Team.
    const comingSoonBadges = screen.getAllByText(/Coming soon/i);
    expect(comingSoonBadges).toHaveLength(2);
  });

  it("paid tier CTAs read 'Join the waitlist', not 'Upgrade'", () => {
    render(<PricingPreview />);
    expect(
      screen.queryByRole("link", { name: /upgrade/i }),
    ).not.toBeInTheDocument();
    const waitlistLinks = screen.getAllByRole("link", {
      name: /join the waitlist/i,
    });
    // Two paid tiers, one CTA each.
    expect(waitlistLinks).toHaveLength(2);
  });

  it("Free tier CTA links to /register", () => {
    render(<PricingPreview />);
    const freeCta = screen.getByRole("link", { name: /get started free/i });
    expect(freeCta).toHaveAttribute("href", "/register");
  });

  it("anchor target #pricing is on the section element", () => {
    const { container } = render(<PricingPreview />);
    expect(container.querySelector("section#pricing")).not.toBeNull();
  });

  it("contains zero em-dashes (locked policy)", () => {
    const { container } = render(<PricingPreview />);
    expect(allText(container)).not.toContain(EM_DASH);
  });
});

describe("L5.1 landing — Faq", () => {
  it("renders 8 FAQ items inside <details>", () => {
    const { container } = render(<Faq />);
    const detailsEls = container.querySelectorAll("details");
    expect(detailsEls.length).toBe(8);
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
        <PricingPreview />
        <ScreenshotShowcase />
        <Faq />
      </div>,
    );
    expect(allText(container)).not.toContain(EM_DASH);
  });

  it("PricingPreview pricing region announces itself via aria-labelledby", () => {
    render(<PricingPreview />);
    const region = screen.getByRole("region", {
      name: /Simple pricing\. No surprises\./,
    });
    expect(region).not.toBeNull();
    // Sanity: the heading inside the region is the same element the
    // section is labelled by.
    const heading = within(region).getByRole("heading", { level: 2 });
    expect(heading.id).toBe("pricing-heading");
  });
});
