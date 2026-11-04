"use client";

import { useEffect, useRef, useState } from "react";
import CategorySelect from "@/components/ui/CategorySelect";
import TagChipInput from "@/components/transactions/TagChipInput";
import { btnPrimary, btnSecondary, input, label, warning } from "@/lib/styles";
import type { Account, Category } from "@/lib/types";

export interface BatchEditPayload {
  category_id?: number;
  status?: "settled" | "pending";
  account_id?: number;
  tags?: string[];
  /** TBD-763: ISO dates (YYYY-MM-DD). Both SET a date; neither shifts one. */
  date?: string;
  settled_date?: string;
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
  const [date, setDate] = useState("");
  const [settledDate, setSettledDate] = useState("");

  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  // Reset the form each time the modal opens so a previous edit doesn't leak
  // into the next batch.
  useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- resets the batch-edit form fields each time the modal opens
      setCategoryId("");
      setStatus("");
      setAccountId("");
      setTags([]);
      setDate("");
      setSettledDate("");
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
    categoryId !== "" ||
    status !== "" ||
    accountId !== "" ||
    tags.length > 0 ||
    date !== "" ||
    settledDate !== "";
  // YYYY-MM-DD compares correctly as a string. Every row taking both would
  // fail the server's ordering check, so it is caught before the request.
  const datesOutOfOrder = date !== "" && settledDate !== "" && settledDate < date;

  function handleApply() {
    if (!hasChange || datesOutOfOrder || submitting) return;
    const payload: BatchEditPayload = {};
    if (categoryId !== "") payload.category_id = categoryId;
    if (status !== "") payload.status = status;
    if (accountId !== "") payload.account_id = accountId;
    if (tags.length > 0) payload.tags = tags;
    if (date !== "") payload.date = date;
    if (settledDate !== "") payload.settled_date = settledDate;
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
            <label htmlFor="batch-edit-date" className={label}>
              Set date
            </label>
            <input
              id="batch-edit-date"
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              aria-describedby="batch-edit-date-hint"
              className={input}
            />
            <p id="batch-edit-date-hint" className="mt-1 text-xs text-text-muted">
              Not applied to recurring occurrences or transfers. A pending
              transaction keeps its gap to the expected settlement.
            </p>
            {/* CSV dedupe matches (date, amount, description): a moved
                imported row no longer matches its line in the file. */}
            {date !== "" && (
              <p className={`mt-2 ${warning}`}>
                Re-importing the same file may duplicate imported transactions
                you move to a new date.
              </p>
            )}
          </div>

          <div>
            <label htmlFor="batch-edit-settled-date" className={label}>
              Set settlement date
            </label>
            <input
              id="batch-edit-settled-date"
              type="date"
              value={settledDate}
              onChange={(e) => setSettledDate(e.target.value)}
              aria-describedby={
                datesOutOfOrder
                  ? "batch-edit-settled-date-error batch-edit-settled-date-hint"
                  : "batch-edit-settled-date-hint"
              }
              aria-invalid={datesOutOfOrder || undefined}
              className={input}
            />
            {datesOutOfOrder && (
              <p
                id="batch-edit-settled-date-error"
                role="alert"
                className="mt-1 text-xs text-danger"
              >
                Settlement date must be on or after the date
              </p>
            )}
            <p
              id="batch-edit-settled-date-hint"
              className="mt-1 text-xs text-text-muted"
            >
              Settled transactions, or ones you set to settled above, settle on
              this date. Pending ones stay pending and expect to settle on it
              (a transfer only while both legs are pending).
            </p>
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
            disabled={!hasChange || datesOutOfOrder || submitting}
            className={`${btnPrimary} w-full sm:w-auto`}
          >
            {submitting ? "Applying…" : "Apply"}
          </button>
        </div>
      </div>
    </div>
  );
}
