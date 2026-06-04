"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import AddCategoryModal from "@/components/ui/AddCategoryModal";
import type { Category } from "@/lib/types";

const RECENT_KEY = "pfv2-recent-categories";
const MAX_RECENT = 5;

function getRecent(): number[] {
  if (typeof window === "undefined") return [];
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
  } catch { return []; }
}

function saveRecent(id: number) {
  const recent = getRecent().filter((r) => r !== id);
  recent.unshift(id);
  localStorage.setItem(RECENT_KEY, JSON.stringify(recent.slice(0, MAX_RECENT)));
}

interface Props {
  id: string;
  categories: Category[];
  value: number | "";
  onChange: (id: number | "") => void;
  filterType?: "income" | "expense";
  /**
   * Uppercase variant used by transfer-related callers
   * (UnpairTransferModal for income/expense legs, TransferForm for the
   * shared transfer leg). `"BOTH"` narrows the list to categories whose
   * `type === "both"`, which is the only kind the backend accepts on a
   * transfer's shared category slot.
   */
  typeFilter?: "INCOME" | "EXPENSE" | "BOTH";
  className?: string;
  "aria-label"?: string;
  onCategoryCreated?: (cat: Category) => void;
  /**
   * Category IDs that should still appear in the dropdown but render as
   * disabled (greyed-out, non-selectable, with an "(already added)"
   * hint). Used by callers that enforce one-row-per-category, so a
   * previously available choice doesn't silently vanish after it's
   * picked — the user can see why the option is no longer selectable.
   */
  disabledIds?: ReadonlySet<number> | number[];
  /**
   * Restrict the selectable rows to MASTER categories only (parent_id is
   * null), rendered as a flat selectable list rather than non-selectable
   * group headers. Used by the Forecast Plans page in "Master categories"
   * build mode, where an item is added straight against a master. Default
   * (off) keeps the standard behavior: subcategories are the selectable
   * rows, grouped under their master's header.
   */
  masterOnly?: boolean;
}

