/**
 * TourProvider — scroll-into-view for off-screen anchors (PR #511).
 *
 * Phase 2b anchored dashboard tiles that can sit below the fold (the
 * recent-transactions tile at grid y=19). The overlay positions its highlight
 * and card from getBoundingClientRect, which is viewport-relative, so an
 * off-screen anchor would render the step below the fold. TourOverlay must
 * scroll a not-fully-visible anchor into view, and must NOT disturb an anchor
 * that is already fully visible (e.g. the header, step 1).
 *
 * jsdom returns a zero rect from getBoundingClientRect by default, so each
 * test stubs the anchor's rect to force the intended branch.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  usePathname: () => "/dashboard",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import { TourProvider } from "@/components/tour/TourProvider";
import { useTour } from "@/components/tour/useTour";

function Starter({ steps }: { steps: string[] }) {
  const tour = useTour();
  return (
    <button type="button" data-testid="starter" onClick={() => tour.start(steps)}>
      start
    </button>
  );
}

let scrollSpy: ReturnType<typeof vi.fn>;

function placeAnchor(
  id: string,
  rect: { top: number; bottom: number; left?: number; width?: number; height?: number },
) {
  const el = document.createElement("div");
  el.setAttribute("data-tour-id", id);
  const full = {
    top: rect.top,
    bottom: rect.bottom,
    left: rect.left ?? 0,
    right: (rect.left ?? 0) + (rect.width ?? 200),
    width: rect.width ?? 200,
    height: rect.height ?? rect.bottom - rect.top,
    x: rect.left ?? 0,
    y: rect.top,
    toJSON: () => ({}),
  } as DOMRect;
  el.getBoundingClientRect = () => full;
  document.body.appendChild(el);
  return el;
}

beforeEach(() => {
  scrollSpy = vi.fn();
  // jsdom does not implement scrollIntoView; install a spy on the prototype.
  Element.prototype.scrollIntoView = scrollSpy as unknown as typeof Element.prototype.scrollIntoView;
  window.innerHeight = 768;
});

afterEach(() => {
  document.querySelectorAll("[data-tour-id]").forEach((n) => n.remove());
  vi.restoreAllMocks();
});

function startTour(steps: string[]) {
  render(
    <TourProvider>
      <Starter steps={steps} />
    </TourProvider>,
  );
  act(() => {
    screen.getByTestId("starter").click();
  });
}

describe("TourProvider scroll-into-view", () => {
  it("scrolls a below-the-fold anchor into view, centered", () => {
    placeAnchor("dashboard.recent-transactions", { top: 2000, bottom: 2400 });
    startTour(["dashboard.recent-transactions"]);

    expect(scrollSpy).toHaveBeenCalled();
    const arg = scrollSpy.mock.calls[0][0];
    expect(arg).toMatchObject({ block: "center" });
  });

  it("does NOT scroll an anchor that is already fully visible", () => {
    placeAnchor("dashboard.header", { top: 100, bottom: 140 });
    startTour(["dashboard.header"]);

    expect(scrollSpy).not.toHaveBeenCalled();
  });
});
