"use client";

import { useEffect, useRef, useState } from "react";

import CategorySelect from "@/components/ui/CategorySelect";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import {
  btnPrimary,
  btnSecondary,
  card,
  error as errorCls,
  input,
  label as labelCls,
} from "@/lib/styles";
import type { Category, Transaction, UnpairTransactionRequest } from "@/lib/types";

interface Props {
  expenseLeg: Transaction;
  incomeLeg: Transaction;
  categories: Category[];
  onUnpaired: () => void;
  onCancel: () => void;
}

export default function UnpairTransferModal({
  expenseLeg,
  incomeLeg,
  categories,
  onUnpaired,
  onCancel,
}: Props) {
  const [expenseCategoryId, setExpenseCategoryId] = useState<number | "">("");
  const [incomeCategoryId, setIncomeCategoryId] = useState<number | "">("");
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);

  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  // Focus + restore
  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement;
    cancelRef.current?.focus();
    return () => {
      previousFocusRef.current?.focus();
    };
  }, []);

  // Escape + focus trap
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) {
        e.stopPropagation();
        onCancel();
        return;
      }
      if (e.key === "Tab") {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
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
  }, [submitting, onCancel]);

  const canSubmit =
    expenseCategoryId !== "" && incomeCategoryId !== "" && !submitting;

  async function handleSubmit() {
    if (expenseCategoryId === "" || incomeCategoryId === "") return;
    setSubmitting(true);
    setErrorText(null);
    try {
      const body: UnpairTransactionRequest = {
        expense_fallback_category_id: expenseCategoryId as number,
        income_fallback_category_id: incomeCategoryId as number,
      };
      // Either leg's id works for the unpair endpoint; use the expense leg.
      await apiFetch(`/api/v1/transactions/${expenseLeg.id}/unpair`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      onUnpaired();
    } catch (err) {
      setErrorText(extractErrorMessage(err, "Failed to unlink transfer"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="unpair-transfer-title"
        className={`${card} w-full max-w-md p-6 shadow-xl`}
      >
        <h2
          id="unpair-transfer-title"
          className="mb-4 text-lg font-semibold text-text-primary"
        >
          Unlink transfer
        </h2>

        <p className="mb-4 text-sm text-text-primary">
          These two rows will no longer be linked. They will return to the
          income/expense totals. Both legs need a category that fits them.
        </p>

        <div className="mb-4 space-y-1 text-sm text-text-primary">
          <div>
            <span className="font-medium">Expense leg:</span>{" "}
            -{formatAmount(expenseLeg.amount)} &middot; {expenseLeg.date}{" "}
            &middot; {expenseLeg.account_name}
          </div>
          <label htmlFor="unpair-expense-cat" className={labelCls}>
            Category
          </label>
          <CategorySelect
            id="unpair-expense-cat"
            aria-label="Expense leg category"
            categories={categories}
            typeFilter="EXPENSE"
            value={expenseCategoryId}
            onChange={setExpenseCategoryId}
            className={input}
          />
        </div>

        <div className="mb-4 space-y-1 text-sm text-text-primary">
          <div>
            <span className="font-medium">Income leg:</span>{" "}
            +{formatAmount(incomeLeg.amount)} &middot; {incomeLeg.date} &middot;{" "}
            {incomeLeg.account_name}
          </div>
          <label htmlFor="unpair-income-cat" className={labelCls}>
            Category
          </label>
          <CategorySelect
            id="unpair-income-cat"
            aria-label="Income leg category"
            categories={categories}
            typeFilter="INCOME"
            value={incomeCategoryId}
            onChange={setIncomeCategoryId}
            className={input}
          />
        </div>

        {errorText && (
          <div role="alert" className={`${errorCls} mb-4`}>
            {errorText}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className={btnSecondary}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            className={btnPrimary}
          >
            {submitting ? "Unlinking..." : "Unlink transfer"}
          </button>
        </div>
      </div>
    </div>
  );
}
