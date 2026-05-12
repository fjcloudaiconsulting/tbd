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
          remains interactive — the tour is informative, not modal. */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          background: "rgba(15, 23, 42, 0.35)",
          transition,
        }}
      />
      {rect ? (
        <div
          aria-hidden
          style={{
            position: "absolute",
            top: rect.top - 6,
            left: rect.left - 6,
            width: rect.width + 12,
            height: rect.height + 12,
            border: "2px solid #f59e0b",
            borderRadius: 10,
            boxShadow: "0 0 0 9999px rgba(0,0,0,0)",
            transition,
            pointerEvents: "none",
          }}
        />
      ) : null}
      <div
        role="region"
        aria-live="polite"
        data-testid="tour-card"
        style={{
          position: "absolute",
          width: 340,
          background: "white",
          color: "#0f172a",
          borderRadius: 12,
          boxShadow: "0 18px 48px rgba(15,23,42,0.25)",
          padding: 20,
          pointerEvents: "auto",
          transition,
          ...cardStyle,
        }}
      >
        <div
          style={{
            fontSize: 12,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            color: "#64748b",
            marginBottom: 6,
          }}
        >
          {stepLabel}
        </div>
        <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 8 }}>
          {copy?.title ?? "Tour"}
        </div>
        <div style={{ fontSize: 14, lineHeight: 1.5, marginBottom: 16 }}>
          {copy?.body ?? ""}
        </div>
        <div
          style={{
            display: "flex",
            gap: 8,
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <button
            type="button"
            onClick={api.close}
            style={{
              background: "transparent",
              border: 0,
              color: "#64748b",
              fontSize: 13,
              cursor: "pointer",
              padding: "6px 8px",
            }}
            data-testid="tour-skip"
          >
            Skip tour
          </button>
          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              onClick={api.prev}
              disabled={api.currentIndex <= 0}
              style={{
                background: "transparent",
                border: "1px solid #cbd5f5",
                borderRadius: 8,
                padding: "6px 12px",
                fontSize: 13,
                cursor: api.currentIndex <= 0 ? "not-allowed" : "pointer",
                color: "#0f172a",
              }}
              data-testid="tour-prev"
            >
              Back
            </button>
            {api.currentIndex === api.totalSteps - 1 ? (
              <button
                type="button"
                onClick={api.finish}
                style={{
                  background: "#0f172a",
                  color: "white",
                  border: 0,
                  borderRadius: 8,
                  padding: "6px 16px",
                  fontSize: 13,
                  cursor: "pointer",
                }}
                data-testid="tour-finish"
              >
                Done
              </button>
            ) : (
              <button
                type="button"
                onClick={api.next}
                style={{
                  background: "#0f172a",
                  color: "white",
                  border: 0,
                  borderRadius: 8,
                  padding: "6px 16px",
                  fontSize: 13,
                  cursor: "pointer",
                }}
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
