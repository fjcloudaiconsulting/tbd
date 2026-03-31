"use client";

import { useEffect, useRef, useState } from "react";
import type { Category } from "@/lib/types";

const RECENT_KEY = "pfv2-recent-categories";
const MAX_RECENT = 5;

function getRecent(): number[] {
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
}

export default function CategorySelect({ id, categories, value, onChange, filterType, className = "" }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlightIdx, setHighlightIdx] = useState(-1);
  const ref = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const selected = categories.find((c) => c.id === value);

  // Build flat list of selectable items
  const selectable = categories.filter((c) => {
    if (filterType && c.type !== filterType && c.type !== "both") return false;
    const hasChildren = categories.some((ch) => ch.parent_id === c.id);
    return c.parent_id !== null || !hasChildren;
  });

  const q = query.toLowerCase();
  const filtered = q
    ? selectable.filter((c) => c.name.toLowerCase().includes(q) || (c.parent_name?.toLowerCase().includes(q) ?? false))
    : selectable;

  // Recent items at top
  const recentIds = getRecent();
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

  function handleKeyDown(e: React.KeyboardEvent) {
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

  // Scroll highlighted item into view
  useEffect(() => {
    if (highlightIdx < 0 || !listRef.current) return;
    const items = listRef.current.querySelectorAll("[data-cat-item]");
    items[highlightIdx]?.scrollIntoView({ block: "nearest" });
  }, [highlightIdx]);

  // Group non-recent by master for display
  const masters = categories.filter((c) => c.parent_id === null);
  const grouped: { label: string; items: Category[] }[] = [];
  for (const master of masters) {
    const items = nonRecent.filter((c) => c.parent_id === master.id);
    if (items.length > 0) grouped.push({ label: master.name, items });
  }
  const masterless = nonRecent.filter((c) => c.parent_id === null);
  if (masterless.length > 0) grouped.push({ label: "Other", items: masterless });

  let itemIdx = recentItems.length; // track index for highlight

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
      />
      <input type="hidden" name={`${id}-value`} value={value} required />

      {open && (
        <div ref={listRef} role="listbox" className="absolute z-50 mt-1 max-h-64 w-full overflow-y-auto rounded-md border border-border bg-surface shadow-lg">
          {recentItems.length > 0 && (
            <>
              <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">Recent</div>
              {recentItems.map((cat, i) => (
                <button key={cat.id} type="button" data-cat-item onClick={() => handleSelect(cat)}
                  className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm transition-colors ${highlightIdx === i ? "bg-accent-dim text-accent" : "text-text-primary hover:bg-surface-raised"}`}
                  role="option" aria-selected={highlightIdx === i}>
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
                    className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm transition-colors ${highlightIdx === startIdx + i ? "bg-accent-dim text-accent" : "text-text-primary hover:bg-surface-raised"}`}
                    role="option" aria-selected={highlightIdx === startIdx + i}>
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
