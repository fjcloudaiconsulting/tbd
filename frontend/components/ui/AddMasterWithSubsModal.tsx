"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import ConfirmModal from "@/components/ui/ConfirmModal";
import { ApiResponseError, apiFetch, extractErrorMessage } from "@/lib/api";
import {
  btnPrimary,
  btnSecondary,
  card,
  error as errorCls,
  input,
  label as labelCls,
} from "@/lib/styles";
import type { Category } from "@/lib/types";

type MasterType = "income" | "expense" | "both";

interface CategoryMoveResult {
  category_id: number;
  source_master_id: number;
  target_master_id: number;
  affected_transaction_count: number;
  affected_recurring_count: number;
  affected_forecast_item_count: number;
  budget_actuals_shifted: boolean;
}

interface BatchMoveResult {
  moves: CategoryMoveResult[];
}

interface Props {
  /** All loaded categories (masters + subcategories). Used to render
   *  the "Move existing subcategories under this master" picker
   *  grouped by current master.
   */
  categories: Category[];
  /** Called when the master has been created and any selected
   *  subcategories were moved successfully. The page should refresh
   *  its category list after this fires. May be async; the modal
   *  awaits it and surfaces reload errors inline so the page never
   *  silently drops a refresh failure.
   */
  onCreated: (created: Category) => void | Promise<void>;
  onCancel: () => void;
}

/**
 * Modal for the C1 punch-list flow: create a new master category and,
 * in the same flow, move a user-picked set of existing subcategories
 * underneath it.
 *
 * Backend contract (C0 spec, sections 4.1 / 4.2 / 4.5):
 *   1. POST /api/v1/categories                                (create master).
 *   2. POST /api/v1/categories/batch-move                     (atomic
 *      multi-row move; either all selected subs move or none do).
 *
 * Order of operations when subs are selected:
 *   - Confirm dialog with generic copy ("Affected transactions and
 *     forecast items will be reassigned. Planned budgets are not
 *     changed.") because we cannot preview before the master exists.
 *   - On Yes: POST creates the master, then a single POST to
 *     /batch-move runs all selected moves atomically. On error the
 *     master remains (it was created in a separate POST that already
 *     committed) and the user can adjust selections and retry. Error
 *     messages from the response surface inline; no per-row retry/skip
 *     flow exists, the contract is all-or-nothing.
 */
