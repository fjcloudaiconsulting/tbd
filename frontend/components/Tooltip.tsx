"use client";

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import Link from "next/link";

/**
 * Tooltip — accessible contextual help bubble.
 *
 * Sits alongside HelpAnchor (which deep-links to /docs sections). Use a
 * Tooltip for the short, one-sentence "why does this exist" explainer and
 * include a "Learn more" link when there is a matching /docs anchor.
 *
 * Design contract (impeccable critique passed):
 *   - Trigger is a real focusable element. Default trigger is a small
 *     button with a "?" icon; callers can pass an explicit trigger via
 *     the `trigger` prop (must be a single ReactElement that accepts a
 *     ref and the standard DOM event handlers).
 *   - Hover, focus, click, AND keyboard all open it. Escape dismisses
 *     and returns focus to the trigger.
 *   - Wires `aria-describedby` from trigger to tooltip and uses
 *     role="tooltip" on the bubble.
 *   - prefers-reduced-motion: reduce disables the fade transition.
 *   - Smart placement: tries above first, flips below if clipped, and
 *     clamps horizontally to the viewport.
 *   - Renders via createPortal to document.body so transform/overflow
 *     ancestors (sticky toolbars, scroll containers) can't clip it. Same
 *     pattern as AddCategoryModal (PR #137).
 *   - Touch: tap opens, tap-elsewhere closes.
 *   - Z-index 60 — above sticky bars (z-20/40) and the floating widget
 *     anchor zone (z-40), below true modal dialogs (z-50/100). Inner
 *     "Learn more" anchor still works inside the portal.
 *
 * NOT a replacement for HelpAnchor — use HelpAnchor for "I want to read
 * the full manual section" and Tooltip for "what does this thing on
 * this screen mean right now."
 */

const TOOLTIP_OFFSET = 8;
const VIEWPORT_PADDING = 8;
const MAX_WIDTH = 280;

export interface TooltipProps {
  /** Short, one-sentence explanation. No em-dashes per house style. */
  content: ReactNode;
  /**
   * Optional /docs anchor id (e.g. "transactions"). When provided, a
   * "Learn more" link is rendered inside the tooltip pointing at
   * `/docs#<learnMoreSection>`.
   */
  learnMoreSection?: string;
  /** Optional explicit label for the docs link. Defaults to "Learn more". */
  learnMoreLabel?: string;
  /**
   * Optional custom trigger. Must be a single ReactElement; the Tooltip
   * will clone it to inject ref, aria-describedby, and event handlers.
   * If omitted, the Tooltip renders its own default "?" button.
   */
  trigger?: ReactElement;
  /** Optional ARIA label for the default "?" trigger button. */
  triggerLabel?: string;
  /** Optional class overrides for the default trigger button. */
  className?: string;
  /** Optional id override for the tooltip element. */
  id?: string;
}

type Placement = "top" | "bottom";

interface Position {
  top: number;
  left: number;
  placement: Placement;
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return reduced;
}

