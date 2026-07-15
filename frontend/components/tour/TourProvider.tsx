"use client";

/**
 * TourProvider — wires the L3.3 tour engine and renders the overlay.
 *
 * Mounted ABOVE AuthProvider in the root layout would be wrong: the
 * overlay needs auth state to know the current user. Mounted INSIDE
 * AuthProvider but outside the page tree means every authenticated
 * page can call ``useTour()`` and the overlay renders as a portal at
 * ``document.body`` so it escapes any scroll/clip ancestor.
 *
 * Two responsibilities:
 *   1. Provide the engine via context (so ``useTour()`` is non-stub).
 *   2. Mount ``<TourOverlay>`` which paints a backdrop and a card
 *      pointed at the current step's ``data-tour-id`` anchor.
 *
 * The overlay reads anchor positions in a layout effect and on resize.
 * If the anchor is not in the DOM (e.g. the user navigated away
 * mid-tour) the engine auto-skips to the next step. This keeps the
 * tour resilient against route changes without resorting to portals
 * scoped to each page.
 */
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { usePathname, useRouter } from "next/navigation";

import {
  DASHBOARD_TOUR_STEPS,
  EXTENDED_TOUR_STEPS,
  STEP_COPY,
  TOUR_FLAG_KEY,
  TOUR_FLAG_VALUE_DASHBOARD,
  TOUR_FLAG_VALUE_EXTENDED,
  pagePrefix,
  routeForPrefix,
} from "@/lib/help/tour";
import {
  safeTourStorageGet,
  safeTourStorageRemove,
} from "@/lib/help/tourStorage";

import {
  TourContext,
  useTourEngine,
  type TourApi,
} from "./useTour";

function DashboardTourAutoStart({ api }: { api: TourApi }) {
  const pathname = usePathname();
  const pendingStart = api.pendingStart;
  useEffect(() => {
    if (pathname !== "/dashboard") return;

    // Two sources, in priority order:
    //   1. The context pending start (storage-independent). Set by the
    //      replay button / RestartTourCard; survives client nav and
    //      Safari private mode where sessionStorage writes throw.
    //   2. The stored flag (via the safe accessor), for the case where a
    //      full page reload happened between the click and this mount (the
    //      context state would have been lost). The safe accessor reads
    //      sessionStorage when available and an in-memory fallback when it
    //      is not, so this path also works within a Safari-private-mode
    //      session where sessionStorage throws.
    // We consume whichever is present, clear BOTH, and start once.
    let steps: string[] | null = null;

    if (pendingStart && pendingStart.length) {
      steps = pendingStart;
    } else {
      const flag = safeTourStorageGet(TOUR_FLAG_KEY);
      if (flag === TOUR_FLAG_VALUE_EXTENDED) {
        steps = EXTENDED_TOUR_STEPS;
      } else if (flag === TOUR_FLAG_VALUE_DASHBOARD) {
        steps = DASHBOARD_TOUR_STEPS;
      }
    }

    if (!steps) return;

    // Clear the flag regardless of which source we used so a later
    // dashboard mount cannot start a second time. Clears both the
    // sessionStorage entry and the in-memory fallback.
    safeTourStorageRemove(TOUR_FLAG_KEY);

    // Defer one tick so the dashboard's TourAnchor DOM is mounted
    // before the engine measures positions. start() clears the context
    // pendingStart, so the two sources can never double-start.
    const t = window.setTimeout(() => {
      api.start(steps);
    }, 100);
    return () => window.clearTimeout(t);
  }, [pathname, pendingStart, api]);
  return null;
}

/**
 * Watches the active step's page prefix and pushes the router when
 * the user is on a different surface. The overlay's anchor-missing
 * fallback would otherwise auto-skip past every off-route step.
 * Only fires while the tour is active.
 */
function TourRouter({ api }: { api: TourApi }) {
  const router = useRouter();
  const pathname = usePathname();
  const currentStep = api.currentStep;
  useEffect(() => {
    if (!api.isActive) return;
    if (!currentStep) return;
    const route = routeForPrefix(pagePrefix(currentStep));
    if (!route) {
      // Unknown prefix means the tour authoring drifted from the
      // route map. The overlay will auto-skip when the anchor isn't
      // found, but surfacing it as a console warning makes the
      // missing entry visible in CI smoke runs / E2E logs instead of
      // silently no-opping.
      console.warn(
        `[tour] no route mapped for step prefix "${pagePrefix(currentStep)}" ` +
          `(step="${currentStep}"). Add an entry to routeForPrefix() in lib/help/tour.ts.`,
      );
      return;
    }
    if (pathname === route) return;
    router.push(route);
  }, [api.isActive, currentStep, pathname, router]);
  return null;
}

