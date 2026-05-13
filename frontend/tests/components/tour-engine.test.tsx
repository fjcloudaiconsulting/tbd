/**
 * Tour-engine state machine tests (L3.3).
 *
 * Verifies the useTourEngine hook implements the documented state
 * machine (idle → active → finished) and the navigation primitives
 * (next, prev, close, finish, reset) correctly.
 */
import { describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useTourEngine } from "@/components/tour/useTour";

describe("useTourEngine", () => {
  it("starts in the idle phase with no current step", () => {
    const { result } = renderHook(() => useTourEngine());
    expect(result.current.phase).toBe("idle");
    expect(result.current.isActive).toBe(false);
    expect(result.current.currentStep).toBeNull();
    expect(result.current.totalSteps).toBe(0);
  });

  it("transitions idle -> active on start() with steps", () => {
    const { result } = renderHook(() => useTourEngine());
    act(() => {
      result.current.start(["a", "b", "c"]);
    });
    expect(result.current.phase).toBe("active");
    expect(result.current.isActive).toBe(true);
    expect(result.current.currentStep).toBe("a");
    expect(result.current.currentIndex).toBe(0);
    expect(result.current.totalSteps).toBe(3);
  });

  it("ignores start() when given an empty step list", () => {
    const { result } = renderHook(() => useTourEngine());
    act(() => {
      result.current.start([]);
    });
    expect(result.current.phase).toBe("idle");
  });

  it("next() advances through steps and transitions to finished at the end", () => {
    const { result } = renderHook(() => useTourEngine());
    act(() => {
      result.current.start(["a", "b"]);
    });
    act(() => {
      result.current.next();
    });
    expect(result.current.currentStep).toBe("b");
    expect(result.current.currentIndex).toBe(1);
    act(() => {
      result.current.next();
    });
    expect(result.current.phase).toBe("finished");
    expect(result.current.isActive).toBe(false);
    expect(result.current.currentStep).toBeNull();
  });

  it("prev() backs up but never below index 0", () => {
    const { result } = renderHook(() => useTourEngine());
    act(() => {
      result.current.start(["a", "b"]);
    });
    act(() => {
      result.current.prev();
    });
    expect(result.current.currentIndex).toBe(0);
    act(() => {
      result.current.next();
      result.current.prev();
    });
    expect(result.current.currentIndex).toBe(0);
  });

  it("close() moves to finished without finishing all steps", () => {
    const { result } = renderHook(() => useTourEngine());
    act(() => {
      result.current.start(["a", "b", "c"]);
    });
    act(() => {
      result.current.close();
    });
    expect(result.current.phase).toBe("finished");
    expect(result.current.currentStep).toBeNull();
  });

  it("reset() returns from finished back to idle", () => {
    const { result } = renderHook(() => useTourEngine());
    act(() => {
      result.current.start(["a"]);
      result.current.finish();
    });
    expect(result.current.phase).toBe("finished");
    act(() => {
      result.current.reset();
    });
    expect(result.current.phase).toBe("idle");
    expect(result.current.totalSteps).toBe(0);
  });

  it("next() and prev() are no-ops when not active", () => {
    const { result } = renderHook(() => useTourEngine());
    act(() => {
      result.current.next();
      result.current.prev();
    });
    expect(result.current.phase).toBe("idle");
  });
});
