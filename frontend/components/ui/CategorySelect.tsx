"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
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
  className?: string;
  "aria-label"?: string;
}

export default function CategorySelect({ id, categories, value, onChange, filterType, className = "", "aria-label": ariaLabel }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const ref = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const selected = categories.find((c) => c.id === value);

  // Precompute parent IDs set and selectable items (O(n) instead of O(n^2))
  const { selectable, parentIds } = useMemo(() => {
    const pIds = new Set<number>();
    for (const c of categories) {
      if (c.parent_id !== null) pIds.add(c.parent_id);
    }
    const items = categories.filter((c) => {
      if (filterType && c.type !== filterType && c.type !== "both") return false;
      return c.parent_id !== null || !pIds.has(c.id);
    });
    return { selectable: items, parentIds: pIds };
  }, [categories, filterType]);

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

  // Build ordered flat list for keyboard nav
  const flatList: Category[] = [...recentItems, ...nonRecent];

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  useEffect(() => { setHighlightIdx(-1); }, [query, open]);

  function handleSelect(cat: Category) {
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

  // Group non-recent by master for display
  const masters = categories.filter((c) => c.parent_id === null);
  const grouped = useMemo(() => {
    const groups: { label: string; items: Category[] }[] = [];
    for (const master of masters) {
      const items = nonRecent.filter((c) => c.parent_id === master.id);
      if (items.length > 0) groups.push({ label: master.name, items });
    }
    const masterless = nonRecent.filter((c) => c.parent_id === null);
    if (masterless.length > 0) groups.push({ label: "Other", items: masterless });
    return groups;
  }, [masters, nonRecent]);

  const activeDescendant = highlightIdx >= 0 && highlightIdx < flatList.length
    ? `${id}-opt-${flatList[highlightIdx].id}` : undefined;

  let itemIdx = recentItems.length;

  return (
    <div ref={ref} className="relative">
      <input
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
              {recentItems.map((cat, i) => (
                <button key={cat.id} type="button" data-cat-item onClick={() => handleSelect(cat)}
                  id={`${id}-opt-${cat.id}`}
                  className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm transition-colors ${highlightIdx === i ? "bg-accent-dim text-accent" : "text-text-primary hover:bg-surface-raised"}`}
                  role="option" aria-selected={cat.id === value}>
                  <span>{cat.name}</span>
                  <span className="text-xs text-text-muted">{cat.parent_name}</span>
                </button>
              ))}
              <div className="border-t border-border-subtle" />
            </>
          )}

          {grouped.map((group) => {
            const startIdx = itemIdx;
            itemIdx += group.items.length;
            return (
              <div key={group.label}>
                <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">{group.label}</div>
                {group.items.map((cat, i) => (
                  <button key={cat.id} type="button" data-cat-item onClick={() => handleSelect(cat)}
                    id={`${id}-opt-${cat.id}`}
                    className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm transition-colors ${highlightIdx === startIdx + i ? "bg-accent-dim text-accent" : "text-text-primary hover:bg-surface-raised"}`}
                    role="option" aria-selected={cat.id === value}>
                    <span>{cat.name}</span>
                    {cat.description && <span className="ml-2 truncate text-xs text-text-muted">{cat.description}</span>}
                  </button>
                ))}
              </div>
            );
          })}

          {filtered.length === 0 && (
            <div className="px-3 py-4 text-center text-sm text-text-muted">No categories match</div>
          )}
        </div>
      )}
    </div>
  );
}