export default function CategorySelect({ id, categories, value, onChange, filterType, typeFilter, className = "", "aria-label": ariaLabel, onCategoryCreated, disabledIds, masterOnly = false }: Props) {
  const disabledSet = useMemo(() => {
    if (!disabledIds) return null;
    return disabledIds instanceof Set ? disabledIds : new Set(disabledIds);
  }, [disabledIds]);
  const isDisabled = (id: number) => (disabledSet ? disabledSet.has(id) : false);
  // Normalize uppercase `typeFilter` (transfers callers) to internal lowercase
  // so it joins the existing filterType machinery without divergent code paths.
  const effectiveFilterType = filterType ?? (typeFilter === "INCOME" ? "income" : typeFilter === "EXPENSE" ? "expense" : undefined);
  // `bothOnly` is a separate mode (not a third filterType value) because
  // the existing semantics for "income"/"expense" mean "show same-type
  // categories AND the always-compatible `both` ones." Transfers need
  // the opposite: show ONLY `both`, hiding income- and expense-only
  // categories that the backend would reject on the shared transfer
  // leg. The `+ Add category` modal launched from this mode defaults to
  // `initialType="both"` without locking it.
  const bothOnly = typeFilter === "BOTH";
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const [showAddModal, setShowAddModal] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Resolve the selected category from the full categories list, but
  // hide it when its type is incompatible with the active filterType.
  // BOTH-typed categories are treated as compatible. We surface the
  // mismatch as "no chip" rather than auto-firing onChange — keeping
  // CategorySelect a pure controlled component, the way every existing
  // caller already uses it. The backend now rejects mismatched
  // (txn_type, category) pairs, so callers should also clear their
  // local state when their type changes (forecast-plans, transactions
  // edit row, etc. already do this).
  const rawSelected = categories.find((c) => c.id === value);
  const selected =
    rawSelected &&
    ((bothOnly && rawSelected.type !== "both") ||
      (effectiveFilterType &&
        rawSelected.type !== effectiveFilterType &&
        rawSelected.type !== "both"))
      ? undefined
      : rawSelected;

  // Precompute parent IDs set and selectable items (O(n) instead of O(n^2))
  const { selectable, parentIds } = useMemo(() => {
    const pIds = new Set<number>();
    for (const c of categories) {
      if (c.parent_id !== null) pIds.add(c.parent_id);
    }
    const items = categories.filter((c) => {
      if (bothOnly && c.type !== "both") return false;
      if (effectiveFilterType && c.type !== effectiveFilterType && c.type !== "both") return false;
      // Master mode: only masters are selectable (subcategories hidden).
      if (masterOnly) return c.parent_id === null;
      return c.parent_id !== null || !pIds.has(c.id);
    });
    return { selectable: items, parentIds: pIds };
  }, [categories, effectiveFilterType, bothOnly, masterOnly]);

  const q = query.toLowerCase();
  const filtered = useMemo(() =>
    q
      ? selectable.filter((c) => c.name.toLowerCase().includes(q) || (c.parent_name?.toLowerCase().includes(q) ?? false))
      : selectable,
    [selectable, q]
  );

  // Recent items — loaded client-side only to avoid SSR hydration mismatch
  const [recentIds, setRecentIds] = useState<number[]>([]);
  useEffect(() => { setRecentIds(getRecent()); }, [open]);

  const recentItems = recentIds
    .map((rid) => filtered.find((c) => c.id === rid))
    .filter(Boolean) as Category[];
  const nonRecent = filtered.filter((c) => !recentIds.includes(c.id));

  // Build ordered flat list for keyboard nav.
  // Disabled categories are skipped — Enter on an "already added" row
  // is a no-op, so we don't waste an arrow stop on it.
  const flatList: Category[] = [...recentItems, ...nonRecent].filter(
    (c) => !isDisabled(c.id),
  );

  useEffect(() => {
    if (!open || showAddModal) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open, showAddModal]);

  useEffect(() => { setHighlightIdx(-1); }, [query, open]);

  function handleSelect(cat: Category) {
    if (isDisabled(cat.id)) return;
    onChange(cat.id);
    saveRecent(cat.id);
    setQuery("");
    setOpen(false);
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "Enter") { setOpen(true); e.preventDefault(); }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlightIdx((prev) => Math.min(prev + 1, flatList.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightIdx((prev) => Math.max(prev - 1, 0));
    } else if (e.key === "Enter" && highlightIdx >= 0 && highlightIdx < flatList.length) {
      e.preventDefault();
      handleSelect(flatList[highlightIdx]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  useEffect(() => {
    if (highlightIdx < 0 || !listRef.current) return;
    const items = listRef.current.querySelectorAll("[data-cat-item]");
    items[highlightIdx]?.scrollIntoView({ block: "nearest" });
  }, [highlightIdx]);

  // Group non-recent by master for display.
  // Memoized so the `grouped` useMemo below sees a stable `masters`
  // reference and only recomputes when the categories list itself changes.
  const masters = useMemo(
    () => categories.filter((c) => c.parent_id === null),
    [categories]
  );
  const grouped = useMemo(() => {
    // Master mode: masters ARE the selectable rows, so render them as a
    // single flat, headerless group instead of grouping subs under each.
    if (masterOnly) {
      return nonRecent.length > 0 ? [{ label: "", items: nonRecent }] : [];
    }
    const groups: { label: string; items: Category[] }[] = [];
    for (const master of masters) {
      const items = nonRecent.filter((c) => c.parent_id === master.id);
      if (items.length > 0) groups.push({ label: master.name, items });
    }
    const masterless = nonRecent.filter((c) => c.parent_id === null);
    if (masterless.length > 0) groups.push({ label: "Other", items: masterless });
    return groups;
  }, [masters, nonRecent, masterOnly]);

  const activeDescendant = highlightIdx >= 0 && highlightIdx < flatList.length
    ? `${id}-opt-${flatList[highlightIdx].id}` : undefined;

  return (
    <div ref={ref} className="relative">
      <input
        ref={inputRef}
        id={id}
        type="text"
        autoComplete="off"
        value={open ? query : (selected?.name ?? "")}
        placeholder="Search category..."
        onChange={(e) => { setQuery(e.target.value); if (!open) setOpen(true); }}
        onFocus={() => { setOpen(true); setQuery(""); }}
        onKeyDown={handleKeyDown}
        className={className}
        role="combobox"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-autocomplete="list"
        aria-controls={`${id}-listbox`}
        aria-activedescendant={activeDescendant}
        aria-label={ariaLabel}
      />
      <input type="hidden" name={`${id}-value`} value={value} required />

      {open && (
        <div ref={listRef} role="listbox" id={`${id}-listbox`} className="absolute z-50 mt-1 max-h-64 w-full overflow-y-auto rounded-md border border-border bg-surface shadow-lg">
          {recentItems.length > 0 && (
            <>
              <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">Recent</div>
              {recentItems.map((cat) => {
                const disabled = isDisabled(cat.id);
                const navIdx = flatList.indexOf(cat);
                const highlighted = !disabled && navIdx >= 0 && highlightIdx === navIdx;
                return (
                  <button
                    key={cat.id}
                    type="button"
                    data-cat-item
                    onClick={() => handleSelect(cat)}
                    id={`${id}-opt-${cat.id}`}
                    disabled={disabled}
                    aria-disabled={disabled}
                    className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm transition-colors ${
                      disabled
                        ? "cursor-not-allowed text-text-muted opacity-60"
                        : highlighted
                          ? "bg-accent-dim text-accent"
                          : "text-text-primary hover:bg-surface-raised"
                    }`}
                    role="option"
                    aria-selected={cat.id === value}
                  >
                    <span>
                      {cat.name}
                      {disabled && (
                        <span className="ml-1.5 text-[11px] italic text-text-muted">(already added)</span>
                      )}
                    </span>
                    <span className="text-xs text-text-muted">{cat.parent_name}</span>
                  </button>
                );
              })}
              <div className="border-t border-border-subtle" />
            </>
          )}

          {grouped.map((group) => (
            <div key={group.label}>
              {group.label && (
                <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">{group.label}</div>
              )}
              {group.items.map((cat) => {
                const disabled = isDisabled(cat.id);
                const navIdx = flatList.indexOf(cat);
                const highlighted = !disabled && navIdx >= 0 && highlightIdx === navIdx;
                return (
                  <button
                    key={cat.id}
                    type="button"
                    data-cat-item
                    onClick={() => handleSelect(cat)}
                    id={`${id}-opt-${cat.id}`}
                    disabled={disabled}
                    aria-disabled={disabled}
                    className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm transition-colors ${
                      disabled
                        ? "cursor-not-allowed text-text-muted opacity-60"
                        : highlighted
                          ? "bg-accent-dim text-accent"
                          : "text-text-primary hover:bg-surface-raised"
                    }`}
                    role="option"
                    aria-selected={cat.id === value}
                  >
                    <span>
                      {cat.name}
                      {disabled && (
                        <span className="ml-1.5 text-[11px] italic text-text-muted">(already added)</span>
                      )}
                    </span>
                    {cat.description && <span className="ml-2 truncate text-xs text-text-muted">{cat.description}</span>}
                  </button>
                );
              })}
            </div>
          ))}

          {filtered.length === 0 && (
            <div className="px-3 py-4 text-center text-sm text-text-muted">No categories match</div>
          )}

          <div className="sticky bottom-0 border-t border-border-subtle bg-surface">
            <button
              type="button"
              onMouseDown={(e) => {
                // Prevent the input's blur/outside-click handler from firing
                // before the click registers; keeps the dropdown open while
                // the modal mounts.
                e.preventDefault();
              }}
              onClick={() => setShowAddModal(true)}
              className="flex w-full items-center px-3 py-2 text-left text-sm font-medium text-accent transition-colors hover:bg-accent-dim"
            >
              + Add category
            </button>
          </div>
        </div>
      )}

      {showAddModal && (
        <AddCategoryModal
          initialName={query}
          // In `bothOnly` (transfer) mode we default the new category to
          // "both" without locking the type, so the user can still pick
          // a different type if they realized the picker context was
          // wrong. For "income"/"expense" pickers the type stays locked.
          initialType={bothOnly ? "both" : (effectiveFilterType ?? "both")}
          lockedType={bothOnly ? undefined : effectiveFilterType}
          masterCategories={masters}
          onCreated={(cat) => {
            setShowAddModal(false);
            // Always notify upward so other pickers can list the
            // newly-created category. But in ``bothOnly`` (transfer)
            // mode the modal's type radio is unlocked — if the user
            // changed it to income or expense, the new category is
            // incompatible with the transfer picker. Do NOT
            // ``handleSelect`` in that case, otherwise the form would
            // store an id the backend rejects at submit. Architect
            // feedback on PR #296.
            onCategoryCreated?.(cat);
            if (bothOnly && cat.type !== "both") {
              return;
            }
            handleSelect(cat);
          }}
          onCancel={() => {
            setShowAddModal(false);
            inputRef.current?.focus();
          }}
        />
      )}
    </div>
  );
}
