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
import { usePathname } from "next/navigation";

import {
  TourContext,
  useTourEngine,
  type TourApi,
} from "./useTour";

// Same sessionStorage key the onboarding wizard writes when the user
// opts into the post-wizard tour. We auto-start the dashboard tour
// when this is set AND the user lands on /dashboard. The flag is
// cleared on start so a reload does not re-trigger.
const TOUR_FLAG_KEY = "tbd-pending-dashboard-tour";

const DASHBOARD_TOUR_STEPS = [
  "dashboard.header",
  "dashboard.import-cta",
  "dashboard.period-nav",
  "dashboard.on-track-tile",
  "dashboard.account-forecast",
];

function DashboardTourAutoStart({ api }: { api: TourApi }) {
  const pathname = usePathname();
  useEffect(() => {
    if (pathname !== "/dashboard") return;
    let flag: string | null = null;
    try {
      flag = window.sessionStorage.getItem(TOUR_FLAG_KEY);
    } catch {
      return;
    }
    if (flag !== "1") return;
    try {
      window.sessionStorage.removeItem(TOUR_FLAG_KEY);
    } catch {
      // best-effort
    }
    // Defer one tick so the dashboard's TourAnchor DOM is mounted
    // before the engine measures positions.
    const t = window.setTimeout(() => {
      api.start(DASHBOARD_TOUR_STEPS);
    }, 100);
    return () => window.clearTimeout(t);
  }, [pathname, api]);
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

interface TourStepCopy {
  title: string;
  body: string;
}

// Step copy in one place so the wizard team can tweak voice without
// chasing JSX. Keys match the dot-namespaced anchor ids the dashboard
// already wired through PR #226.
const STEP_COPY: Record<string, TourStepCopy> = {
  "dashboard.header": {
    title: "Welcome to your dashboard",
    body: "This is where you will see how the month is going at a glance. Net cashflow, balances, and what is coming up.",
  },
  "dashboard.import-cta": {
    title: "Bring in your transactions",
    body: "Import a bank export here, or add transactions one by one. The Better Decision works with whatever you have.",
  },
  "dashboard.period-nav": {
    title: "Move through periods",
    body: "Each month is its own billing period. Use these arrows to look back at history or peek ahead.",
  },
  "dashboard.on-track-tile": {
    title: "How the month is shaping up",
    body: "On Track tells you if your spending plan and your reality agree. Green means you are on it. Yellow means it is worth a look.",
  },
  "dashboard.account-forecast": {
    title: "Account forecast",
    body: "We project each account out to the end of the period using your recurring transactions and budgets.",
  },
};

function TourOverlay({ api }: { api: TourApi }) {
  const reducedMotion = usePrefersReducedMotion();
  const [rect, setRect] = useState<AnchorRect | null>(null);
  const [mounted, setMounted] = useState(false);

  useEffect(() => setMounted(true), []);

  const recompute = useCallback(() => {
    setRect(getAnchorRect(api.currentStep));
  }, [api.currentStep]);

  useLayoutEffect(() => {
    if (!api.isActive) return;
    recompute();
  }, [api.isActive, api.currentStep, recompute]);

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
  // not stall the user. 200ms is enough for a Next.js client nav.
  useEffect(() => {
    if (!api.isActive) return;
    if (rect) return;
    const t = window.setTimeout(() => {
      const fresh = getAnchorRect(api.currentStep);
      if (!fresh) api.next();
    }, 200);
    return () => window.clearTimeout(t);
  }, [api, rect]);

  if (!mounted) return null;
  if (!api.isActive) return null;

  const copy = api.currentStep ? STEP_COPY[api.currentStep] : null;
  const stepLabel = `Step ${api.currentIndex + 1} of ${api.totalSteps}`;

  // Position the card under the anchor. If the anchor is offscreen or
  // missing, center the card.
  const cardStyle = rect
    ? {
        top: Math.max(16, rect.top + rect.height + 12),
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
        className="absolute w-[340px] rounded-xl bg-surface text-text-primary shadow-card p-5 pointer-events-auto border border-border"
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
      <TourOverlay api={api} />
    </TourContext.Provider>
  );
}

export default TourProvider;
