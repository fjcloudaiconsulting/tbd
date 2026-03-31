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
  const ref = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const selected = categories.find((c) => c.id === value);

  // Filter categories: only subcategories (parent_id set) or masters with no children
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

  // Group non-recent by master
  const masters = categories.filter((c) => c.parent_id === null);
  const grouped: { label: string; items: Category[] }[] = [];
  for (const master of masters) {
    const items = nonRecent.filter((c) => c.parent_id === master.id);
    if (items.length > 0) grouped.push({ label: master.name, items });
  }
  // Masterless (custom with no parent)
  const masterless = nonRecent.filter((c) => c.parent_id === null);
  if (masterless.length > 0) grouped.push({ label: "Other", items: masterless });

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  function handleSelect(cat: Category) {
    onChange(cat.id);
    saveRecent(cat.id);
    setQuery("");
    setOpen(false);
  }

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
        ref={inputRef}
        className={className}
      />
      {/* Hidden input for form required validation */}
      <input type="hidden" name={`${id}-value`} value={value} required />

      {open && (
        <div className="absolute z-50 mt-1 max-h-64 w-full overflow-y-auto rounded-md border border-border bg-surface shadow-lg">
          {recentItems.length > 0 && (
            <>
              <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">Recent</div>
              {recentItems.map((cat) => (
                <button key={cat.id} type="button" onClick={() => handleSelect(cat)}
                  className="flex w-full items-center justify-between px-3 py-2 text-left text-sm text-text-primary hover:bg-surface-raised">
                  <span>{cat.name}</span>
                  <span className="text-xs text-text-muted">{cat.parent_name}</span>
                </button>
              ))}
              <div className="border-t border-border-subtle" />
            </>
          )}

          {grouped.map((group) => (
            <div key={group.label}>
              <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-text-muted">{group.label}</div>
              {group.items.map((cat) => (
                <button key={cat.id} type="button" onClick={() => handleSelect(cat)}
                  className="flex w-full items-center justify-between px-3 py-2 text-left text-sm text-text-primary hover:bg-surface-raised">
                  <span>{cat.name}</span>
                  {cat.description && <span className="ml-2 truncate text-xs text-text-muted">{cat.description}</span>}
                </button>
              ))}
            </div>
          ))}

          {filtered.length === 0 && (
            <div className="px-3 py-4 text-center text-sm text-text-muted">No categories match</div>
          )}
        </div>
      )}
    </div>
  );
}
