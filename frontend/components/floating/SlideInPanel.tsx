"use client";

import { ReactNode, useEffect, useRef } from "react";

import { X } from "lucide-react";

/**
 * Right-side slide-in panel for quick-entry flows. Pattern reference:
 * Linear / Notion task quick-add. Side panel was chosen over a centered
 * modal because the FAB lives in the bottom-right and a panel sliding
 * from the same corner reinforces the visual link.
 *
 * Behavior:
 *   - Renders nothing when `open` is false (no dead overlay in the DOM).
 *   - Esc closes (handler at document level, capture phase off so inner
 *     dialogs win).
 *   - Click on the dimmed overlay closes. Click inside the panel does
 *     not close.
 *   - Focus is trapped inside the panel: Tab/Shift+Tab cycle through
 *     focusables. On open, the first focusable receives focus. On
 *     close, focus returns to the trigger.
 *   - Body scroll is locked while open so the panel feels modal.
 *   - z-50 to sit above the AnchorZone (z-40) and any non-modal page
 *     content. ConfirmModal also uses z-50; a panel-spawned confirm
 *     modal renders on top because it mounts later.
 */

export interface SlideInPanelProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  /**
   * Optional test id so tests can target the panel specifically (default
   * "slide-in-panel"). Useful when more than one panel could exist on a
   * page.
   */
  testId?: string;
  /**
   * Optional width class. Defaults to a comfortable form width.
   */
  widthClass?: string;
}

export default function SlideInPanel({
  open,
  onClose,
  title,
  children,
  testId,
  widthClass,
}: SlideInPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  // Capture the previously-focused element before the panel takes over,
  // and restore it on close. Mirrors ConfirmModal's pattern.
  useEffect(() => {
    if (open) {
      previousFocusRef.current = document.activeElement as HTMLElement;
      // Give the panel one tick to render before moving focus.
      const id = window.setTimeout(() => {
        const focusables = getFocusables(panelRef.current);
        focusables[0]?.focus();
      }, 0);
      return () => window.clearTimeout(id);
    }
    if (previousFocusRef.current) {
      previousFocusRef.current.focus();
      previousFocusRef.current = null;
    }
  }, [open]);

  // Esc + focus trap.
  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key === "Tab") {
        const focusables = getFocusables(panelRef.current);
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open, onClose]);

  // Body scroll lock so the underlying page doesn't move while the panel
  // is open. Cleared on unmount as a belt-and-suspenders.
  useEffect(() => {
    if (open) document.body.style.overflow = "hidden";
    else document.body.style.overflow = "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  if (!open) return null;

  return (
    <div
      data-testid={testId ?? "slide-in-panel"}
      className="fixed inset-0 z-50 flex justify-end bg-bg/80"
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="slide-in-panel-title"
        className={`flex h-full w-full flex-col overflow-y-auto border-l border-border bg-surface shadow-xl ${
          widthClass ?? "sm:max-w-md md:max-w-lg"
        }`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-border px-6 py-4">
          <h2
            id="slide-in-panel-title"
            className="text-lg font-semibold text-text-primary"
          >
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md text-text-muted hover:bg-surface-raised hover:text-text-primary"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-6 py-5">{children}</div>
      </div>
    </div>
  );
}

function getFocusables(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  const nodes = root.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  );
  return Array.from(nodes);
}
