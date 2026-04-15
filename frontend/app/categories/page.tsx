"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import AppShell from "@/components/AppShell";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { input, btnPrimary, card, cardHeader, cardTitle, error as errorCls, pageTitle } from "@/lib/styles";
import type { Category } from "@/lib/types";
import ConfirmModal from "@/components/ui/ConfirmModal";
import {
  Wallet, Home, Zap, UtensilsCrossed, Car, HeartPulse,
  Scissors, Gamepad2, Target, CreditCard, Gift, HelpCircle, Tag,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

const CATEGORY_ICONS: Record<string, LucideIcon> = {
  income: Wallet,
  housing: Home,
  utilities: Zap,
  food_dining: UtensilsCrossed,
  transportation: Car,
  health: HeartPulse,
  personal_care: Scissors,
  lifestyle: Gamepad2,
  financial_goals: Target,
  debt: CreditCard,
  giving: Gift,
  miscellaneous: HelpCircle,
};

const TYPE_COLORS: Record<Category["type"], string> = {
  income: "text-success",
  expense: "text-danger",
  both: "text-text-muted",
};

export default function CategoriesPage() {
  const { user, loading } = useAuth();
  const [categories, setCategories] = useState<Category[]>([]);
  const [fetching, setFetching] = useState(true);
  const [error, setError] = useState("");

  // Edit
  const [editingCatId, setEditingCatId] = useState<number | null>(null);
  const [editCatName, setEditCatName] = useState("");

  // Add subcategory form
  const [addingToMaster, setAddingToMaster] = useState<number | null>(null);
  const [newSubName, setNewSubName] = useState("");
  const [newSubDesc, setNewSubDesc] = useState("");

  // Search
  const [search, setSearch] = useState("");

  // Add custom master form
  const [showAddMaster, setShowAddMaster] = useState(false);
  const [newMasterName, setNewMasterName] = useState("");
  const [newMasterType, setNewMasterType] = useState<"income" | "expense" | "both">("expense");
  const [newMasterDesc, setNewMasterDesc] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  const reload = useCallback(async () => {
    const data = await apiFetch<Category[]>("/api/v1/categories");
    setCategories(data ?? []);
    setFetching(false);
  }, []);

  useEffect(() => {
    if (!loading && user) reload().catch(() => setFetching(false));
  }, [loading, user, reload]);

  const { allMasters, childrenMap } = useMemo(() => {
    const masters = categories.filter((c) => c.parent_id === null);
    const map = new Map<number, Category[]>();
    for (const c of categories) {
      if (c.parent_id !== null) {
        const list = map.get(c.parent_id) ?? [];
        list.push(c);
        map.set(c.parent_id, list);
      }
    }
    return { allMasters: masters, childrenMap: map };
  }, [categories]);
  const childrenOf = (parentId: number) => childrenMap.get(parentId) ?? [];

  const sq = search.toLowerCase();
  const masters = sq
    ? allMasters.filter((m) => {
        if (m.name.toLowerCase().includes(sq)) return true;
        return childrenOf(m.id).some((c) => c.name.toLowerCase().includes(sq));
      })
    : allMasters;

  async function handleAddSub(e: FormEvent) {
    e.preventDefault();
    if (!addingToMaster) return;
    const master = categories.find((c) => c.id === addingToMaster);
    setError("");
    try {
      await apiFetch("/api/v1/categories", {
        method: "POST",
        body: JSON.stringify({
          name: newSubName,
          description: newSubDesc || null,
          parent_id: addingToMaster,
          type: master?.type ?? "expense",
        }),
      });
      setNewSubName("");
      setNewSubDesc("");
      setAddingToMaster(null);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleAddMaster(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await apiFetch("/api/v1/categories", {
        method: "POST",
        body: JSON.stringify({
          name: newMasterName,
          type: newMasterType,
          description: newMasterDesc || null,
        }),
      });
      setNewMasterName("");
      setNewMasterDesc("");
      setShowAddMaster(false);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleEditCat(id: number) {
    if (!editCatName.trim()) return;
    setError("");
    try {
      await apiFetch(`/api/v1/categories/${id}`, {
        method: "PUT",
        body: JSON.stringify({ name: editCatName }),
      });
      setEditingCatId(null);
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  async function handleDelete(id: number) {
    setConfirmDeleteId(null);
    setError("");
    try {
      await apiFetch(`/api/v1/categories/${id}`, { method: "DELETE" });
      await reload();
    } catch (err) { setError(extractErrorMessage(err)); }
  }

  return (
    <AppShell>
      <div className="mb-8 flex items-center justify-between">
        <h1 className={`${pageTitle} mb-0`}>Categories</h1>
        <button onClick={() => setShowAddMaster(!showAddMaster)} className={btnPrimary}>
          {showAddMaster ? "Cancel" : "+ Custom Category"}
        </button>
      </div>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {showAddMaster && (
        <div className={`mb-6 ${card} p-6`}>
          <h2 className={`mb-4 ${cardTitle}`}>New Master Category</h2>
          <form onSubmit={handleAddMaster} className="flex flex-wrap gap-3">
            <div className="flex-1 min-w-[200px]">
              <label htmlFor="master-name" className="sr-only">Name</label>
              <input id="master-name" type="text" required placeholder="Category name" value={newMasterName} onChange={(e) => setNewMasterName(e.target.value)} className={input} />
            </div>
            <div className="w-32">
              <label htmlFor="master-type" className="sr-only">Type</label>
              <select id="master-type" value={newMasterType} onChange={(e) => setNewMasterType(e.target.value as typeof newMasterType)} className={input}>
                <option value="expense">Expense</option>
                <option value="income">Income</option>
                <option value="both">Both</option>
              </select>
            </div>
            <div className="flex-1 min-w-[200px]">
              <label htmlFor="master-desc" className="sr-only">Description</label>
              <input id="master-desc" type="text" placeholder="Description (optional)" value={newMasterDesc} onChange={(e) => setNewMasterDesc(e.target.value)} className={input} />
            </div>
            <button type="submit" className={btnPrimary}>Add</button>
          </form>
        </div>
      )}

      {!fetching && allMasters.length > 6 && (
        <div className="mb-4">
          <label htmlFor="cat-search" className="sr-only">Search categories</label>
          <input id="cat-search" type="text" placeholder="Search categories..." value={search} onChange={(e) => setSearch(e.target.value)} className={`max-w-sm ${input}`} />
        </div>
      )}

      {fetching ? (
        <Spinner />
      ) : (
        <div className="space-y-4">
          {masters.map((master) => {
            const subs = childrenOf(master.id);
            const Icon = (master.slug && CATEGORY_ICONS[master.slug]) || Tag;
            return (
              <div key={master.id} className={card}>
                <div className={`flex items-center justify-between ${cardHeader}`}>
                  <div className="flex items-center gap-2.5">
                    <Icon className="h-4 w-4 text-text-muted" />
                    {editingCatId === master.id ? (
                      <div className="flex items-center gap-2">
                        <input type="text" value={editCatName} onChange={(e) => setEditCatName(e.target.value)} className={`text-sm ${input}`} autoFocus
                          onKeyDown={(e) => { if (e.key === "Enter") handleEditCat(master.id); if (e.key === "Escape") setEditingCatId(null); }} />
                        <button onClick={() => handleEditCat(master.id)} className="text-xs text-accent">Save</button>
                        <button onClick={() => setEditingCatId(null)} className="text-xs text-text-muted">Cancel</button>
                      </div>
                    ) : (
                      <>
                        <h2 className="text-sm font-medium text-text-primary">{master.name}</h2>
                        <span className={`text-[11px] font-medium ${TYPE_COLORS[master.type]}`}>{master.type}</span>
                        {master.is_system && <span className="rounded bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium text-text-muted">system</span>}
                      </>
                    )}
                  </div>
                  <div className="flex items-center gap-3">
                    <button
                      onClick={() => { setAddingToMaster(addingToMaster === master.id ? null : master.id); setNewSubName(""); setNewSubDesc(""); }}
                      className="text-xs text-accent hover:text-accent-hover"
                    >
                      {addingToMaster === master.id ? "Cancel" : "+ Add Sub"}
                    </button>
                    <button onClick={() => { setEditingCatId(master.id); setEditCatName(master.name); }} className="text-xs text-text-muted hover:text-accent">Edit</button>
                    <button onClick={() => setConfirmDeleteId(master.id)} aria-label={`Delete ${master.name}`} className="text-xs text-text-muted hover:text-danger">Delete</button>
                  </div>
                </div>

                {master.description && (
                  <p className="px-6 pt-2 text-xs text-text-muted">{master.description}</p>
                )}

                <div className="px-6 py-3">
                  {addingToMaster === master.id && (
                    <form onSubmit={handleAddSub} className="mb-3 flex gap-2">
                      <div className="flex-1">
                        <label htmlFor={`sub-name-${master.id}`} className="sr-only">Subcategory name</label>
                        <input id={`sub-name-${master.id}`} type="text" required placeholder="Subcategory name" value={newSubName} onChange={(e) => setNewSubName(e.target.value)} className={input} autoFocus />
                      </div>
                      <div className="flex-1">
                        <label htmlFor={`sub-desc-${master.id}`} className="sr-only">Description</label>
                        <input id={`sub-desc-${master.id}`} type="text" placeholder="Hint / description" value={newSubDesc} onChange={(e) => setNewSubDesc(e.target.value)} className={input} />
                      </div>
                      <button type="submit" className={btnPrimary}>Add</button>
                    </form>
                  )}

                  {subs.length > 0 ? (
                    <div className="space-y-0.5">
                      {subs.map((sub) => (
                        <div key={sub.id} className="flex items-center justify-between rounded-md px-3 py-2 transition-colors hover:bg-surface-raised">
                          {editingCatId === sub.id ? (
                            <div className="flex flex-1 items-center gap-2">
                              <input type="text" value={editCatName} onChange={(e) => setEditCatName(e.target.value)} className={`flex-1 text-sm ${input}`} autoFocus
                                onKeyDown={(e) => { if (e.key === "Enter") handleEditCat(sub.id); if (e.key === "Escape") setEditingCatId(null); }} />
                              <button onClick={() => handleEditCat(sub.id)} className="text-xs text-accent">Save</button>
                              <button onClick={() => setEditingCatId(null)} className="text-xs text-text-muted">Cancel</button>
                            </div>
                          ) : (
                            <>
                              <div>
                                <span className="text-sm text-text-primary">{sub.name}</span>
                                {sub.description && <span className="ml-2 text-xs text-text-muted">{sub.description}</span>}
                                <span className="ml-2 text-xs text-text-muted" title={`${sub.transaction_count} transaction(s)`}>{sub.transaction_count}</span>
                              </div>
                              <div className="flex gap-2">
                                <button onClick={() => { setEditingCatId(sub.id); setEditCatName(sub.name); }} className="text-xs text-text-muted hover:text-accent">Edit</button>
                                <button onClick={() => setConfirmDeleteId(sub.id)} aria-label={`Delete ${sub.name}`} className="text-xs text-text-muted hover:text-danger">Delete</button>
                              </div>
                            </>
                          )}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-text-muted py-1">No subcategories</p>
                  )}
                </div>
              </div>
            );
          })}

          {masters.length === 0 && (
            <div className={`${card} p-8 text-center`}>
              <p className="text-sm text-text-muted">No categories yet. Register a new account to seed system categories.</p>
            </div>
          )}
        </div>
      )}
      <ConfirmModal
        open={confirmDeleteId !== null}
        title="Delete Category"
        message="Delete this category?"
        confirmLabel="Delete"
        variant="danger"
        onConfirm={() => confirmDeleteId !== null && handleDelete(confirmDeleteId)}
        onCancel={() => setConfirmDeleteId(null)}
      />
    </AppShell>
  );
}
