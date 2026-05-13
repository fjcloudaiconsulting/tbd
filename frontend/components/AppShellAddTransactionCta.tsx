"use client";

import {
  KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

import { ArrowLeftRight, ChevronDown, Plus, Receipt } from "lucide-react";

import SlideInPanel from "@/components/floating/SlideInPanel";
import TransactionForm from "@/components/floating/TransactionForm";
import TransferForm from "@/components/floating/TransferForm";
import { apiFetch } from "@/lib/api";
import { btnPrimary } from "@/lib/styles";
import type { Account, Category } from "@/lib/types";

/**
 * AppShell-level quick-add CTA.
 *
 * Two affordances side-by-side as a split button:
 *   - Primary "+ New transaction" opens the Transaction panel directly
 *     (one click, preserves prior behavior).
 *   - Chevron toggles a small popover menu with two items:
 *       1. New transaction
 *       2. New transfer
 *     The Transfer item opens a TransferForm panel that posts to the
 *     existing POST /api/v1/transactions/transfer endpoint shipped
 *     with L3.x Transfers (PRs #110-#118). No backend changes.
 *
 * UX rationale (Approach B variant — split button):
 *   - Tabs inside the modal (Approach A) would force TransactionForm to
 *     gain a transfer mode, duplicating the canonical
 *     /transactions transfer flow inside a quick-entry surface that was
 *     deliberately built single-purpose (see TransactionForm jsdoc).
 *   - A plain dropdown that replaces the button (full Approach B)
 *     regresses the most-common path (add an expense) from one click
 *     to two.
 *   - The split button keeps the dominant path one click, makes
 *     Transfer one extra click and one tab-stop away, and scales for
 *     future quick-types (Batch entry, recurring template, etc.) by
 *     just appending menu items.
 *
 * Keyboard contract (focus order, top to bottom):
 *   - Tab focuses the primary "New transaction" button.
 *   - Tab again focuses the chevron toggle.
 *   - Enter / Space on the chevron opens the menu and focuses item 1.
 *   - Down / Up arrows cycle menu items.
 *   - Enter / Space activates the focused item.
 *   - Escape closes the menu and returns focus to the chevron.
 *   - Tab from inside the menu closes it (menu is not a focus-trap;
 *     the SlideInPanel that follows owns its own trap).
 *
 * Data refresh after submit: same `pfv:transaction-added` window event
 * for both Transaction and Transfer (the Transactions page already
 * listens for it). Decoupled, no prop drilling.
 */

type PanelKind = "transaction" | "transfer";

export default function AppShellAddTransactionCta() {
  const [openPanel, setOpenPanel] = useState<PanelKind | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [loaded, setLoaded] = useState(false);

  const menuId = useId();
  const chevronRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const firstMenuItemRef = useRef<HTMLButtonElement>(null);

  const loadRefs = useCallback(async () => {
    try {
      const [accts, cats] = await Promise.all([
        apiFetch<Account[]>("/api/v1/accounts"),
        apiFetch<Category[]>("/api/v1/categories"),
      ]);
      setAccounts(accts ?? []);
      setCategories(cats ?? []);
      setLoaded(true);
    } catch {
      // Swallow ref-load errors silently. Forms fall through to their
      // empty states and any submit error surfaces inline. The CTA
      // itself stays clickable so the user can retry.
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void loadRefs();
  }, [loadRefs]);

  // Close the menu on outside click or Escape (when the menu is open
  // but no panel is yet open). The SlideInPanel owns its own Escape
  // handler once a panel is open.
  useEffect(() => {
    if (!menuOpen) return;
    function handleClick(e: MouseEvent) {
      const target = e.target as Node;
      if (
        menuRef.current?.contains(target) ||
        chevronRef.current?.contains(target)
      ) {
        return;
      }
      setMenuOpen(false);
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        setMenuOpen(false);
        chevronRef.current?.focus();
      }
    }
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleKey);
    };
  }, [menuOpen]);

  // When the menu opens, focus the first item so keyboard users can
  // drive it without a mouse trip. setTimeout 0 gives the menu a tick
  // to mount before focus moves.
  useEffect(() => {
    if (menuOpen) {
      const id = window.setTimeout(() => {
        firstMenuItemRef.current?.focus();
      }, 0);
      return () => window.clearTimeout(id);
    }
  }, [menuOpen]);

  function openTransaction() {
    void loadRefs();
    setMenuOpen(false);
    setOpenPanel("transaction");
  }

  function openTransfer() {
    void loadRefs();
    setMenuOpen(false);
    setOpenPanel("transfer");
  }

  function handleTransactionAdded() {
    if (typeof window !== "undefined") {
      window.dispatchEvent(new Event("pfv:transaction-added"));
    }
  }

  function onMenuKeyDown(e: ReactKeyboardEvent<HTMLDivElement>) {
    // Tab closes the menu so focus doesn't slip into page chrome
    // (theme toggle, sign-out) while the popover is still visually
    // open. Returns focus to the chevron so the user can re-open
    // without hunting for the trigger. Mirrors the WAI-ARIA menu
    // pattern when the menu is not itself a navigation surface.
    if (e.key === "Tab") {
      e.preventDefault();
      setMenuOpen(false);
      chevronRef.current?.focus();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]',
      ) ?? [],
    );
    if (items.length === 0) return;
    const active = document.activeElement as HTMLElement | null;
    const currentIndex = active ? items.indexOf(active as HTMLButtonElement) : -1;
    const delta = e.key === "ArrowDown" ? 1 : -1;
    const nextIndex =
      currentIndex === -1
        ? 0
        : (currentIndex + delta + items.length) % items.length;
    items[nextIndex].focus();
  }

  return (
    <div className="relative inline-flex">
      <div className="inline-flex rounded-md shadow-sm">
        <button
          type="button"
          onClick={openTransaction}
          aria-label="New transaction"
          data-testid="appshell-add-transaction-cta"
          className={`${btnPrimary} inline-flex min-h-[44px] items-center gap-1.5 rounded-r-none border-r border-accent-text/20`}
        >
          <Plus className="h-4 w-4" aria-hidden="true" />
          <span className="hidden sm:inline">New transaction</span>
        </button>
        <button
          ref={chevronRef}
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          aria-label="More quick-add options"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          aria-controls={menuId}
          data-testid="appshell-quick-add-menu-toggle"
          className={`${btnPrimary} inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded-l-none px-2`}
        >
          <ChevronDown
            className={`h-4 w-4 transition-transform ${menuOpen ? "rotate-180" : ""}`}
            aria-hidden="true"
          />
        </button>
      </div>

      {menuOpen && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label="Quick-add options"
          data-testid="appshell-quick-add-menu"
          onKeyDown={onMenuKeyDown}
          className="absolute right-0 top-full z-50 mt-1 w-56 overflow-hidden rounded-md border border-border bg-surface shadow-lg"
        >
          <button
            ref={firstMenuItemRef}
            type="button"
            role="menuitem"
            onClick={openTransaction}
            data-testid="appshell-quick-add-menu-transaction"
            className="flex w-full items-center gap-3 px-3 py-2.5 text-left text-sm text-text-primary hover:bg-surface-raised focus:bg-surface-raised focus:outline-none"
          >
            <Receipt className="h-4 w-4 text-text-muted" aria-hidden="true" />
            <span>New transaction</span>
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={openTransfer}
            data-testid="appshell-quick-add-menu-transfer"
            className="flex w-full items-center gap-3 px-3 py-2.5 text-left text-sm text-text-primary hover:bg-surface-raised focus:bg-surface-raised focus:outline-none"
          >
            <ArrowLeftRight
              className="h-4 w-4 text-text-muted"
              aria-hidden="true"
            />
            <span>New transfer</span>
          </button>
        </div>
      )}

      <SlideInPanel
        open={openPanel === "transaction"}
        onClose={() => setOpenPanel(null)}
        title="Add transaction"
        testId="add-transaction-panel"
      >
        {loaded ? (
          <TransactionForm
            accounts={accounts}
            categories={categories}
            onSaved={() => setOpenPanel(null)}
            onCategoryCreated={(cat) =>
              setCategories((prev) => [...prev, cat])
            }
            onTransactionAdded={handleTransactionAdded}
          />
        ) : (
          <div className="flex items-center justify-center py-12 text-sm text-text-muted">
            Loading...
          </div>
        )}
      </SlideInPanel>

      <SlideInPanel
        open={openPanel === "transfer"}
        onClose={() => setOpenPanel(null)}
        title="Add transfer"
        testId="add-transfer-panel"
      >
        {loaded ? (
          <TransferForm
            accounts={accounts}
            categories={categories}
            onSaved={() => setOpenPanel(null)}
            onCategoryCreated={(cat) =>
              setCategories((prev) => [...prev, cat])
            }
            onTransactionAdded={handleTransactionAdded}
          />
        ) : (
          <div className="flex items-center justify-center py-12 text-sm text-text-muted">
            Loading...
          </div>
        )}
      </SlideInPanel>
    </div>
  );
}

/**
 * Route allow-list helper. Exposed for AppShell to gate visibility, and
 * for unit tests to assert the predicate without rendering the shell.
 *
 * Show on the core money routes; hide on settings/admin/system. The
 * empty-string fallback ("/") is treated as not-a-money-route, the
 * /login redirect runs before AppShell mounts, so we won't see "/" in
 * practice, but the predicate stays well-defined.
 */
const SHOW_ON: readonly string[] = [
  "/dashboard",
  "/transactions",
  "/accounts",
  "/categories",
  "/forecast-plans",
  "/budgets",
  "/recurring",
];

const HIDE_PREFIXES: readonly string[] = [
  "/settings/",
  "/admin/",
  "/system/",
];

export function shouldShowAddTransactionCta(pathname: string | null): boolean {
  if (!pathname) return false;
  // Hide-list wins over show-list. `/admin` is a money-adjacent root
  // but its children are platform-admin, not user money flows.
  if (HIDE_PREFIXES.some((p) => pathname.startsWith(p))) return false;
  if (pathname === "/settings" || pathname === "/admin" || pathname === "/system") {
    return false;
  }
  return SHOW_ON.some((r) => pathname === r || pathname.startsWith(r + "/"));
}