interface AnchorRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

function getAnchorRect(stepId: string | null): AnchorRect | null {
  if (!stepId || typeof document === "undefined") return null;
  const el = document.querySelector<HTMLElement>(
    `[data-tour-id="${CSS.escape(stepId)}"]`,
  );
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { top: r.top, left: r.left, width: r.width, height: r.height };
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => setReduced(mql.matches);
    apply();
    mql.addEventListener?.("change", apply);
    return () => mql.removeEventListener?.("change", apply);
  }, []);
  return reduced;
}

function TourOverlay({ api }: { api: TourApi }) {
  const reducedMotion = usePrefersReducedMotion();
  const pathname = usePathname();
  const [rect, setRect] = useState<AnchorRect | null>(null);
  const [mounted, setMounted] = useState(false);

  // eslint-disable-next-line react-hooks/set-state-in-effect -- flag mounted after first commit so the overlay portal renders client-side only (avoids SSR hydration mismatch)
  useEffect(() => setMounted(true), []);

  const recompute = useCallback(() => {
    setRect(getAnchorRect(api.currentStep));
  }, [api.currentStep]);

  useLayoutEffect(() => {
    if (!api.isActive) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- measure the active tour anchor's rect after layout so the highlight/card position correctly (recompute -> setRect)
    recompute();
  }, [api.isActive, api.currentStep, recompute]);

  // Bring the active step's anchor into view when it isn't fully visible.
  // Anchors can sit below the fold (e.g. the recent-transactions dashboard
  // tile at grid y=19), and the overlay positions its highlight and card from
  // getBoundingClientRect — a viewport-relative measurement — so an off-screen
  // anchor would render the step below the fold. We scroll only when the
  // element is not already fully visible, so on-screen steps (the header,
  // period nav) are never yanked out of place. The `scroll` listener below
  // then refreshes the rect as the scroll settles.
  useEffect(() => {
    if (!api.isActive || !api.currentStep) return;
    const el = document.querySelector<HTMLElement>(
      `[data-tour-id="${CSS.escape(api.currentStep)}"]`,
    );
    if (!el) return;
    const r = el.getBoundingClientRect();
    const fullyVisible = r.top >= 0 && r.bottom <= window.innerHeight;
    if (fullyVisible) return;
    el.scrollIntoView({
      block: "center",
      inline: "nearest",
      behavior: reducedMotion ? "auto" : "smooth",
    });
  }, [api.isActive, api.currentStep, reducedMotion]);

  useEffect(() => {
    if (!api.isActive) return;
    const onResize = () => recompute();
    window.addEventListener("resize", onResize);
    window.addEventListener("scroll", onResize, true);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("scroll", onResize, true);
    };
  }, [api.isActive, recompute]);

  // If the active step has no anchor in the DOM (route changed,
  // element removed), advance after a short grace so flicker does
  // not stall the user.
  //
  // BUT: when a cross-page step is in flight, TourRouter has just
  // pushed the new route and we're waiting for the destination page
  // to mount its `data-tour-id` anchors. Auto-skipping during that
  // window would silently advance past every step on a slow page.
  // Suspend auto-skip until the pathname matches the step's expected
  // route, then poll on a longer grace (800ms) to absorb the
  // hydration + data-fetch tail.
  useEffect(() => {
    if (!api.isActive) return;
    if (rect) return;
    if (!api.currentStep) return;
    const expectedRoute = routeForPrefix(pagePrefix(api.currentStep));
    // If we know the step belongs to a different route than we're on,
    // don't auto-skip — TourRouter will move us there and the anchor
    // will appear shortly.
    if (expectedRoute && pathname !== expectedRoute) return;
    const t = window.setTimeout(() => {
      const fresh = getAnchorRect(api.currentStep);
      if (!fresh) api.next();
    }, 800);
    return () => window.clearTimeout(t);
  }, [api, rect, pathname]);

  // Keyboard nav. The overlay is informative (aria-modal="false") so
  // we don't trap focus, but the tour itself has to be fully usable
  // from the keyboard:
  //   - Escape closes
  //   - ArrowRight advances (matches "Next" button)
  //   - ArrowLeft goes back (matches "Back" button)
  // We deliberately leave Tab/Enter/Space to the browser's default
  // button focus handling so the card's own buttons keep their
  // standard semantics.
  useEffect(() => {
    if (!api.isActive) return;
    const isEditableTarget = (target: EventTarget | null): boolean => {
      // Arrow keys belong to inputs, textareas, selects, contenteditables
      // — for cursor movement, radio/listbox navigation, etc. Hijacking
      // them while the user is mid-keystroke breaks the underlying page.
      // Escape stays global; arrow advance/back only fires when focus is
      // outside an editable element.
      if (!(target instanceof HTMLElement)) return false;
      // Real browsers compute isContentEditable from the attribute, but
      // jsdom does not — so also check the raw attribute as a fallback
      // so tests stay honest.
      if (target.isContentEditable) return true;
      const ce = target.getAttribute("contenteditable");
      if (ce === "" || ce === "true" || ce === "plaintext-only") return true;
      const tag = target.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        api.close();
        return;
      }
      if (isEditableTarget(e.target)) return;
      if (e.key === "ArrowRight") {
        api.next();
        return;
      }
      if (e.key === "ArrowLeft") {
        api.prev();
        return;
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [api]);

  if (!mounted) return null;
  if (!api.isActive) return null;

  const copy = api.currentStep ? STEP_COPY[api.currentStep] : null;
  const stepLabel = `Step ${api.currentIndex + 1} of ${api.totalSteps}`;

  // Position the card under the anchor. If the anchor is offscreen or
  // missing, center the card.
  // Clamp the card's top so a tall or near-bottom anchor can't push the card
  // below the fold. ESTIMATED_CARD_HEIGHT reserves room for the tallest step
  // copy; the lower bound keeps it clear of the top edge.
  const ESTIMATED_CARD_HEIGHT = 220;
  const cardStyle = rect
    ? {
        top: Math.max(
          16,
          Math.min(
            rect.top + rect.height + 12,
            window.innerHeight - ESTIMATED_CARD_HEIGHT,
          ),
        ),
        left: Math.max(16, Math.min(rect.left, window.innerWidth - 360)),
      }
    : {
        top: window.innerHeight / 2 - 100,
        left: window.innerWidth / 2 - 180,
      };

  const transition = reducedMotion ? "none" : "all 150ms ease-out";

  return createPortal(
    <div
      className="tour-overlay-root"
      role="dialog"
      aria-modal="false"
      aria-label="Product tour"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        pointerEvents: "none",
      }}
    >
      {/* Soft backdrop. pointer-events stays off so the underlying UI
          remains interactive: the tour is informative, not modal. The
          scrim color comes from a theme token so it darkens
          appropriately in light vs dark. */}
      <div
        className="absolute inset-0 bg-scrim"
        style={{ transition }}
      />
      {rect ? (
        <div
          aria-hidden
          className="absolute rounded-[10px] border-2 border-warning pointer-events-none"
          style={{
            top: rect.top - 6,
            left: rect.left - 6,
            width: rect.width + 12,
            height: rect.height + 12,
            transition,
          }}
        />
      ) : null}
      <div
        role="region"
        aria-live="polite"
        data-testid="tour-card"
        className="absolute w-[min(340px,calc(100vw-2rem))] rounded-xl bg-surface text-text-primary shadow-card p-5 pointer-events-auto border border-border"
        style={{
          transition,
          ...cardStyle,
        }}
      >
        <div className="mb-1.5 text-xs uppercase tracking-[0.06em] text-text-muted">
          {stepLabel}
        </div>
        <div className="mb-2 text-lg font-semibold text-text-primary">
          {copy?.title ?? "Tour"}
        </div>
        <div className="mb-4 text-sm leading-relaxed text-text-secondary">
          {copy?.body ?? ""}
        </div>
        <div className="flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={api.close}
            className="border-0 bg-transparent px-2 py-1.5 text-[13px] text-text-muted hover:text-text-primary cursor-pointer"
            data-testid="tour-skip"
          >
            Skip tour
          </button>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={api.prev}
              disabled={api.currentIndex <= 0}
              className="rounded-lg border border-border bg-transparent px-3 py-1.5 text-[13px] text-text-primary hover:bg-surface-raised disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer"
              data-testid="tour-prev"
            >
              Back
            </button>
            {api.currentIndex === api.totalSteps - 1 ? (
              <button
                type="button"
                onClick={api.finish}
                className="rounded-lg border-0 bg-accent px-4 py-1.5 text-[13px] font-medium text-accent-text hover:bg-accent-hover cursor-pointer"
                data-testid="tour-finish"
              >
                Done
              </button>
            ) : (
              <button
                type="button"
                onClick={api.next}
                className="rounded-lg border-0 bg-accent px-4 py-1.5 text-[13px] font-medium text-accent-text hover:bg-accent-hover cursor-pointer"
                data-testid="tour-next"
              >
                Next
              </button>
            )}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function TourProvider({ children }: { children: React.ReactNode }) {
  const api = useTourEngine();
  return (
    <TourContext.Provider value={api}>
      {children}
      <DashboardTourAutoStart api={api} />
      <TourRouter api={api} />
      <TourOverlay api={api} />
    </TourContext.Provider>
  );
}

export default TourProvider;
