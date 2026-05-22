"use client";

/**
 * Category filter — tree-style picker that respects the
 * master / sub category hierarchy from the existing
 * ``backend/app/models/category.py`` model.
 *
 * Master row: checkbox toggles ALL sub categories under that master.
 * When some but not all subs are selected, the master shows the
 * indeterminate / partial state.
 * Sub row: checkbox toggles its own id. Unselecting a sub while the
 * master is fully checked leaves the master partial.
 *
 * Search input filters the tree by name; matching subs keep their
 * master visible (collapsed if the master itself doesn't match).
 *
 * Returns ``category_ids: number[]`` — IDs of every selected master
 * AND sub. The widget AST filter on ``category_id IN (...)`` doesn't
 * understand the hierarchy, so we always materialize the full list.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import useSWR from "swr";

import { apiFetch } from "@/lib/api";
import type { Category } from "@/lib/types";

interface Props {
  value: number[];
  onChange: (next: number[]) => void;
  label?: string;
}

const CATEGORIES_SWR_KEY = "/api/v1/categories?for=reports-filter";

async function fetchCategories(): Promise<Category[]> {
  return apiFetch<Category[]>("/api/v1/categories");
}

interface TreeNode {
  master: Category;
  subs: Category[];
}

function buildTree(cats: Category[]): TreeNode[] {
  const masters = cats.filter((c) => c.parent_id === null);
  return masters
    .map((m) => ({
      master: m,
      subs: cats.filter((c) => c.parent_id === m.id),
    }))
    .sort((a, b) => a.master.name.localeCompare(b.master.name));
}

export default function CategoryPicker({
  value,
  onChange,
  label = "Categories",
}: Props) {
  const { data, error, isLoading } = useSWR<Category[]>(
    CATEGORIES_SWR_KEY,
    fetchCategories,
    { revalidateOnFocus: false },
  );

  const [search, setSearch] = useState("");
  const selected = useMemo(() => new Set(value), [value]);
  const cats = data ?? [];
  const tree = useMemo(() => buildTree(cats), [cats]);

  const visibleTree = useMemo(() => {
    if (!search.trim()) return tree;
    const q = search.toLowerCase();
    return tree
      .map((node) => {
        const masterMatches = node.master.name.toLowerCase().includes(q);
        const subs = node.subs.filter((s) => s.name.toLowerCase().includes(q));
        if (masterMatches) return { master: node.master, subs: node.subs };
        if (subs.length > 0) return { master: node.master, subs };
        return null;
      })
      .filter((n): n is TreeNode => n !== null);
  }, [tree, search]);

  function toggleMaster(node: TreeNode) {
    const ids = [node.master.id, ...node.subs.map((s) => s.id)];
    const allSelected = ids.every((id) => selected.has(id));
    if (allSelected) {
      onChange(value.filter((v) => !ids.includes(v)));
    } else {
      const next = new Set(value);
      for (const id of ids) next.add(id);
      onChange([...next]);
    }
  }

  function toggleSub(sub: Category) {
    const next = selected.has(sub.id)
      ? value.filter((v) => v !== sub.id)
      : [...value, sub.id];
    onChange(next);
  }

  return (
    <div className="flex flex-col gap-1.5" data-testid="category-picker">
      <span className="text-[10px] font-medium uppercase tracking-wider text-text-muted">
        {label}
      </span>
      {error ? (
        <div
          role="alert"
          data-testid="category-picker-error"
          className="text-xs text-danger"
        >
          Couldn&apos;t load categories
        </div>
      ) : isLoading ? (
        <div
          data-testid="category-picker-loading"
          className="h-6 w-32 animate-pulse rounded bg-border/40"
        />
      ) : cats.length === 0 ? (
        <span className="text-xs text-text-muted">No categories yet</span>
      ) : (
        <>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            data-testid="category-picker-search"
            aria-label="Search categories"
            placeholder="Search categories..."
            className="rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
          />
          <div className="max-h-56 overflow-y-auto rounded-md border border-border bg-bg p-2">
            {visibleTree.length === 0 ? (
              <span className="text-xs text-text-muted">No categories match</span>
            ) : (
              <ul className="flex flex-col gap-1">
                {visibleTree.map((node) => (
                  <CategoryTreeRow
                    key={node.master.id}
                    node={node}
                    selected={selected}
                    onToggleMaster={() => toggleMaster(node)}
                    onToggleSub={toggleSub}
                  />
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function CategoryTreeRow({
  node,
  selected,
  onToggleMaster,
  onToggleSub,
}: {
  node: TreeNode;
  selected: Set<number>;
  onToggleMaster: () => void;
  onToggleSub: (sub: Category) => void;
}) {
  const masterRef = useRef<HTMLInputElement>(null);
  const allIds = [node.master.id, ...node.subs.map((s) => s.id)];
  const selCount = allIds.filter((id) => selected.has(id)).length;
  const total = allIds.length;
  const allChecked = selCount === total;
  const partial = selCount > 0 && selCount < total;

  // The HTML input doesn't have an attribute for indeterminate; it's
  // a DOM-only property. Sync it whenever the count changes.
  useEffect(() => {
    if (masterRef.current) masterRef.current.indeterminate = partial;
  }, [partial]);

  return (
    <li>
      <label className="flex items-center gap-2 text-sm text-text-primary">
        <input
          ref={masterRef}
          type="checkbox"
          data-testid={`category-master-${node.master.id}`}
          checked={allChecked}
          onChange={onToggleMaster}
          aria-checked={partial ? "mixed" : allChecked}
          aria-label={`Category ${node.master.name}`}
        />
        <span className="font-medium">{node.master.name}</span>
        <span className="text-[10px] text-text-muted">
          {selCount}/{total}
        </span>
      </label>
      {node.subs.length > 0 && (
        <ul className="ml-5 mt-1 flex flex-col gap-0.5">
          {node.subs.map((s) => (
            <li key={s.id}>
              <label className="flex items-center gap-2 text-xs text-text-secondary">
                <input
                  type="checkbox"
                  data-testid={`category-sub-${s.id}`}
                  checked={selected.has(s.id)}
                  onChange={() => onToggleSub(s)}
                  aria-label={`Category ${s.name}`}
                />
                <span>{s.name}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}
