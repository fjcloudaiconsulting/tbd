// landing-payments-hidden.test.tsx — enforces that ZERO payment/pricing
// references surface to visitors on the landing page.
//
// The apex static export cannot read the runtime `billingUiEnabled` flag,
// so payment-surface removal is hardcoded (Option A precedent 2026-05-21).
// This test acts as the regression gate: if any forbidden string re-appears
// anywhere across the composed landing sections, this test fails.
//
// Strategy: render all landing sections that appear in app/page.tsx
// together (same approach as landing-page.test.tsx — direct component
// render, not the async RSC), collect the full textContent, then assert
// none of the forbidden substrings appear (case-insensitive).

import React from "react";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import Faq from "@/components/landing/Faq";
import FeatureTiles from "@/components/landing/FeatureTiles";
import Hero from "@/components/landing/Hero";
import HowItWorks from "@/components/landing/HowItWorks";
import LandingFooter from "@/components/landing/LandingFooter";
import ScreenshotShowcase from "@/components/landing/ScreenshotShowcase";
import SecondCta from "@/components/landing/SecondCta";
import TopNav from "@/components/landing/TopNav";

// NOTE: PricingPreview is intentionally not imported here.
// It is deleted in Task B.3 — the forbidden strings it previously
// contributed ("Pro", "Team", "€9", "€19", "Coming soon",
// "Join the waitlist", "Pricing") must not re-appear from any
// remaining section either.

// Forbidden patterns — any match in the rendered text is a payment-surface
// leak. Each entry is [label, regex] so the failure message is readable.
//
// "Pro" and "Team" are checked as whole words (word-boundary regex) to avoid
// false positives against "product", "provider", "projected", etc., which
// are legitimate copy. All other patterns are specific enough to not collide.
const FORBIDDEN: Array<[string, RegExp]> = [
  ["Pro (tier name)", /\bpro\b/i],
  ["Team (tier name)", /\bteam\b/i],
  ["€9", /€9/],
  ["€19", /€19/],
  ["Coming soon", /coming soon/i],
  ["Join the waitlist", /join the waitlist/i],
  ["Pricing", /pricing/i],
  ["payment methods", /payment methods/i],
  ["free plan", /free plan/i],
];

describe("landing page — payment surfaces hidden", () => {
  it("renders none of the forbidden payment/pricing patterns", () => {
    const { container } = render(
      <div>
        <TopNav />
        <Hero />
        <FeatureTiles />
        <ScreenshotShowcase />
        <HowItWorks />
        <Faq />
        <SecondCta />
        <LandingFooter />
      </div>,
    );

    const text = container.textContent ?? "";

    for (const [label, pattern] of FORBIDDEN) {
      expect(
        text,
        `Expected no "${label}" in landing page text`,
      ).not.toMatch(pattern);
    }
  });
});
