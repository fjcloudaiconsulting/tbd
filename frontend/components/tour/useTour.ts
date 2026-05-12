"use client";

/**
 * useTour — first-run product tour engine (L3.3).
 *
 * State machine:
 *
 *   idle ──start()──▶ active ──finish()/close()──▶ finished
 *      ▲                │
 *      └────reset()─────┘  (only from finished or when the same tour
 *                          is re-started fresh)
 *
 * The engine keeps a list of step ids (dot-namespaced, matching
 * ``data-tour-id`` on the DOM). The active step index walks forward
 * via ``next()`` and backward via ``prev()``. ``close()`` and
 * ``finish()`` both move to ``finished`` and clear the step pointer
 * so the overlay can unmount; we keep them distinct so consumers can
 * tell "user dismissed" vs "user completed all steps".
 *
 * Reduced-motion: the OverlayCard component (separate file) reads
 * ``prefers-reduced-motion`` and disables transitions; the engine
 * itself is render-only.
 *
 * No localStorage persistence at this layer — the onboarding wizard
 * owns "should we start the tour" via the server-side
 * ``users.onboarded_at`` flag. ``useTour`` is a transient UI state
 * machine: when the user reloads mid-tour they will not resume,
 * which is a deliberate simplification for the first-run case.
 */
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";

export type TourPhase = "idle" | "active" | "finished";

export interface TourApi {
  /** True while the tour is in progress (phase === "active"). */
  isActive: boolean;
  /** Current phase. */
  phase: TourPhase;
  /** Current step id or null when not active. */
  currentStep: string | null;
  /** Zero-based index into the step list. -1 when not active. */
  currentIndex: number;
  /** Total step count for the current tour run. */
  totalSteps: number;
  /** Begin the tour with the given step ids. */
  start: (steps: string[]) => void;
  /** Advance to the next step, finishing if at the end. */
  next: () => void;
  /** Step back (no-op when already at index 0). */
  prev: () => void;
  /** Close / cancel the tour (sets phase=finished). */
  close: () => void;
  /** Move directly to phase=finished, callable from the last step's "Done" button. */
  finish: () => void;
  /** Reset back to phase=idle so a future start() takes effect. */
  reset: () => void;
}

interface TourState {
  phase: TourPhase;
  steps: string[];
  index: number;
}

const INITIAL_STATE: TourState = { phase: "idle", steps: [], index: -1 };

const TourContext = createContext<TourApi | null>(null);

/**
 * Internal hook that produces a fresh TourApi tied to local state.
 * Exported for tests; production code mounts <TourProvider> at the
 * root and consumes via the context hook.
 */
export function useTourEngine(): TourApi {
  const [state, setState] = useState<TourState>(INITIAL_STATE);
  // Guard re-entrancy: start() called twice in the same tick should
  // not split state across two renders.
  const guard = useRef(false);

  const start = useCallback((steps: string[]) => {
    if (guard.current) return;
    if (!steps.length) return;
    guard.current = true;
    setState({ phase: "active", steps, index: 0 });
    queueMicrotask(() => {
      guard.current = false;
    });
  }, []);

  const next = useCallback(() => {
    setState((s) => {
      if (s.phase !== "active") return s;
      const nextIdx = s.index + 1;
      if (nextIdx >= s.steps.length) {
        return { phase: "finished", steps: s.steps, index: -1 };
      }
      return { ...s, index: nextIdx };
    });
  }, []);

  const prev = useCallback(() => {
    setState((s) => {
      if (s.phase !== "active") return s;
      if (s.index <= 0) return s;
      return { ...s, index: s.index - 1 };
    });
  }, []);

  const close = useCallback(() => {
    setState((s) => {
      if (s.phase !== "active") return s;
      return { phase: "finished", steps: s.steps, index: -1 };
    });
  }, []);

  const finish = useCallback(() => {
    setState((s) => ({ phase: "finished", steps: s.steps, index: -1 }));
  }, []);

  const reset = useCallback(() => {
    setState(INITIAL_STATE);
  }, []);

  return useMemo<TourApi>(
    () => ({
      isActive: state.phase === "active",
      phase: state.phase,
      currentStep:
        state.phase === "active" && state.index >= 0
          ? state.steps[state.index] ?? null
          : null,
      currentIndex: state.phase === "active" ? state.index : -1,
      totalSteps: state.steps.length,
      start,
      next,
      prev,
      close,
      finish,
      reset,
    }),
    [state, start, next, prev, close, finish, reset],
  );
}

export function useTour(): TourApi {
  const ctx = useContext(TourContext);
  if (ctx) return ctx;
  // Soft fallback for components rendered outside a TourProvider (e.g.
  // marketing pages). Matches the stub contract: every method is a no-op.
  return {
    isActive: false,
    phase: "idle",
    currentStep: null,
    currentIndex: -1,
    totalSteps: 0,
    start: () => {},
    next: () => {},
    prev: () => {},
    close: () => {},
    finish: () => {},
    reset: () => {},
  };
}

export { TourContext };
export default useTour;
