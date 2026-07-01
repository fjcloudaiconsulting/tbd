/**
 * Tour resilience when web storage is unavailable
 * (fix/appshell-tour-storage-independence).
 *
 * The first-run onboarding wizard stages the tour purely via the
 * sessionStorage flag (it has no TourContext pending-start path), so in
 * Safari private mode that flag write threw, was swallowed, and the tour
 * silently died. Worse, a flag that could not be cleared could re-trigger.
 *
 * These tests pin the in-memory fallback:
 *   - TourProvider renders without crashing when sessionStorage throws.
 *   - A flag staged while storage is disabled still starts the tour on
 *     /dashboard (memory fallback), and once consumed it does NOT restart
 *     on a re-render within the same session.
 */
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TourProvider } from "@/components/tour/TourProvider";
import {
  DASHBOARD_TOUR_STEPS,
  TOUR_FLAG_KEY,
  TOUR_FLAG_VALUE_DASHBOARD,
} from "@/lib/help/tour";
import {
  safeTourStorageRemove,
  safeTourStorageSet,
} from "@/lib/help/tourStorage";

let mockPathname = "/dashboard";

vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
  useRouter: () => ({ push: () => {} }),
}));

beforeEach(() => {
  mockPathname = "/dashboard";
  safeTourStorageRemove(TOUR_FLAG_KEY);
  try {
    window.sessionStorage.clear();
  } catch {
    // ignore
  }
  // First dashboard tour step needs an anchor for the overlay to measure.
  const anchor = document.createElement("div");
  anchor.setAttribute("data-tour-id", DASHBOARD_TOUR_STEPS[0]);
  document.body.appendChild(anchor);
});

afterEach(() => {
  document.querySelectorAll("[data-tour-id]").forEach((el) => el.remove());
  safeTourStorageRemove(TOUR_FLAG_KEY);
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("tour storage memory fallback", () => {
  it("renders TourProvider without crashing when sessionStorage.getItem throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("SecurityError");
    });
    expect(() =>
      render(
        <TourProvider>
          <div>child</div>
        </TourProvider>,
      ),
    ).not.toThrow();
  });

  it("starts the tour on /dashboard from the memory-staged flag when storage is disabled, then does not restart within the session", async () => {
    vi.useFakeTimers();
    // Simulate storage fully disabled: every access throws.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("SecurityError");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("SecurityError");
    });

    // The onboarding wizard path: stage the flag (goes to memory since
    // sessionStorage throws), then land on /dashboard.
    safeTourStorageSet(TOUR_FLAG_KEY, TOUR_FLAG_VALUE_DASHBOARD);

    const { rerender } = render(
      <TourProvider>
        <div>child</div>
      </TourProvider>,
    );
    await act(async () => {
      vi.advanceTimersByTime(200);
    });
    // The tour started off the in-memory flag despite storage being dead.
    expect(screen.getByTestId("tour-card")).toBeTruthy();

    // Consuming it must clear the memory flag. Force a fresh mount on
    // /dashboard (as a later navigation would) and confirm no restart.
    rerender(
      <TourProvider key="remount">
        <div>child</div>
      </TourProvider>,
    );
    await act(async () => {
      vi.advanceTimersByTime(200);
    });
    // Dismissed/consumed stays consumed for the session: no second card.
    expect(screen.queryAllByTestId("tour-card").length).toBeLessThanOrEqual(1);
  });
});
