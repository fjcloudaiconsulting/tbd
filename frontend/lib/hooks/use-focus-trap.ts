"use client";

import { RefObject, useEffect } from "react";

const FOCUSABLE_SELECTOR =
  'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

interface UseFocusTrapOptions {
  /** When true, the trap is engaged and the previously focused element
   *  is restored on close. */
  active: boolean;
  /** Ref to the dialog container. Tab/Shift+Tab cycles within this
   *  container's focusable descendants. */
  containerRef: RefObject<HTMLElement | null>;
  /** Optional ref to receive initial focus on open. Falls back to the
   *  first focusable element in the container. */
  initialFocusRef?: RefObject<HTMLElement | null>;
}

/**
 * Focus trap for modal dialogs.
 *
 * On open: stores the previously focused element, then focuses
 * `initialFocusRef` (or the first focusable child of `containerRef`).
 *
 * While open: Tab/Shift+Tab wrap focus inside the container.
 *
 * On close: restores focus to the element that was active before open.
 *
 * Owned by the shared UI primitives. Mirrors the behavior baked into
 * `ConfirmModal` so batch modals can drop their custom keyboard handling
 * and stay consistent.
 */
export function useFocusTrap({
  active,
  containerRef,
  initialFocusRef,
}: UseFocusTrapOptions): void {
  useEffect(() => {
    if (!active) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;

    const focusInitial = () => {
      if (initialFocusRef?.current) {
        initialFocusRef.current.focus();
        return;
      }
      const first = containerRef.current?.querySelector<HTMLElement>(
        FOCUSABLE_SELECTOR,
      );
      first?.focus();
    };
    focusInitial();

    return () => {
      previouslyFocused?.focus?.();
    };
  }, [active, containerRef, initialFocusRef]);

  useEffect(() => {
    if (!active) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      const root = containerRef.current;
      if (!root) return;
      const all = Array.from(
        root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter((el) => !el.hasAttribute("disabled") && el.offsetParent !== null);
      if (all.length === 0) {
        e.preventDefault();
        return;
      }
      const first = all[0];
      const last = all[all.length - 1];
      const activeEl = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (activeEl === first || !root.contains(activeEl)) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (activeEl === last || !root.contains(activeEl)) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [active, containerRef]);
}
