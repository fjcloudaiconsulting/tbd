import { act, renderHook } from "@testing-library/react";
import { useIsMobile } from "@/lib/hooks/use-is-mobile";

// Mirror the matchMedia stub used in reports-editor-page.test.tsx so the
// hook tests run consistently in jsdom (which ships without matchMedia).

type MediaQueryListener = () => void;

function mockMatchMedia(isSmall: boolean) {
  const listeners: MediaQueryListener[] = [];
  const mq = {
    matches: isSmall,
    media: "(max-width: 639px)",
    onchange: null,
    addEventListener: vi.fn((_: string, cb: MediaQueryListener) => {
      listeners.push(cb);
    }),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
    // Helper to fire a "change" event (simulates viewport resize).
    _fire: () => listeners.forEach((cb) => cb()),
    // Allow tests to flip the matches value before firing.
    _setMatches: (v: boolean) => { mq.matches = v; },
  };
  window.matchMedia = vi.fn().mockReturnValue(mq);
  return mq;
}

describe("useIsMobile", () => {
  beforeEach(() => {
    // @ts-expect-error -- clear any stub a prior test installed
    delete window.matchMedia;
  });

  it("returns false by default (SSR-safe: no matchMedia in jsdom without stub)", () => {
    // No matchMedia installed → SSR-safe default.
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });

  it("returns true when matchMedia matches the small-screen query", () => {
    mockMatchMedia(true);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);
  });

  it("returns false when matchMedia does not match the small-screen query", () => {
    mockMatchMedia(false);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });

  it("updates when a change event fires (viewport resize across the breakpoint)", () => {
    const mq = mockMatchMedia(false);
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);

    // Simulate a resize that crosses into the small-screen breakpoint.
    act(() => {
      mq._setMatches(true);
      mq._fire();
    });
    expect(result.current).toBe(true);

    // Simulate resize back to desktop.
    act(() => {
      mq._setMatches(false);
      mq._fire();
    });
    expect(result.current).toBe(false);
  });

  it("subscribes with addEventListener('change', …) on the media query object", () => {
    const mq = mockMatchMedia(false);
    renderHook(() => useIsMobile());
    expect(mq.addEventListener).toHaveBeenCalledWith("change", expect.any(Function));
  });

  it("removes the event listener on unmount", () => {
    const mq = mockMatchMedia(false);
    const { unmount } = renderHook(() => useIsMobile());
    unmount();
    expect(mq.removeEventListener).toHaveBeenCalledWith("change", expect.any(Function));
  });
});
