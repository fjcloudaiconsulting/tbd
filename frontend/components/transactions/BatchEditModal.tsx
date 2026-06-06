"use client";

import { useEffect, useRef, useState } from "react";
import CategorySelect from "@/components/ui/CategorySelect";
import TagChipInput from "@/components/transactions/TagChipInput";
import { btnPrimary, btnSecondary, input, label } from "@/lib/styles";
import type { Account, Category } from "@/lib/types";

export interface BatchEditPayload {
  category_id?: number;
  status?: "settled" | "pending";
  account_id?: number;
  tags?: string[];
}

interface Props {
  open: boolean;
  /** Number of selected transactions — surfaced in the title. */
  count: number;
  categories: Category[];
  accounts: Account[];
  submitting: boolean;
  onSubmit: (payload: BatchEditPayload) => void;
  onCancel: () => void;
}

export default function BatchEditModal({
  open,
  count,
  categories,
  accounts,
  submitting,
  onSubmit,
  onCancel,
}: Props) {
  // Empty / "" means "no change" for every field. Tags default to an empty
  // list; a non-empty list means the user wants to add those tags.
  const [categoryId, setCategoryId] = useState<number | "">("");
  const [status, setStatus] = useState<"" | "settled" | "pending">("");
  const [accountId, setAccountId] = useState<number | "">("");
  const [tags, setTags] = useState<string[]>([]);

  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  // Reset the form each time the modal opens so a previous edit doesn't leak
  // into the next batch.
  useEffect(() => {
    if (open) {
      setCategoryId("");
      setStatus("");
      setAccountId("");
      setTags([]);
    }
  }, [open]);

  // Focus management mirrors ConfirmModal: focus Cancel on open, restore the
  // previously-focused element on close.
  useEffect(() => {
    if (open) {
      previousFocusRef.current = document.activeElement as HTMLElement;
      cancelRef.current?.focus();
    } else if (previousFocusRef.current) {
      previousFocusRef.current.focus();
      previousFocusRef.current = null;
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
        return;
      }
      if (e.key === "Tab") {
        // Trap focus inside the dialog (matches ConfirmModal) so keyboard
        // users can't tab out to the page behind this multi-field form.
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        );
        if (!focusable || focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
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
  }, [open, onCancel]);

  useEffect(() => {
    if (open) document.body.style.overflow = "hidden";
    else document.body.style.overflow = "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  if (!open) return null;

  const hasChange =
    categoryId !== "" || status !== "" || accountId !== "" || tags.length > 0;

  function handleApply() {
    if (!hasChange || submitting) return;
    const payload: BatchEditPayload = {};
    if (categoryId !== "") payload.category_id = categoryId;
    if (status !== "") payload.status = status;
    if (accountId !== "") payload.account_id = accountId;
    if (tags.length > 0) payload.tags = tags;
    onSubmit(payload);
  }

  const activeAccounts = accounts.filter((a) => a.is_active);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={onCancel}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="batch-edit-modal-title"
        className="w-full max-w-[min(32rem,calc(100vw-2rem))] max-h-[90vh] overflow-y-auto rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3
          id="batch-edit-modal-title"
          className="text-lg font-semibold text-text-primary"
        >
          Batch edit {count} selected transaction{count === 1 ? "" : "s"}
        </h3>
        <p className="mt-2 text-sm text-text-secondary">
          Account and tags are not applied to transfers. A transfer&apos;s
          category must be a transfer-compatible (both) category.
        </p>

        <div className="mt-5 space-y-4">
          <div>
            <label htmlFor="batch-edit-category" className={label}>
              Set category
            </label>
            <CategorySelect
              id="batch-edit-category"
              aria-label="Category"
              categories={categories}
              value={categoryId}
              onChange={setCategoryId}
              className={input}
            />
          </div>

          <div>
            <label htmlFor="batch-edit-status" className={label}>
              Status
            </label>
            <select
              id="batch-edit-status"
              aria-label="Status"
              value={status}
              onChange={(e) =>
                setStatus(e.target.value as "" | "settled" | "pending")
              }
              className={input}
            >
              <option value="">No change</option>
              <option value="settled">Settled</option>
              <option value="pending">Pending</option>
            </select>
          </div>

          <div>
            <label htmlFor="batch-edit-account" className={label}>
              Account
            </label>
            <select
              id="batch-edit-account"
              aria-label="Account"
              value={accountId}
              onChange={(e) =>
                setAccountId(e.target.value === "" ? "" : Number(e.target.value))
              }
              className={input}
            >
              <option value="">No change</option>
              {activeAccounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label htmlFor="batch-edit-tags" className={label}>
              Add tags
            </label>
            <TagChipInput
              id="batch-edit-tags"
              ariaLabel="Add tags"
              value={tags}
              onChange={setTags}
              categoryId={categoryId}
            />
            <p className="mt-1 text-xs text-text-muted">
              Added to each transaction (existing tags kept).
            </p>
          </div>
        </div>

        <div className="mt-6 flex flex-col-reverse sm:flex-row sm:justify-end gap-2">
          <button
            type="button"
            ref={cancelRef}
            onClick={onCancel}
            className={`${btnSecondary} w-full sm:w-auto min-h-[44px]`}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleApply}
            disabled={!hasChange || submitting}
            className={`${btnPrimary} w-full sm:w-auto`}
          >
            {submitting ? "Applying…" : "Apply"}
          </button>
        </div>
      </div>
    </div>
  );
}
