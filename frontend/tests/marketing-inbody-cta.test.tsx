import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ComparePage from "@/app/compare/page";
import FeaturesPage from "@/app/features/page";
import LandingPage from "@/app/page";
import VsYnabPage from "@/app/vs/ynab/page";
import SecondCta from "@/components/landing/SecondCta";
import { trackRegisterClick } from "@/lib/analytics";

// TBD-269. /features is the paid campaign's final URL and /compare carries the
// high-intent competitor traffic; both shipped with the nav "Get started" as
// their ONLY conversion path, so a visitor who scrolled had to scroll back up.
//
// Two properties are fenced here, and they are separate claims:
//   1. each page renders a signup CTA INSIDE <main> (not merely somewhere on
//      the page, which the nav CTA would satisfy on its own), and
//   2. every placement reports a DISTINCT GA4 cta_location, with the homepage
//      block still reporting the pre-existing "second_cta" default.

vi.mock("@/lib/analytics", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/analytics")>()),
  trackRegisterClick: vi.fn(),
}));

vi.mock("@/lib/nonce", () => ({ readNonce: async () => "" }));

vi.mock("@/components/landing/LandingAuthRedirect", () => ({
  default: () => null,
}));

vi.mock("@/components/ThemeProvider", () => ({
  useTheme: () => ({ theme: "dark", toggle: vi.fn() }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

type AsyncPage = () => Promise<React.ReactElement>;

async function renderPage(Page: AsyncPage): Promise<HTMLElement> {
  const { container } = render((await Page()) as React.ReactElement);
  return container;
}

// The signup href on the non-apex test build is "/register" (see
// tests/components/landing/signup-link.test.tsx).
function signupLinksIn(root: Element | null): HTMLAnchorElement[] {
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLAnchorElement>('a[href="/register"]'));
}

function primaryNav(container: HTMLElement): Element | null {
  return container.querySelector('nav[aria-label="Primary"]');
}

// "In body" means inside the page's <main> landmark. <main> and the primary
// <nav> are disjoint subtrees (MarketingShell renders TopNav as a sibling of
// its children), so this deliberately CANNOT be satisfied by the nav CTA.
function inBodySignupLinks(container: HTMLElement): HTMLAnchorElement[] {
  return signupLinksIn(container.querySelector("main"));
}

// jsdom does not implement navigation; cancel it so the component's onClick
// still runs without a "Not implemented" bail-out.
function clickWithoutNavigating(link: HTMLAnchorElement): void {
  link.addEventListener("click", (e) => e.preventDefault());
  fireEvent.click(link);
}

function lastReportedLocation(): unknown {
  const mock = vi.mocked(trackRegisterClick);
  expect(mock).toHaveBeenCalledTimes(1);
  return mock.mock.calls[0][0];
}

async function inBodyCtaLocation(Page: AsyncPage): Promise<unknown> {
  const container = await renderPage(Page);
  const links = inBodySignupLinks(container);
  expect(links.length).toBeGreaterThanOrEqual(1);
  clickWithoutNavigating(links[0]);
  const location = lastReportedLocation();
  cleanup();
  vi.clearAllMocks();
  return location;
}

describe("marketing pages expose an in-body signup CTA", () => {
  it("/features renders a signup CTA inside <main>, not only in the nav", async () => {
    const container = await renderPage(FeaturesPage);

    // The nav CTA is still there; this assertion proves the selector works and
    // is NOT what carries the test.
    const nav = primaryNav(container);
    expect(signupLinksIn(nav)).toHaveLength(1);

    const inBody = inBodySignupLinks(container);
    expect(inBody.length).toBeGreaterThanOrEqual(1);
    expect(nav?.contains(inBody[0])).toBe(false);
  });

  it("/compare renders a signup CTA inside <main>, not only in the nav", async () => {
    const container = await renderPage(ComparePage);

    const nav = primaryNav(container);
    expect(signupLinksIn(nav)).toHaveLength(1);

    const inBody = inBodySignupLinks(container);
    expect(inBody.length).toBeGreaterThanOrEqual(1);
    expect(nav?.contains(inBody[0])).toBe(false);
  });
});

describe("signup CTA telemetry stays attributable per placement", () => {
  it("/features in-body CTA reports cta_location 'features'", async () => {
    const container = await renderPage(FeaturesPage);
    clickWithoutNavigating(inBodySignupLinks(container)[0]);
    expect(trackRegisterClick).toHaveBeenCalledTimes(1);
    expect(trackRegisterClick).toHaveBeenCalledWith("features");
  });

  it("/compare in-body CTA reports cta_location 'compare'", async () => {
    const container = await renderPage(ComparePage);
    clickWithoutNavigating(inBodySignupLinks(container)[0]);
    expect(trackRegisterClick).toHaveBeenCalledTimes(1);
    expect(trackRegisterClick).toHaveBeenCalledWith("compare");
  });

  it("the two new in-body CTAs, the /vs CTA, the nav CTA and the homepage block are five distinct locations", async () => {
    const featuresLocation = await inBodyCtaLocation(FeaturesPage);
    const compareLocation = await inBodyCtaLocation(ComparePage);
    // /vs/* is untouched by TBD-269, but it is a fifth in-body placement and
    // its location must not collide with the two new ones.
    const vsLocation = await inBodyCtaLocation(VsYnabPage as AsyncPage);

    // Nav location, read from a real page render rather than hard-coded.
    const container = await renderPage(FeaturesPage);
    clickWithoutNavigating(signupLinksIn(primaryNav(container))[0]);
    const navLocation = lastReportedLocation();
    cleanup();
    vi.clearAllMocks();

    // Homepage block, via the un-parameterised default.
    render(<SecondCta />);
    clickWithoutNavigating(
      screen.getByRole("link", { name: /get started free/i }) as HTMLAnchorElement,
    );
    const homeLocation = lastReportedLocation();

    expect(
      new Set([
        featuresLocation,
        compareLocation,
        vsLocation,
        navLocation,
        homeLocation,
      ]).size,
    ).toBe(5);
    expect(featuresLocation).not.toBe(compareLocation);
    expect(featuresLocation).not.toBe(navLocation);
    expect(compareLocation).not.toBe(navLocation);
    expect(featuresLocation).not.toBe(vsLocation);
    expect(compareLocation).not.toBe(vsLocation);
  });
});

describe("the homepage's second_cta telemetry is unchanged", () => {
  it("SecondCta with no location prop still reports 'second_cta'", () => {
    render(<SecondCta />);
    clickWithoutNavigating(
      screen.getByRole("link", { name: /get started free/i }) as HTMLAnchorElement,
    );
    expect(trackRegisterClick).toHaveBeenCalledTimes(1);
    expect(trackRegisterClick).toHaveBeenCalledWith("second_cta");
  });

  it("the homepage renders SecondCta without overriding its location", async () => {
    await renderPage(LandingPage as AsyncPage);
    const heading = screen.getByRole("heading", {
      level: 2,
      name: /ready to see clearly\?/i,
    });
    const block = heading.closest("section");
    expect(block).not.toBeNull();
    const link = signupLinksIn(block)[0];
    expect(link).toBeDefined();
    clickWithoutNavigating(link);
    expect(trackRegisterClick).toHaveBeenCalledTimes(1);
    expect(trackRegisterClick).toHaveBeenCalledWith("second_cta");
  });
});
