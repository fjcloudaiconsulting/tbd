/**
 * Storage-independent tour replay start (fix/tour-storage-independent-start).
 *
 * The "Replay product tour" flow used to depend solely on a
 * sessionStorage flag: the replay button wrote it, then
 * DashboardTourAutoStart read it on /dashboard. When sessionStorage is
 * unavailable (Safari private mode, disabled storage) the write threw,
 * was swallowed, and the tour silently never started.
 *
 * These tests pin the fix:
 *   - The context "pending start" path starts the tour on /dashboard
 *     even when no sessionStorage flag was written (and when the write
 *     throws).
 *   - The tour starts exactly once when BOTH a context pending start
 *     and a sessionStorage flag are present (no double-start).
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TourProvider } from "@/components/tour/TourProvider";
import { useTour } from "@/components/tour/useTour";
import {
  EXTENDED_TOUR_STEPS,
  TOUR_FLAG_KEY,
  TOUR_FLAG_VALUE_EXTENDED,
} from "@/lib/help/tour";

let mockPathname = "/settings";

vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
  useRouter: () => ({
    push: (route: string) => {
      mockPathname = route;
    },
  }),
}));

/** Mirrors the AppShell replay button's primary context path. */
function ReplayButton() {
  const api = useTour();
  return (
    <button
      data-testid="replay"
      onClick={() => api.requestStart(EXTENDED_TOUR_STEPS)}
    >
      replay
    </button>
  );
}

/** Reports the engine's current total step count for assertions. */
function StepProbe() {
  const api = useTour();
  return <div data-testid="total-steps">{api.totalSteps}</div>;
}

beforeEach(() => {
  mockPathname = "/settings";
  try {
    window.sessionStorage.clear();
  } catch {
    // ignore
  }
  // The extended tour's first step targets the dashboard header; plant
  // its anchor so the overlay measures a real rect.
  const anchor = document.createElement("div");
  anchor.setAttribute("data-tour-id", EXTENDED_TOUR_STEPS[0]);
  document.body.appendChild(anchor);
});

afterEach(() => {
  document.querySelectorAll("[data-tour-id]").forEach((el) => el.remove());
  vi.useRealTimers();
});

describe("storage-independent tour replay", () => {
  it("starts the tour on /dashboard via the context pending path (sessionStorage available)", async () => {
    vi.useFakeTimers();
    render(
      <TourProvider>
        <ReplayButton />
      </TourProvider>,
    );
    // Click stages the context pending start, then the replay flow
    // navigates to /dashboard (mock router flips the pathname).
    act(() => {
      fireEvent.click(screen.getByTestId("replay"));
      mockPathname = "/dashboard";
    });
    // A second click re-renders so DashboardTourAutoStart's effect sees
    // the new pathname (re-staging the same list is last-write-wins, so
    // the pending start is unchanged).
    act(() => {
      fireEvent.click(screen.getByTestId("replay"));
    });
    await act(async () => {
      // Past the ~100ms anchor-mount defer.
      vi.advanceTimersByTime(200);
    });
    expect(screen.getByTestId("tour-card")).toBeTruthy();
  });

  it("starts the tour even when sessionStorage.setItem THROWS (Safari private mode)", async () => {
    vi.useFakeTimers();
    const setItemSpy = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("QuotaExceededError");
      });
    try {
      render(
        <TourProvider>
          <ReplayButton />
        </TourProvider>,
      );
      act(() => {
        fireEvent.click(screen.getByTestId("replay"));
        mockPathname = "/dashboard";
      });
      act(() => {
        fireEvent.click(screen.getByTestId("replay"));
      });
      await act(async () => {
        vi.advanceTimersByTime(200);
      });
      // With sessionStorage broken the OLD code would never start; the
      // context path must carry it.
      expect(screen.getByTestId("tour-card")).toBeTruthy();
    } finally {
      setItemSpy.mockRestore();
    }
  });

  it("starts exactly once when both a context pending start and a sessionStorage flag are present", async () => {
    vi.useFakeTimers();
    // The card is a singleton overlay and both sources resolve to the
    // same step list, so "one card / right step count" can't actually
    // prove start() ran once. Instead, count how many times the consume
    // effect's deferred-start callback actually FIRES.
    //
    // DashboardTourAutoStart schedules window.setTimeout(api.start, 100)
    // when it consumes a source, and clears it in cleanup before
    // re-scheduling, so across the two renders only one callback
    // survives to execute. We wrap each ~100ms timer's callback in a
    // counter: a regression that fired start twice (e.g. consuming BOTH
    // sources, or scheduling a second un-cleared timer) would execute
    // two deferred-start callbacks and trip this assertion. Counting
    // executions (not schedules) ignores the cleaned-up timer.
    let deferredStartFires = 0;
    const realSetTimeout = window.setTimeout.bind(window);
    const setTimeoutSpy = vi
      .spyOn(window, "setTimeout")
      .mockImplementation(((fn: TimerHandler, delay?: number, ...rest: unknown[]) => {
        if (delay === 100 && typeof fn === "function") {
          const wrapped = ((...args: unknown[]) => {
            deferredStartFires += 1;
            return (fn as (...a: unknown[]) => unknown)(...args);
          }) as TimerHandler;
          return realSetTimeout(wrapped, delay, ...rest);
        }
        return realSetTimeout(fn, delay as number, ...rest);
      }) as typeof window.setTimeout);

    render(
      <TourProvider>
        <ReplayButton />
        <StepProbe />
      </TourProvider>,
    );
    act(() => {
      // Seed the legacy flag directly (simulating a full reload that
      // also staged sessionStorage), plus stage the context pending
      // start via the replay button.
      try {
        window.sessionStorage.setItem(TOUR_FLAG_KEY, TOUR_FLAG_VALUE_EXTENDED);
      } catch {
        // ignore
      }
      fireEvent.click(screen.getByTestId("replay")); // context pending
      mockPathname = "/dashboard";
    });
    act(() => {
      fireEvent.click(screen.getByTestId("replay"));
    });
    await act(async () => {
      vi.advanceTimersByTime(200);
    });

    // The load-bearing guard: the deferred-start callback fired exactly
    // once. This bites if start() runs twice (e.g. both sources consumed,
    // or a second timer is scheduled without clearing the first).
    expect(deferredStartFires).toBe(1);
    // Exactly one card on screen — not two from a double-start.
    expect(screen.getAllByTestId("tour-card")).toHaveLength(1);
    // The extended tour has a fixed step count; a double-start that
    // re-entered with a different list would change this.
    expect(screen.getByTestId("total-steps").textContent).toBe(
      String(EXTENDED_TOUR_STEPS.length),
    );
    // Both sources consumed: the legacy flag must be cleared so a later
    // dashboard mount does not start a second time.
    expect(window.sessionStorage.getItem(TOUR_FLAG_KEY)).toBeNull();

    setTimeoutSpy.mockRestore();
  });
});
