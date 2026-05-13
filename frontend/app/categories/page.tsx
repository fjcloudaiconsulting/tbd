"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import AppShell from "@/components/AppShell";
import HelpAnchor from "@/components/HelpAnchor";
import Tooltip from "@/components/Tooltip";
import Spinner from "@/components/ui/Spinner";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { useTransactionAddedListener } from "@/lib/hooks/use-transaction-added";
import { input, btnPrimary, btnSecondary, card, cardHeader, error as errorCls, pageTitle } from "@/lib/styles";
import type { Category } from "@/lib/types";
import ConfirmModal from "@/components/ui/ConfirmModal";
import AddMasterWithSubsModal from "@/components/ui/AddMasterWithSubsModal";
import BatchActionBar from "@/components/categories/BatchActionBar";
import BatchMoveModal from "@/components/categories/BatchMoveModal";
import BatchDeleteModal from "@/components/categories/BatchDeleteModal";
import DraggableSubcategoryRow from "@/components/categories/DraggableSubcategoryRow";
import MasterDropZone from "@/components/categories/MasterDropZone";
import DragMoveConfirmModal from "@/components/categories/DragMoveConfirmModal";
import { buildMoveErrorMessage, classifyDrop } from "@/components/categories/dragMoveHelpers";
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  TouchSensor,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import type { DragEndEvent, DragStartEvent } from "@dnd-kit/core";
import {
  Wallet, Home, Zap, UtensilsCrossed, Car, HeartPulse,
  Scissors, Gamepad2, Target, CreditCard, Gift, HelpCircle, Tag,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

interface CategoryMoveResult {
  category_id: number;
  source_master_id: number;
  target_master_id: number;
  affected_transaction_count: number;
  affected_recurring_count: number;
  affected_forecast_item_count: number;
  budget_actuals_shifted: boolean;
}

interface PendingDrag {
  subcategoryId: number;
  subcategoryName: string;
  subcategoryType: Category["type"];
  sourceParentId: number;
  targetMasterId: number;
  targetMasterName: string;
}


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

  // Add Master modal (C1: create master that reuses existing
  // subcategories via batch move).
  const [showAddMasterModal, setShowAddMasterModal] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  // Refresh banner state for the AppShell-level "transaction added" event.
  // Adding a transaction can change `transaction_count` on any category, so
  // the categories list must reload when the global event fires. Mirrors
  // the same pattern used by Transactions/Accounts/Forecast Plans/Budgets.
  const [refreshError, setRefreshError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // -- C2 UI: Edit-mode + batch select (C2a + C2c) --------------------------
  // C2a: page-level Edit toggle. C2c: batch select for move/delete.
  // Selection is subcategory-only by design (C0 spec section 4.7: master
  // delete with children returns 409, so master rows do not participate in
  // batch operations). Selection clears on Edit-mode exit.
  const [editMode, setEditMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [batchMoveOpen, setBatchMoveOpen] = useState(false);
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);

  // -- C2b: drag-and-drop subcategory move ---------------------------------
  // Drag a subcategory onto a same-type master to move it. Two-step:
  // (1) preview via GET .../move/preview, (2) confirm via PATCH .../move.
  // Edit mode is the gate; outside Edit mode rows render the same as today.
  const [activeDragSub, setActiveDragSub] = useState<{
    id: number;
    name: string;
    type: Category["type"];
    parentId: number;
  } | null>(null);
  const [pendingDrag, setPendingDrag] = useState<PendingDrag | null>(null);
  const [pendingPreview, setPendingPreview] = useState<CategoryMoveResult | null>(null);
  const [pendingPreviewLoading, setPendingPreviewLoading] = useState(false);
  const [pendingPreviewError, setPendingPreviewError] = useState<string>("");
  const [pendingMoveSubmitting, setPendingMoveSubmitting] = useState(false);
  const [dragMoveError, setDragMoveError] = useState<string>("");

  const sensors = useSensors(
    // 6px activation distance keeps clicks on the grip from registering
    // as drags accidentally. Pointer covers mouse + pen; the explicit
    // TouchSensor with a small delay keeps the row scrollable on mobile
    // while still allowing drag from the handle.
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 150, tolerance: 8 } }),
    useSensor(KeyboardSensor),
  );

  const clearPendingDrag = useCallback(() => {
    setPendingDrag(null);
    setPendingPreview(null);
    setPendingPreviewError("");
    setPendingPreviewLoading(false);
    setPendingMoveSubmitting(false);
  }, []);

  const exitEditMode = useCallback(() => {
    setEditMode(false);
    setSelectedIds([]);
    setBatchMoveOpen(false);
    setBatchDeleteOpen(false);
    setActiveDragSub(null);
    clearPendingDrag();
    setDragMoveError("");
  }, [clearPendingDrag]);

  const toggleSelected = useCallback((id: number) => {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  }, []);

  // Esc exits Edit mode (only when no modal is open; modals own their own Esc).
  useEffect(() => {
    if (!editMode) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (batchMoveOpen || batchDeleteOpen || confirmDeleteId !== null) return;
      if (editingCatId !== null || addingToMaster !== null) return;
      if (pendingDrag !== null) return;
      exitEditMode();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [editMode, batchMoveOpen, batchDeleteOpen, confirmDeleteId, editingCatId, addingToMaster, pendingDrag, exitEditMode]);
  // -- /C2 UI ---------------------------------------------------------------

  const reload = useCallback(async () => {
    const data = await apiFetch<Category[]>("/api/v1/categories");
    setCategories(data ?? []);
    setFetching(false);
  }, []);

  useEffect(() => {
    if (!loading && user) reload().catch(() => setFetching(false));
  }, [loading, user, reload]);

  const refreshAfterTransactionAdded = useCallback(async () => {
    if (loading || !user) return;
    setRefreshing(true);
    try {
      await reload();
      setRefreshError(false);
    } catch {
      setRefreshError(true);
    } finally {
      setRefreshing(false);
    }
  }, [loading, user, reload]);

  useTransactionAddedListener(() => {
    void refreshAfterTransactionAdded();
  });

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

  // -- C2b drag handlers ----------------------------------------------------
  const handleDragStart = useCallback((event: DragStartEvent) => {
    const data = event.active.data.current as
      | { kind?: string; subcategoryId?: number; subcategoryName?: string; subcategoryType?: Category["type"]; parentId?: number }
      | undefined;
    if (!data || data.kind !== "subcategory") return;
    if (data.subcategoryId === undefined || data.subcategoryName === undefined
        || data.subcategoryType === undefined || data.parentId === undefined) return;
    setDragMoveError("");
    setActiveDragSub({
      id: data.subcategoryId,
      name: data.subcategoryName,
      type: data.subcategoryType,
      parentId: data.parentId,
    });
  }, []);

  const handleDragCancel = useCallback(() => {
    setActiveDragSub(null);
  }, []);

  const handleDragEnd = useCallback((event: DragEndEvent) => {
    setActiveDragSub(null);
    if (!editMode) return;

    const classification = classifyDrop(
      event.active.data.current,
      event.over?.data.current,
    );
    if (classification.kind !== "valid") return;

    const { sub, target } = classification;
    const targetMaster = categories.find((c) => c.id === target.masterId);
    setPendingDrag({
      subcategoryId: sub.subcategoryId,
      subcategoryName: sub.subcategoryName,
      subcategoryType: sub.subcategoryType,
      sourceParentId: sub.parentId,
      targetMasterId: target.masterId,
      targetMasterName: targetMaster?.name ?? "target master",
    });
  }, [editMode, categories]);

  // Preview fetch fires once `pendingDrag` is set.
  useEffect(() => {
    if (pendingDrag === null) return;
    let cancelled = false;
    setPendingPreviewLoading(true);
    setPendingPreview(null);
    setPendingPreviewError("");
    (async () => {
      try {
        const result = await apiFetch<CategoryMoveResult>(
          `/api/v1/categories/${pendingDrag.subcategoryId}/move/preview?target_parent_id=${pendingDrag.targetMasterId}`,
        );
        if (cancelled) return;
        setPendingPreview(result);
      } catch (err) {
        if (cancelled) return;
        setPendingPreviewError(buildMoveErrorMessage(err, pendingDrag.subcategoryName, pendingDrag.targetMasterName));
      } finally {
        if (!cancelled) setPendingPreviewLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [pendingDrag]);

  async function handleConfirmDragMove() {
    if (pendingDrag === null) return;
    setPendingMoveSubmitting(true);
    setDragMoveError("");
    try {
      await apiFetch<CategoryMoveResult>(
        `/api/v1/categories/${pendingDrag.subcategoryId}/move`,
        {
          method: "PATCH",
          body: JSON.stringify({ target_parent_id: pendingDrag.targetMasterId }),
        },
      );
      // Reload first; if it throws, surface the error inline and keep the
      // pending-move state so the user can dismiss. The PATCH already
      // succeeded server-side. This mirrors the reload-before-close
      // pattern used by BatchMoveModal.
      await reload();
      clearPendingDrag();
    } catch (err) {
      setDragMoveError(buildMoveErrorMessage(err, pendingDrag.subcategoryName, pendingDrag.targetMasterName));
      setPendingMoveSubmitting(false);
    }
  }

  function handleCancelDragMove() {
    clearPendingDrag();
    setDragMoveError("");
  }
  // -- /C2b drag handlers --------------------------------------------------

  return (
    <AppShell>
      <div className="mb-8 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-1">
          <h1 className={`${pageTitle} mb-0`}>Categories</h1>
          <HelpAnchor section="categories" label="Categories" />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* C2a: Edit-mode toggle. Owned by Team Categories C2 UI. */}
          <span className="inline-flex items-center gap-1">
            <button
              type="button"
              data-testid="categories-edit-toggle"
              aria-pressed={editMode}
              onClick={() => (editMode ? exitEditMode() : setEditMode(true))}
              className={`${btnSecondary} min-h-[44px] sm:min-h-0`}
            >
              {editMode ? "Cancel Edit" : "Edit"}
            </button>
            {!editMode && (
              <Tooltip
                content="Edit mode unlocks drag and drop reordering, moving subcategories between masters, and batch select for delete or move."
                learnMoreSection="categories"
                triggerLabel="What does Edit mode unlock?"
              />
            )}
          </span>
          <span className="inline-flex items-center gap-1">
            <button
              onClick={() => setShowAddMasterModal(true)}
              className={btnPrimary}
            >
              + Add Master
            </button>
            <Tooltip
              content="Master categories anchor budgets. Subcategories nest under a master and act as tags on individual transactions."
              learnMoreSection="categories"
              triggerLabel="What is a Master category?"
            />
          </span>
        </div>
      </div>

      {error && <div className={`mb-6 ${errorCls}`}>{error}</div>}

      {refreshError && (
        <div
          className={`mb-6 flex items-center justify-between gap-3 ${errorCls}`}
          role="status"
          data-testid="categories-refresh-error"
        >
          <span>Failed to refresh after the last update. Try again.</span>
          <button
            type="button"
            onClick={() => {
              setRefreshError(false);
              void refreshAfterTransactionAdded();
            }}
            disabled={refreshing}
            className="rounded-md border border-danger/40 px-3 py-1 text-xs font-medium text-danger hover:bg-danger/10 disabled:opacity-50"
          >
            {refreshing ? "Retrying..." : "Retry"}
          </button>
        </div>
      )}

      {showAddMasterModal && (
        <AddMasterWithSubsModal
          categories={categories}
          onCreated={async () => {
            // Reload first; only close the modal if the reload
            // succeeded so the modal can surface a retry-refresh
            // affordance on failure.
            await reload();
            setShowAddMasterModal(false);
          }}
          onCancel={() => setShowAddMasterModal(false)}
        />
      )}

      {!fetching && allMasters.length > 6 && (
        <div className="mb-4">
          <label htmlFor="cat-search" className="sr-only">Search categories</label>
          <input id="cat-search" type="text" placeholder="Search categories..." value={search} onChange={(e) => setSearch(e.target.value)} className={`max-w-sm ${input}`} />
        </div>
      )}

      {editMode && (
        <p
          id="categories-drag-instructions"
          data-testid="categories-drag-instructions"
          className="mb-3 text-xs text-text-muted"
        >
          Drag a subcategory by its handle onto a same-type master to move it. Batch Move is still available via the selection checkboxes.
        </p>
      )}

      {fetching ? (
        <Spinner />
      ) : (
        <DndContext
          sensors={sensors}
          onDragStart={handleDragStart}
          onDragEnd={handleDragEnd}
          onDragCancel={handleDragCancel}
        >
        <div className="space-y-4">
          {masters.map((master) => {
            const subs = childrenOf(master.id);
            const Icon = (master.slug && CATEGORY_ICONS[master.slug]) || Tag;
            return (
              <MasterDropZone
                key={master.id}
                masterId={master.id}
                masterType={master.type}
                enabled={editMode}
              >
              <div className={card}>
                <div data-testid={`master-row-${master.id}`} className={`flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between ${cardHeader}`}>
                  <div className="flex min-w-0 w-full items-center gap-2.5 sm:w-auto sm:flex-1">
                    <Icon className="h-4 w-4 flex-shrink-0 text-text-muted" />
                    {editingCatId === master.id ? (
                      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
                        <input type="text" value={editCatName} onChange={(e) => setEditCatName(e.target.value)} className={`min-w-0 flex-1 text-sm ${input}`} autoFocus
                          onKeyDown={(e) => { if (e.key === "Enter") handleEditCat(master.id); if (e.key === "Escape") setEditingCatId(null); }} />
                        <button onClick={() => handleEditCat(master.id)} className="inline-flex min-h-[44px] items-center px-1 text-xs text-accent md:min-h-0">Save</button>
                        <button onClick={() => setEditingCatId(null)} className="inline-flex min-h-[44px] items-center px-1 text-xs text-text-muted md:min-h-0">Cancel</button>
                      </div>
                    ) : (
                      <>
                        <h2 className="min-w-0 flex-1 truncate text-sm font-medium text-text-primary">{master.name}</h2>
                        <span className={`flex-shrink-0 text-[11px] font-medium ${TYPE_COLORS[master.type]}`}>{master.type}</span>
                        {master.is_system && <span className="flex-shrink-0 rounded bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium text-text-muted">system</span>}
                      </>
                    )}
                  </div>
                  <div data-testid={`master-actions-${master.id}`} className="flex flex-wrap items-center gap-1 sm:gap-2 md:gap-3">
                    <button
                      onClick={() => { setAddingToMaster(addingToMaster === master.id ? null : master.id); setNewSubName(""); setNewSubDesc(""); }}
                      className="inline-flex min-h-[44px] items-center px-2 text-xs text-accent hover:text-accent-hover md:min-h-0 md:px-0"
                    >
                      {addingToMaster === master.id ? "Cancel" : "+ Add Sub"}
                    </button>
                    <button onClick={() => { setEditingCatId(master.id); setEditCatName(master.name); }} className="inline-flex min-h-[44px] items-center px-2 text-xs text-text-muted hover:text-accent md:min-h-0 md:px-0">Edit</button>
                    <button onClick={() => setConfirmDeleteId(master.id)} aria-label={`Delete ${master.name}`} className="inline-flex min-h-[44px] items-center px-2 text-xs text-text-muted hover:text-danger md:min-h-0 md:px-0">Delete</button>
                  </div>
                </div>

                {master.description && (
                  <p className="px-4 pt-2 text-xs text-text-muted md:px-6">{master.description}</p>
                )}

                <div className="px-4 py-3 md:px-6">
                  {addingToMaster === master.id && (
                    <form onSubmit={handleAddSub} className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-2">
                      <div className="flex-1 w-full">
                        <label htmlFor={`sub-name-${master.id}`} className="sr-only">Subcategory name</label>
                        <input id={`sub-name-${master.id}`} type="text" required placeholder="Subcategory name" value={newSubName} onChange={(e) => setNewSubName(e.target.value)} className={input} autoFocus />
                      </div>
                      <div className="flex-1 w-full">
                        <label htmlFor={`sub-desc-${master.id}`} className="sr-only">Description</label>
                        <input id={`sub-desc-${master.id}`} type="text" placeholder="Hint / description" value={newSubDesc} onChange={(e) => setNewSubDesc(e.target.value)} className={input} />
                      </div>
                      <button type="submit" className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>Add</button>
                    </form>
                  )}

                  {subs.length > 0 ? (
                    <div className="space-y-0.5">
                      {subs.map((sub) => (
                        <DraggableSubcategoryRow
                          key={sub.id}
                          subcategoryId={sub.id}
                          subcategoryName={sub.name}
                          subcategoryType={sub.type}
                          parentId={master.id}
                          enabled={editMode && editingCatId !== sub.id}
                        >
                        <div data-testid={`sub-row-${sub.id}`} className="flex flex-wrap items-center justify-between gap-2 rounded-md px-3 py-2 transition-colors hover:bg-surface-raised">
                          {editMode && editingCatId !== sub.id && (
                            <input
                              type="checkbox"
                              data-testid={`sub-checkbox-${sub.id}`}
                              aria-label={`Select ${sub.name}`}
                              checked={selectedIds.includes(sub.id)}
                              onChange={() => toggleSelected(sub.id)}
                              className="mr-2 h-4 w-4 flex-shrink-0 cursor-pointer"
                            />
                          )}
                          {editingCatId === sub.id ? (
                            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
                              <input type="text" value={editCatName} onChange={(e) => setEditCatName(e.target.value)} className={`min-w-0 flex-1 text-sm ${input}`} autoFocus
                                onKeyDown={(e) => { if (e.key === "Enter") handleEditCat(sub.id); if (e.key === "Escape") setEditingCatId(null); }} />
                              <button onClick={() => handleEditCat(sub.id)} className="inline-flex min-h-[44px] items-center px-2 text-xs text-accent md:min-h-0 md:px-0">Save</button>
                              <button onClick={() => setEditingCatId(null)} className="inline-flex min-h-[44px] items-center px-2 text-xs text-text-muted md:min-h-0 md:px-0">Cancel</button>
                            </div>
                          ) : (
                            <>
                              <div className="min-w-0 flex-1 truncate">
                                <span className="text-sm text-text-primary">{sub.name}</span>
                                {sub.description && <span className="ml-2 text-xs text-text-muted">{sub.description}</span>}
                                <span className="ml-2 text-xs text-text-muted" title={`${sub.transaction_count} transaction(s)`}>{sub.transaction_count}</span>
                              </div>
                              <div data-testid={`sub-actions-${sub.id}`} className="flex flex-wrap gap-1 sm:gap-2">
                                <button onClick={() => { setEditingCatId(sub.id); setEditCatName(sub.name); }} className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center px-2 text-xs text-text-muted hover:text-accent md:min-h-0 md:min-w-0 md:px-0">Edit</button>
                                <button onClick={() => setConfirmDeleteId(sub.id)} aria-label={`Delete ${sub.name}`} className="inline-flex min-h-[44px] min-w-[44px] items-center justify-center px-2 text-xs text-text-muted hover:text-danger md:min-h-0 md:min-w-0 md:px-0">Delete</button>
                              </div>
                            </>
                          )}
                        </div>
                        </DraggableSubcategoryRow>
                      ))}
                    </div>
                  ) : (
                    <p className="text-xs text-text-muted py-1">No subcategories</p>
                  )}
                </div>
              </div>
              </MasterDropZone>
            );
          })}

          {masters.length === 0 && (
            <div className={`${card} p-8 text-center`}>
              <p className="text-sm text-text-muted">No categories yet. Register a new account to seed system categories.</p>
            </div>
          )}
        </div>
        <DragOverlay dropAnimation={null}>
          {activeDragSub ? (
            <div
              data-testid="categories-drag-overlay"
              className="pointer-events-none rounded-md border border-accent bg-surface-raised px-3 py-2 text-sm font-medium text-text-primary shadow-lg"
            >
              {activeDragSub.name}
            </div>
          ) : null}
        </DragOverlay>
        </DndContext>
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

      {/* C2c: batch select infrastructure. Owned by Team Categories C2 UI. */}
      {editMode && (
        <BatchActionBar
          count={selectedIds.length}
          onMove={() => setBatchMoveOpen(true)}
          onDelete={() => setBatchDeleteOpen(true)}
          onClear={() => setSelectedIds([])}
        />
      )}
      <BatchMoveModal
        open={batchMoveOpen}
        selectedIds={selectedIds}
        categories={categories}
        onCancel={() => setBatchMoveOpen(false)}
        onSuccess={async () => {
          // Reload FIRST so a refresh failure surfaces inside the modal's
          // batch-move-refresh-error banner (the modal awaits this promise
          // and re-throws). Closing the modal before awaiting reload would
          // unmount the banner before it could ever render. Mirrors the
          // finishWithMaster pattern from AddMasterWithSubsModal in PR #192.
          await reload();
          setBatchMoveOpen(false);
          exitEditMode();
        }}
      />
      <BatchDeleteModal
        open={batchDeleteOpen}
        selectedIds={selectedIds}
        categories={categories}
        onCancel={() => {
          setBatchDeleteOpen(false);
          // Reload so any partial successes show through, even on cancel.
          void reload();
        }}
        onSuccess={async (failures) => {
          // Reload FIRST. If reload throws, the modal stays open and shows
          // its batch-delete-refresh-error banner. State changes that
          // depend on a successful reload (closing the modal, exiting edit
          // mode, dropping succeeded ids) only happen after reload resolves.
          await reload();
          if (failures.length === 0) {
            setBatchDeleteOpen(false);
            exitEditMode();
          } else {
            // Drop succeeded ids from selection so user can retry only failures.
            const failedIds = new Set(failures.map((f) => f.category_id));
            setSelectedIds((prev) => prev.filter((id) => failedIds.has(id)));
          }
        }}
      />

      <DragMoveConfirmModal
        open={pendingDrag !== null}
        subcategoryName={pendingDrag?.subcategoryName ?? ""}
        targetMasterName={pendingDrag?.targetMasterName ?? ""}
        preview={pendingPreview}
        previewLoading={pendingPreviewLoading}
        previewError={pendingPreviewError}
        moveError={dragMoveError}
        submitting={pendingMoveSubmitting}
        onConfirm={() => void handleConfirmDragMove()}
        onCancel={handleCancelDragMove}
      />
    </AppShell>
  );
}