export default function Tooltip({
  content,
  learnMoreSection,
  learnMoreLabel = "Learn more",
  trigger,
  triggerLabel = "More info",
  className = "",
  id: idProp,
}: TooltipProps) {
  const reactId = useId();
  const tooltipId = idProp ?? `tt-${reactId.replace(/:/g, "")}`;

  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [position, setPosition] = useState<Position>({
    top: 0,
    left: 0,
    placement: "top",
  });

  const triggerRef = useRef<HTMLSpanElement | null>(null);
  const tooltipRef = useRef<HTMLDivElement | null>(null);
  // Set by handleFocus, read + cleared by handleClick. Browsers fire
  // `focus` immediately before `click` when a user mouse-clicks an
  // unfocused element. Without this flag, the focus opens the tooltip
  // and the click in the same tick toggles it back closed — inverting
  // every other click. handleFocus arms the flag, the next click clears
  // it instead of toggling, and a queued microtask resets the flag so a
  // deliberate second click (with no fresh focus) still toggles.
  const justOpenedByFocus = useRef(false);
  const reducedMotion = useReducedMotion();

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- flag mounted after first commit so the portal renders client-side only (avoids SSR hydration mismatch)
    setMounted(true);
  }, []);

  const focusTrigger = useCallback(() => {
    // The wrapper span is not itself focusable, so find the first
    // focusable descendant (typically the default "?" <button>, or the
    // first focusable element inside a caller-supplied trigger).
    const wrapper = triggerRef.current;
    if (!wrapper) return;
    const focusable = wrapper.querySelector<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    focusable?.focus();
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    // Return focus to the trigger after dismiss so keyboard users keep
    // their place. Only do this if the active element is inside the
    // tooltip; otherwise the user has already moved on intentionally.
    if (
      typeof document !== "undefined" &&
      tooltipRef.current &&
      document.activeElement &&
      tooltipRef.current.contains(document.activeElement)
    ) {
      focusTrigger();
    }
  }, [focusTrigger]);

  const computePosition = useCallback(() => {
    const triggerEl = triggerRef.current;
    const tooltipEl = tooltipRef.current;
    if (!triggerEl || !tooltipEl || typeof window === "undefined") return;

    const triggerRect = triggerEl.getBoundingClientRect();
    const tooltipRect = tooltipEl.getBoundingClientRect();
    const viewportW = window.innerWidth;
    const viewportH = window.innerHeight;

    // Prefer above; flip below when there is not enough room above and
    // there is more room below.
    const spaceAbove = triggerRect.top;
    const spaceBelow = viewportH - triggerRect.bottom;
    const needed = tooltipRect.height + TOOLTIP_OFFSET + VIEWPORT_PADDING;
    const placement: Placement =
      spaceAbove >= needed || spaceAbove >= spaceBelow ? "top" : "bottom";

    let top =
      placement === "top"
        ? triggerRect.top - tooltipRect.height - TOOLTIP_OFFSET
        : triggerRect.bottom + TOOLTIP_OFFSET;

    let left =
      triggerRect.left + triggerRect.width / 2 - tooltipRect.width / 2;

    // Clamp horizontally to viewport with padding.
    if (left < VIEWPORT_PADDING) left = VIEWPORT_PADDING;
    const maxLeft = viewportW - tooltipRect.width - VIEWPORT_PADDING;
    if (left > maxLeft) left = Math.max(VIEWPORT_PADDING, maxLeft);

    // Account for window scroll: getBoundingClientRect is viewport-
    // relative but we render in document.body with position: fixed, so
    // viewport-relative values are correct as-is.
    if (top < VIEWPORT_PADDING) top = VIEWPORT_PADDING;

    setPosition({ top, left, placement });
  }, []);

  // Recompute on open and on window resize/scroll while open.
  useLayoutEffect(() => {
    if (!open) return;
    computePosition();
  }, [open, computePosition, content]);

  useEffect(() => {
    if (!open) return;
    const onReflow = () => computePosition();
    window.addEventListener("resize", onReflow);
    window.addEventListener("scroll", onReflow, true);
    return () => {
      window.removeEventListener("resize", onReflow);
      window.removeEventListener("scroll", onReflow, true);
    };
  }, [open, computePosition]);

  // Outside-click / outside-tap dismiss.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node | null;
      if (!target) return;
      if (
        triggerRef.current?.contains(target) ||
        tooltipRef.current?.contains(target)
      ) {
        return;
      }
      setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  // Escape dismiss (global, since focus could be inside the portal).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, close]);

  // Imperatively set aria-describedby on the first focusable descendant
  // when the tooltip is open. We don't pass this through React props
  // because the custom-trigger path can't safely set props on a child
  // we don't own (and we deliberately avoid cloneElement to stay clear
  // of the react-hooks/refs lint rule). Cleanup removes the attribute
  // so screen readers don't reference a stale id.
  useEffect(() => {
    const wrapper = triggerRef.current;
    if (!wrapper) return;
    const focusable = wrapper.querySelector<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable) return;
    if (open) {
      focusable.setAttribute("aria-describedby", tooltipId);
    } else {
      focusable.removeAttribute("aria-describedby");
    }
    return () => {
      focusable.removeAttribute("aria-describedby");
    };
  }, [open, tooltipId]);

  const handleMouseEnter = useCallback(() => setOpen(true), []);
  const handleMouseLeave = useCallback((e: React.MouseEvent) => {
    // Don't close if the cursor moved into the tooltip itself (so users
    // can click "Learn more").
    const related = e.relatedTarget as Node | null;
    if (related && tooltipRef.current?.contains(related)) return;
    setOpen(false);
  }, []);
  const handleFocus = useCallback(() => {
    setOpen(true);
    // Arm the focus-then-click suppressor. Queued microtask resets the
    // flag after the same-tick click handler runs. requestAnimationFrame
    // would also work; setTimeout(0) is the conservative cross-browser
    // pick for "next tick" and matches what testing-library's
    // act-flushing expects.
    justOpenedByFocus.current = true;
    if (typeof window !== "undefined") {
      window.setTimeout(() => {
        justOpenedByFocus.current = false;
      }, 0);
    }
  }, []);
  const handleBlur = useCallback((e: React.FocusEvent) => {
    const next = e.relatedTarget as Node | null;
    if (next && tooltipRef.current?.contains(next)) return;
    setOpen(false);
  }, []);
  const handleClick = useCallback(() => {
    // Suppress the click that fires in the same tick as focus (browser
    // mouse-click order is focus -> click). Clear the flag so the next
    // click toggles normally.
    if (justOpenedByFocus.current) {
      justOpenedByFocus.current = false;
      return;
    }
    setOpen((v) => !v);
  }, []);
  const handleKeyDown = useCallback(
    (e: ReactKeyboardEvent) => {
      if (e.key === "Escape" && open) {
        e.preventDefault();
        close();
      }
    },
    [open, close],
  );

  // The trigger is rendered inside a wrapping <span> that owns the ref
  // and the event handlers. This sidesteps the React 19 react-hooks/refs
  // lint rule (which forbids passing refs through cloneElement) and lets
  // any custom trigger element work without needing forwardRef. Focus +
  // hover events bubble through React's synthetic event system, so the
  // wrapper still sees them even though the focusable element is the
  // child <button>. The span uses `inline-flex` so its layout footprint
  // matches the child's natural inline-block size.
  //
  // `aria-describedby` is wired imperatively onto the first focusable
  // descendant (typically the inner <button>) via the effect below, so
  // assistive tech announces the bubble's content on the actual focused
  // element instead of an interaction-empty wrapper.

  const wrapperHandlers = {
    onMouseEnter: handleMouseEnter,
    onMouseLeave: handleMouseLeave,
    onFocus: handleFocus,
    onBlur: handleBlur,
    onClick: handleClick,
    onKeyDown: handleKeyDown,
  };

  const triggerNode = trigger ? (
    <span
      ref={triggerRef}
      data-testid="tooltip-trigger-wrapper"
      className="inline-flex"
      {...wrapperHandlers}
    >
      {trigger as ReactElement}
    </span>
  ) : (
    <span
      ref={triggerRef}
      className="inline-flex"
      {...wrapperHandlers}
    >
      <button
        type="button"
        aria-label={triggerLabel}
        data-testid="tooltip-trigger"
        className={`inline-flex min-h-[24px] min-w-[24px] items-center justify-center rounded-full text-text-muted hover:text-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 ${className}`}
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          fill="none"
          className="h-3.5 w-3.5"
          stroke="currentColor"
          strokeWidth={1.6}
        >
          <circle cx="8" cy="8" r="6.5" />
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M6.4 6.2c.2-.9 1-1.4 1.8-1.4 1 0 1.7.7 1.7 1.6 0 .7-.4 1.1-1.1 1.5-.5.3-.8.7-.8 1.3"
          />
          <circle cx="8.05" cy="11.2" r="0.55" fill="currentColor" stroke="none" />
        </svg>
      </button>
    </span>
  );

  const bubbleStyle: CSSProperties = {
    position: "fixed",
    top: position.top,
    left: position.left,
    maxWidth: MAX_WIDTH,
    zIndex: 60,
  };

  const transitionClass = reducedMotion
    ? ""
    : "transition-opacity duration-150 ease-out";

  return (
    <>
      {triggerNode}
      {mounted && open
        ? createPortal(
            <div
              ref={tooltipRef}
              id={tooltipId}
              role="tooltip"
              data-testid="tooltip-bubble"
              data-placement={position.placement}
              data-reduced-motion={reducedMotion ? "true" : "false"}
              style={bubbleStyle}
              className={`rounded-md border border-border bg-surface-overlay px-3 py-2 text-xs leading-snug text-text-primary shadow-lg ${transitionClass}`}
              onMouseLeave={(e) => {
                const next = e.relatedTarget as Node | null;
                if (next && triggerRef.current?.contains(next)) return;
                setOpen(false);
              }}
            >
              <div>{content}</div>
              {learnMoreSection ? (
                <div className="mt-1.5">
                  <Link
                    href={`/docs#${learnMoreSection}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    data-testid="tooltip-learn-more"
                    data-section={learnMoreSection}
                    className="inline-flex items-center gap-1 text-[11px] font-medium text-accent hover:text-accent-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                  >
                    {learnMoreLabel}
                    <span aria-hidden="true">&rarr;</span>
                  </Link>
                </div>
              ) : null}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
