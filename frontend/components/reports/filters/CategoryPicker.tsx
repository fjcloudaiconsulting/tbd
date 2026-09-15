"use client";

/**
 * Category filter — tree-style picker that respects the
 * master / sub category hierarchy from the existing
 * ``backend/app/models/category.py`` model.
 *
 * Two modes:
 *  - Default (Reports): the master checkbox toggles the master AND all its
 *    subs. When some but not all are selected it shows the indeterminate /
 *    partial state, so unselecting a sub under a fully checked master leaves
 *    the master partial.
 *  - ``ownRow`` (transactions panel, TBD-464 option C): the master checkbox
 *    is a standard tri-state group toggle over the master AND all its subs,
 *    including subs a search hides. A master with subs gets an extra first
 *    child row, "<Master> (other)", toggling only the master's id, when it
 *    holds transactions of its own or is selected outside a fully checked
 *    group. The group's checked / partial state is derived from its VISIBLE
 *    rows, so a master whose (other) row is hidden does not affect it.
 * Sub row (both modes): checkbox toggles its own id.
 *
 * Search input filters the tree by name; matching subs keep their
 * master visible (collapsed if the master itself doesn't match).
 *
 * Returns ``category_ids: number[]`` — IDs of every selected master
 * AND sub. The widget AST filter on ``category_id IN (...)`` doesn't
 * understand the hierarchy, so we always materialize the full list.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { useAuth } from "@/components/auth/AuthProvider";
import { OWN_ITEM_SUFFIX } from "@/components/ui/CategorySelect";
import { useCategories } from "@/lib/hooks/use-categories";
import type { Category } from "@/lib/types";

interface Props {
  value: number[];
  onChange: (next: number[]) => void;
  label?: string;
  /**
   * TBD-464 option C (transactions panel): a tri-state group toggle plus a
   * "<Master> (other)" row for the master's own transactions. See the file
   * header. Default (Reports): the master toggles its whole subtree.
   */
  ownRow?: boolean;
}

