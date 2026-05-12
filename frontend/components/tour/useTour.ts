"use client";

/**
 * useTour — stub hook exposing the contract the L3.3 Onboarding team
 * will implement.
 *
 * In this PR, the hook is intentionally inert: `isActive` is always
 * false, navigation methods are no-ops. The contract exists so the
 * tour-engine implementation can land later without churning the
 * components that already call `useTour()`.
 *
 * Step IDs are the same dot-namespaced strings used by TourAnchor's
 * `id` prop, e.g. "dashboard.balance-tile".
 */

export interface TourApi {
  /** True while the tour is in progress. Always false in the stub. */
  isActive: boolean;
  /** Current step id or null. Always null in the stub. */
  currentStep: string | null;
  /** Total step count for the current tour run. Always 0 in the stub. */
  totalSteps: number;
  /** Begin the tour. No-op in the stub. */
  start: (firstStep?: string) => void;
  /** Advance to the next step. No-op in the stub. */
  next: () => void;
  /** Step back. No-op in the stub. */
  prev: () => void;
  /** Close / cancel the tour. No-op in the stub. */
  close: () => void;
}

const noop = () => {};

export function useTour(): TourApi {
  return {
    isActive: false,
    currentStep: null,
    totalSteps: 0,
    start: noop,
    next: noop,
    prev: noop,
    close: noop,
  };
}

export default useTour;
