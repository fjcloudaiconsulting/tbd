"use client";

import {
  KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

import { MoreHorizontal } from "lucide-react";

/**
 * Reusable per-row overflow ("...") menu.
 *
 * Mirrors the dropdown-menu pattern used by AppShellAddTransactionCta:
 *   - Trigger button with the lucide MoreHorizontal icon and the full
 *     aria-haspopup / aria-expanded / aria-controls contract.
 *   - Popover with role="menu", absolutely positioned, right-aligned.
 *   - Each item is a <button role="menuitem"> with an optional danger
 *     variant.
 *
 * Keyboard contract (WAI-ARIA menu button, non-trapping):
 *   - Enter / Space / click on the trigger opens the menu and focuses
 *     the first item.
 *   - ArrowDown / ArrowUp cycle items.
 *   - Escape closes the menu and returns focus to the trigger.
 *   - Tab closes the menu and returns focus to the trigger (the menu is
 *     not a focus trap; focus then continues to the next page control).
 *   - Outside mousedown closes the menu.
 */

export interface OverflowMenuItem {
  label: string;
  onSelect: () => void;
  danger?: boolean;
  /** Override the accessible name when it must differ from the visible label. */
  ariaLabel?: string;
  /** Optional data-testid for the individual menu item button. */
  testId?: string;
}

export interface OverflowMenuProps {
  items: OverflowMenuItem[];
  /** aria-label for the trigger button. Default "More actions". */
  label?: string;
  /** Optional data-testid for the trigger button. */
  testId?: string;
}

export default function OverflowMenu({
  items,
  label = "More actions",
  testId,
}: OverflowMenuProps) {
  const [open, setOpen] = useState(false);

  const menuId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const firstItemRef = useRef<HTMLButtonElement>(null);

  // Close on outside mousedown / Escape (returning focus to the trigger).
  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      const target = e.target as Node;
      if (
        menuRef.current?.contains(target) ||
        triggerRef.current?.contains(target)
      ) {
        return;
      }
      setOpen(false);
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        triggerRef.current?.focus();
      }
    }
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKey);
    };
  }, [open]);

  // Focus the first item when the menu opens. setTimeout 0 lets the
  // popover mount before focus moves.
  useEffect(() => {
    if (open) {
      const id = window.setTimeout(() => {
        firstItemRef.current?.focus();
      }, 0);
      return () => window.clearTimeout(id);
    }
  }, [open]);

  function onMenuKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key === "Tab") {
      e.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const buttons = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]',
      ) ?? [],
    );
    if (buttons.length === 0) return;
    const active = document.activeElement as HTMLElement | null;
    const currentIndex = active
      ? buttons.indexOf(active as HTMLButtonElement)
      : -1;
    const delta = e.key === "ArrowDown" ? 1 : -1;
    const nextIndex =
      currentIndex === -1
        ? 0
        : (currentIndex + delta + buttons.length) % buttons.length;
    buttons[nextIndex].focus();
  }

  function selectItem(item: OverflowMenuItem) {
    setOpen(false);
    item.onSelect();
  }

  if (items.length === 0) return null;

  return (
    <div className="relative inline-flex">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        data-testid={testId}
        className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md text-text-muted hover:bg-surface-raised hover:text-text-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 md:min-h-0 md:min-w-0 md:p-1.5"
      >
        <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
      </button>

      {open && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={label}
          onKeyDown={onMenuKeyDown}
          className="absolute right-0 top-full z-50 mt-1 w-48 overflow-hidden rounded-md border border-border bg-surface shadow-lg"
        >
          {items.map((item, i) => (
            <button
              key={item.label}
              ref={i === 0 ? firstItemRef : undefined}
              type="button"
              role="menuitem"
              onClick={() => selectItem(item)}
              aria-label={item.ariaLabel}
              data-testid={item.testId}
              className={`flex w-full items-center gap-3 px-3 py-2.5 text-left text-sm hover:bg-surface-raised focus:bg-surface-raised focus:outline-none ${
                item.danger
                  ? "text-danger hover:text-danger"
                  : "text-text-primary"
              }`}
            >
              <span>{item.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