interface TreeNode {
  master: Category;
  subs: Category[];
  /** False when a search hides the master's own name (and its (other) row). */
  masterMatches?: boolean;
  /** Every sub of the master, when `subs` is narrowed by a search. */
  allSubs?: Category[];
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
  ownRow = false,
}: Props) {
  // Share the org categories cache via the bare-path `useCategories` hook,
  // auth-gated (`!loading && !!user`) like the page-level consumers.
  const { user, loading } = useAuth();
  const enabled = !loading && !!user;
  const { data, error, isLoading } = useCategories(enabled);

  const [search, setSearch] = useState("");
  const selected = useMemo(() => new Set(value), [value]);
  // Wrap in useMemo so the `?? []` fallback doesn't mint a fresh array
  // every render, which would destabilize the `tree` useMemo below.
  const cats = useMemo(() => data ?? [], [data]);
  const tree = useMemo(() => buildTree(cats), [cats]);

  const visibleTree = useMemo(() => {
    if (!search.trim()) return tree;
    const q = search.toLowerCase();
    return tree
      .map((node): TreeNode | null => {
        const masterMatches = node.master.name.toLowerCase().includes(q);
        const subs = node.subs.filter((s) => s.name.toLowerCase().includes(q));
        if (masterMatches) return { master: node.master, subs: node.subs, masterMatches };
        if (subs.length > 0) return { master: node.master, subs, masterMatches, allSubs: node.subs };
        return null;
      })
      .filter((n): n is TreeNode => n !== null);
  }, [tree, search]);

  function toggleMaster(visible: TreeNode) {
    if (ownRow) {
      // The row hands over the search-filtered node, but the group toggle
      // covers the WHOLE group: every sub, and always the master's own id,
      // even when its (other) row is hidden. A fully checked group is then
      // the same rows as the subtree.
      const node = tree.find((n) => n.master.id === visible.master.id) ?? visible;
      const ids = [node.master.id, ...node.subs.map((s) => s.id)];
      const checked = groupState(visible, selected).checked;
      onChange(
        checked
          ? value.filter((v) => !ids.includes(v))
          : [...new Set([...value, ...ids])],
      );
      return;
    }
    const node = visible;
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
      {label && (
        <span className="text-[10px] font-medium uppercase tracking-wider text-text-muted">
          {label}
        </span>
      )}
      {error ? (
        <div
          role="alert"
          data-testid="category-picker-error"
          className="text-xs text-danger"
        >
          Couldn&apos;t load categories
        </div>
      ) : isLoading || !enabled || (data === undefined && !error) ? (
        // Treat "auth gate off" and "data not yet arrived" as loading, not
        // empty — otherwise a picker mounted above an auth/loading gate would
        // flash the "No categories yet" empty state on a null SWR key.
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
          {/* TBD-464: the tree must never scroll sideways in the 16rem side
              panel. overflow-y-auto alone computes overflow-x to auto, so a
              name that cannot shrink became a horizontal scrollbar. Names
              wrap instead (min-w-0 + overflow-wrap:anywhere, so even one long
              token breaks); checkboxes and counts never shrink. The p-2 inset
              keeps the 2px + 2px focus outline inside the clip. */}
          <div role="group" aria-label={label || "Categories"} className="max-h-56 overflow-y-auto overflow-x-hidden rounded-md border border-border bg-bg p-2">
            {visibleTree.length === 0 ? (
              <span className="text-xs text-text-muted">No categories match</span>
            ) : (
              <ul className="flex flex-col gap-1">
                {visibleTree.map((node) => (
                  <CategoryTreeRow
                    key={node.master.id}
                    node={node}
                    selected={selected}
                    ownRow={ownRow}
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

// ownRow mode: whether a master's "(other)" row is shown. A master with subs
// shows it when it holds transactions of its own, OR when its id is selected
// outside a fully checked group. Without that second arm, unchecking every sub
// of a checked group (or a saved `[M]`) leaves M selected, sent and invisible:
// an empty list with nothing on screen to explain or clear it. A hidden M can
// therefore only exist inside a fully checked group. A search that hides the
// master's name hides the row too.
function showsOtherRow(node: TreeNode, selected: Set<number>): boolean {
  if (node.subs.length === 0 || node.masterMatches === false) return false;
  const allSubs = node.allSubs ?? node.subs;
  return (
    node.master.transaction_count > 0 ||
    (selected.has(node.master.id) && !allSubs.every((s) => selected.has(s.id)))
  );
}

// ownRow mode: the group's state over its VISIBLE rows. A master with no subs
// is a plain leaf, its own row.
function groupState(node: TreeNode, selected: Set<number>) {
  const ids =
    node.subs.length === 0
      ? [node.master.id]
      : [...(showsOtherRow(node, selected) ? [node.master.id] : []), ...node.subs.map((s) => s.id)];
  const count = ids.filter((id) => selected.has(id)).length;
  return {
    count,
    total: ids.length,
    checked: ids.length > 0 && count === ids.length,
    partial: count > 0 && count < ids.length,
  };
}

function CategoryTreeRow({
  node,
  selected,
  ownRow,
  onToggleMaster,
  onToggleSub,
}: {
  node: TreeNode;
  selected: Set<number>;
  ownRow: boolean;
  onToggleMaster: () => void;
  onToggleSub: (sub: Category) => void;
}) {
  const masterRef = useRef<HTMLInputElement>(null);
  const allIds = [node.master.id, ...node.subs.map((s) => s.id)];
  const own = ownRow ? groupState(node, selected) : null;
  const selCount = own ? own.count : allIds.filter((id) => selected.has(id)).length;
  const total = own ? own.total : allIds.length;
  const allChecked = own ? own.checked : selCount === total;
  const partial = own ? own.partial : selCount > 0 && selCount < total;
  const otherRow = ownRow && showsOtherRow(node, selected);

  // The HTML input doesn't have an attribute for indeterminate; it's
  // a DOM-only property. Sync it whenever the count changes.
  useEffect(() => {
    if (masterRef.current) masterRef.current.indeterminate = partial;
  }, [partial]);

  return (
    <li>
      <label className="flex min-h-[44px] items-center gap-2 text-sm text-text-primary xl:min-h-0">
        <input
          ref={masterRef}
          type="checkbox"
          className="shrink-0"
          data-testid={`category-master-${node.master.id}`}
          checked={allChecked}
          onChange={onToggleMaster}
          aria-label={`Category ${node.master.name}`}
        />
        <span className="min-w-0 font-medium [overflow-wrap:anywhere]">{node.master.name}</span>
        <span data-testid={`category-count-${node.master.id}`} className="shrink-0 text-[10px] text-text-muted">
          {selCount}/{total}
        </span>
      </label>
      {node.subs.length > 0 && (
        <ul className="ml-5 mt-1 flex flex-col gap-0.5">
          {otherRow && (
            <li>
              <label className="flex min-h-[44px] items-center gap-2 text-xs text-text-secondary xl:min-h-0">
                <input
                  type="checkbox"
                  className="shrink-0"
                  data-testid={`category-own-${node.master.id}`}
                  checked={selected.has(node.master.id)}
                  onChange={() => onToggleSub(node.master)}
                  aria-label={`Category ${node.master.name} ${OWN_ITEM_SUFFIX}`}
                />
                <span className="min-w-0 [overflow-wrap:anywhere]">{node.master.name} {OWN_ITEM_SUFFIX}</span>
              </label>
            </li>
          )}
          {node.subs.map((s) => (
            <li key={s.id}>
              <label className="flex min-h-[44px] items-center gap-2 text-xs text-text-secondary xl:min-h-0">
                <input
                  type="checkbox"
                  className="shrink-0"
                  data-testid={`category-sub-${s.id}`}
                  checked={selected.has(s.id)}
                  onChange={() => onToggleSub(s)}
                  aria-label={`Category ${s.name}`}
                />
                <span className="min-w-0 [overflow-wrap:anywhere]">{s.name}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}