export default function AddMasterWithSubsModal({
  categories,
  onCreated,
  onCancel,
}: Props) {
  const [name, setName] = useState("");
  const [type, setType] = useState<MasterType>("expense");
  const [selectedSubIds, setSelectedSubIds] = useState<Set<number>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [createdMaster, setCreatedMaster] = useState<Category | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [reloadError, setReloadError] = useState(false);
  const [mounted, setMounted] = useState(false);

  const dialogRef = useRef<HTMLDivElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!mounted) return;
    previousFocusRef.current = document.activeElement as HTMLElement;
    nameRef.current?.focus();
    nameRef.current?.select();
    return () => {
      previousFocusRef.current?.focus();
    };
  }, [mounted]);

  // Escape closes the parent modal. ConfirmModal owns its own Escape
  // and Tab handling when it is open, so we gate this trap on
  // !confirmOpen to avoid stealing keyboard input from the confirm
  // dialog (which previously trapped Tab inside the parent modal and
  // made Confirm/Cancel buttons unreachable).
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting && !confirmOpen) {
        e.stopPropagation();
        if (createdMaster) {
          // After a master was created the parent should still get the
          // master so its list refreshes; treat Esc as Done.
          void finishWithMaster(createdMaster);
          return;
        }
        onCancel();
        return;
      }
      if (e.key === "Tab" && !confirmOpen) {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (!focusable || focusable.length === 0) return;
        const visible = Array.from(focusable).filter(
          (el) => !el.hasAttribute("disabled") && el.offsetParent !== null
        );
        if (visible.length === 0) return;
        const first = visible[0];
        const last = visible[visible.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [submitting, confirmOpen, createdMaster, onCancel, onCreated]);

  // Lock body scroll while open.
  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, []);

  const trimmedName = name.trim();
  const nameValid = trimmedName.length > 0 && trimmedName.length <= 100;

  // Group existing subcategories by their current master so the picker
  // mirrors the page's structure. The list only shows subs whose
  // effective type matches the target type the user picked. Per the
  // C0 spec section 4.6 and `category_service.move_subcategory`, the
  // backend rejects any move where `sub.type != target.type` with 400
  // type_mismatch. Filter cross-type subs out of the UI to mirror the
  // backend rule exactly:
  //   - target=INCOME  -> only INCOME subs are selectable.
  //   - target=EXPENSE -> only EXPENSE subs are selectable.
  //   - target=BOTH    -> only BOTH subs are selectable.
  const groups = useMemo(() => {
    const masters = categories.filter((c) => c.parent_id === null);
    const subsByMaster = new Map<number, Category[]>();
    for (const c of categories) {
      if (c.parent_id !== null) {
        const list = subsByMaster.get(c.parent_id) ?? [];
        list.push(c);
        subsByMaster.set(c.parent_id, list);
      }
    }
    const compatible = (sub: Category): boolean => sub.type === type;
    return masters
      .map((master) => ({
        master,
        subs: (subsByMaster.get(master.id) ?? []).filter(compatible),
      }))
      .filter((g) => g.subs.length > 0);
  }, [categories, type]);

  const totalCandidates = groups.reduce((acc, g) => acc + g.subs.length, 0);

  const toggleSub = (id: number) => {
    setSelectedSubIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // Click on the form's primary button. Three flows:
  //   (a) No subs selected and no master yet: create master directly.
  //   (b) Subs selected and no master yet: open confirm dialog. Yes
  //       triggers create + atomic batch-move.
  //   (c) Master already created (post-error retry path): rerun the
  //       atomic batch-move with the still-selected subs.
  function handlePrimaryClick() {
    if (!nameValid || submitting) return;
    setErrorText(null);

    const subIds = Array.from(selectedSubIds);

    if (createdMaster) {
      // Master already created from a prior attempt; the batch-move
      // failed atomically (no partial commit). Rerun with the current
      // selection.
      if (subIds.length === 0) {
        void finishWithMaster(createdMaster);
        return;
      }
      void runBatchMove(createdMaster, subIds);
      return;
    }

    if (subIds.length === 0) {
      // Empty selection: create the master with no confirm.
      void runCreate([]);
      return;
    }

    // Subs selected: confirm before mutating anything.
    setConfirmOpen(true);
  }

  async function handleConfirmYes() {
    setConfirmOpen(false);
    const subIds = Array.from(selectedSubIds);
    await runCreate(subIds);
  }

  // Phase 1: create master. Phase 2: atomic batch-move of all selected
  // subs in a single backend call (all-or-nothing per C0 section 3.C).
  async function runCreate(subIds: number[]) {
    setSubmitting(true);
    setErrorText(null);

    let master: Category;
    try {
      master = await apiFetch<Category>("/api/v1/categories", {
        method: "POST",
        body: JSON.stringify({ name: trimmedName, type }),
      });
    } catch (err) {
      const message =
        err instanceof ApiResponseError
          ? err.message
          : extractErrorMessage(err, "Failed to create master");
      setErrorText(message);
      setSubmitting(false);
      return;
    }
    setCreatedMaster(master);

    if (subIds.length === 0) {
      setSubmitting(false);
      await finishWithMaster(master);
      return;
    }

    await runBatchMove(master, subIds);
  }

  async function runBatchMove(master: Category, subIds: number[]) {
    setSubmitting(true);
    setErrorText(null);

    try {
      await apiFetch<BatchMoveResult>("/api/v1/categories/batch-move", {
        method: "POST",
        body: JSON.stringify({
          moves: subIds.map((id) => ({
            subcategory_id: id,
            target_parent_id: master.id,
          })),
        }),
      });
    } catch (err) {
      const message =
        err instanceof ApiResponseError
          ? err.message
          : extractErrorMessage(err, "Batch move failed");
      // Atomic semantics: nothing moved. The master row remains because
      // its POST already committed in the previous step. The user can
      // adjust the selection (rename a colliding sub, unselect it,
      // etc.) and click submit again to retry against the same master.
      setErrorText(message);
      setSubmitting(false);
      return;
    }

    setSubmitting(false);
    await finishWithMaster(master);
  }

  // Final step: notify the parent. The parent reload may be async and
  // may fail (network blip during the post-mutation refresh). Surface
  // the error inline with a Retry-refresh button instead of dropping
  // the promise silently.
  async function finishWithMaster(master: Category) {
    try {
      await onCreated(master);
      setReloadError(false);
    } catch (err) {
      setReloadError(true);
      setErrorText(
        err instanceof Error
          ? `Master created but the page failed to refresh: ${err.message}`
          : "Master created but the page failed to refresh.",
      );
    }
  }

  const submitLabel = submitting
    ? "Working..."
    : createdMaster !== null
      ? "Retry move"
      : selectedSubIds.size > 0
        ? "Create master and move"
        : "Create master";

  // After the master is created, the user is in the post-error retry
  // state if a batch-move failed. They need to either fix the
  // selection and retry, or click Done.
  const canSubmit =
    nameValid &&
    !submitting &&
    (createdMaster !== null ? selectedSubIds.size > 0 : true);

  // Confirm dialog copy. Pre-mutation we have no preview yet, so use
  // generic copy per spec section 4.2.
  const confirmCount = selectedSubIds.size;
  const confirmMessage =
    `Create master "${trimmedName}" and move ${confirmCount} subcategor${confirmCount === 1 ? "y" : "ies"} under it?\n\n` +
    "Affected transactions and forecast items will be reassigned to the new master. Planned budgets are not changed.";

  if (!mounted) return null;

  const modal = (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/80 p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-master-with-subs-title"
        className={`${card} flex max-h-[90vh] w-full max-w-2xl flex-col p-6 shadow-xl`}
      >
        <h2
          id="add-master-with-subs-title"
          className="mb-4 text-lg font-semibold text-text-primary"
        >
          New master category
        </h2>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            handlePrimaryClick();
          }}
          className="flex min-h-0 flex-1 flex-col gap-4"
        >
          <div>
            <label htmlFor="add-master-name" className={labelCls}>
              Master name
            </label>
            <input
              ref={nameRef}
              id="add-master-name"
              type="text"
              required
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={input}
              autoComplete="off"
              disabled={submitting || createdMaster !== null}
            />
            {createdMaster !== null && (
              <p className="mt-1 text-xs text-text-muted">
                Master already created. Adjust the subcategory selection and
                retry, or click Done.
              </p>
            )}
          </div>

          <fieldset disabled={submitting || createdMaster !== null}>
            <legend className={labelCls}>Type</legend>
            <div className="flex gap-4 text-sm text-text-primary">
              {(["expense", "income", "both"] as const).map((t) => (
                <label key={t} className="flex items-center gap-1.5">
                  <input
                    type="radio"
                    name="add-master-type"
                    value={t}
                    checked={type === t}
                    onChange={() => {
                      setType(t);
                      // Clear selections incompatible with the new type
                      // so the user can't carry forward a now-invalid
                      // pick.
                      setSelectedSubIds(new Set());
                    }}
                  />
                  <span className="capitalize">{t}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <div className="min-h-0 flex-1 overflow-y-auto rounded-md border border-border bg-surface-raised p-3">
            <div className="mb-2 flex items-center justify-between">
              <p className={labelCls + " mb-0"}>
                Move existing subcategories under this master
              </p>
              <span
                className="text-xs text-text-muted"
                data-testid="selected-count"
              >
                {selectedSubIds.size} selected
              </span>
            </div>

            {totalCandidates === 0 ? (
              <p className="py-4 text-sm text-text-muted">
                No compatible subcategories to move. You can still create the
                master and add subcategories later.
              </p>
            ) : (
              <ul className="space-y-3" data-testid="sub-picker">
                {groups.map(({ master, subs }) => (
                  <li key={master.id} data-testid={`group-${master.id}`}>
                    <p
                      className="mb-1 text-xs font-semibold uppercase tracking-wide text-text-muted"
                      data-testid={`group-${master.id}-label`}
                    >
                      {master.name}
                    </p>
                    <ul className="space-y-1 pl-2">
                      {subs.map((sub) => (
                        <li key={sub.id}>
                          <label
                            data-testid={`sub-row-${sub.id}`}
                            className="flex items-start gap-2 rounded px-2 py-1.5 text-sm hover:bg-surface"
                          >
                            <input
                              type="checkbox"
                              checked={selectedSubIds.has(sub.id)}
                              onChange={() => toggleSub(sub.id)}
                              disabled={submitting}
                              className="mt-0.5"
                              aria-label={`Move subcategory ${sub.name} under new master`}
                            />
                            <span className="flex-1">
                              <span className="text-text-primary">
                                {sub.name}
                              </span>
                              {sub.transaction_count > 0 && (
                                <span className="ml-2 text-xs text-text-muted">
                                  {sub.transaction_count} txn
                                  {sub.transaction_count === 1 ? "" : "s"}
                                </span>
                              )}
                            </span>
                          </label>
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {errorText && (
            <div role="alert" className={errorCls}>
              {errorText}
              {reloadError && createdMaster && (
                <button
                  type="button"
                  onClick={() => void finishWithMaster(createdMaster)}
                  className="ml-3 underline"
                  data-testid="retry-refresh"
                >
                  Retry refresh
                </button>
              )}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={() => {
                if (createdMaster) {
                  void finishWithMaster(createdMaster);
                  return;
                }
                onCancel();
              }}
              disabled={submitting}
              className={btnSecondary}
            >
              {createdMaster ? "Done" : "Cancel"}
            </button>
            <button
              type="submit"
              disabled={!canSubmit}
              className={btnPrimary}
            >
              {submitLabel}
            </button>
          </div>
        </form>
      </div>

      {confirmOpen && (
        <ConfirmModal
          open={confirmOpen}
          title={`Create "${trimmedName || "master"}" and move ${selectedSubIds.size} subcategor${selectedSubIds.size === 1 ? "y" : "ies"}?`}
          message={confirmMessage}
          confirmLabel="Yes, create and move"
          cancelLabel="Cancel"
          onConfirm={handleConfirmYes}
          onCancel={() => setConfirmOpen(false)}
        />
      )}
    </div>
  );

  return createPortal(modal, document.body);
}
