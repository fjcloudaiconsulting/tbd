/**
 * Theme regression test for the L3.3 tour overlay (PR #238 fix).
 *
 * The first cut of TourProvider.tsx hard-coded slate-900 and slate-500
 * hex literals into the overlay styles, which broke dark mode (dark
 * text on dark page). This test pins the overlay chrome to the theme
 * tokens published by app/globals.css so a future regression surfaces
 * here before the design-token CI check has a chance to fire.
 *
 * jsdom does not actually compute the cascaded value of a CSS custom
 * property, so we assert on the Tailwind class names that map to the
 * tokens. The build-time mapping in globals.css guarantees these
 * classes resolve to the per-theme `--theme-*` variables.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  usePathname: () => "/onboarding",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import { TourProvider } from "@/components/tour/TourProvider";
import { useTour } from "@/components/tour/useTour";

function Starter({ steps }: { steps: string[] }) {
  const tour = useTour();
  return (
    <button
      type="button"
      data-testid="starter"
      onClick={() => tour.start(steps)}
    >
      start
    </button>
  );
}

beforeEach(() => {
  // Ensure each test starts in a known theme state.
  document.documentElement.removeAttribute("data-theme");
  // Place an anchor in the DOM so the overlay has something to point at.
  const anchor = document.createElement("div");
  anchor.setAttribute("data-tour-id", "dashboard.header");
  anchor.style.position = "fixed";
  anchor.style.top = "100px";
  anchor.style.left = "100px";
  anchor.style.width = "200px";
  anchor.style.height = "40px";
  document.body.appendChild(anchor);
});

afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
  document
    .querySelectorAll('[data-tour-id="dashboard.header"]')
    .forEach((n) => n.remove());
});

function startTour() {
  act(() => {
    screen.getByTestId("starter").click();
  });
}

describe("Tour overlay theming", () => {
  it("paints the backdrop and card via theme tokens, not raw hex", () => {
    render(
      <TourProvider>
        <Starter steps={["dashboard.header", "dashboard.import-cta"]} />
      </TourProvider>,
    );
    startTour();

    const card = screen.getByTestId("tour-card");
    // Card uses surface + text-primary tokens, not a slate-900 literal.
    expect(card.className).toMatch(/\bbg-surface\b/);
    expect(card.className).toMatch(/\btext-text-primary\b/);
    expect(card.className).toMatch(/\bshadow-card\b/);

    // No inline style should smuggle in a hex literal. Tailwind classes
    // own every color path so the theme switch can reach them.
    expect(card.getAttribute("style") ?? "").not.toMatch(/#[0-9a-fA-F]{6}/);

    // Skip button uses text-muted token, prev uses border-border + text-primary.
    expect(screen.getByTestId("tour-skip").className).toMatch(
      /\btext-text-muted\b/,
    );
    expect(screen.getByTestId("tour-prev").className).toMatch(
      /\bborder-border\b/,
    );
    expect(screen.getByTestId("tour-prev").className).toMatch(
      /\btext-text-primary\b/,
    );

    // Next button uses the brand accent, mirroring btnPrimary.
    expect(screen.getByTestId("tour-next").className).toMatch(/\bbg-accent\b/);
    expect(screen.getByTestId("tour-next").className).toMatch(
      /\btext-accent-text\b/,
    );
  });

  it("keeps the same token-driven classes when data-theme=light is active", () => {
    document.documentElement.setAttribute("data-theme", "light");
    render(
      <TourProvider>
        <Starter steps={["dashboard.header", "dashboard.import-cta"]} />
      </TourProvider>,
    );
    startTour();

    const card = screen.getByTestId("tour-card");
    // Same class names; the per-theme CSS variables behind them flip
    // automatically. If a future change reintroduces a hex literal
    // inline this assertion catches it before CI does.
    expect(card.className).toMatch(/\bbg-surface\b/);
    expect(card.className).toMatch(/\btext-text-primary\b/);
    expect(card.getAttribute("style") ?? "").not.toMatch(/#[0-9a-fA-F]{6}/);

    expect(screen.getByTestId("tour-next").className).toMatch(/\bbg-accent\b/);
    expect(screen.getByTestId("tour-next").className).toMatch(
      /\btext-accent-text\b/,
    );
  });
});
