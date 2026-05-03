"use client";

import { useEffect, useRef, useState } from "react";

import { apiFetch, extractErrorMessage } from "@/lib/api";
import { formatAmount } from "@/lib/format";
import { btnPrimary, btnSecondary, card, error as errorCls } from "@/lib/styles";
import type { Transaction, TransactionPairRequest } from "@/lib/types";

interface Props {
  expenseLeg: Transaction;
  incomeLeg: Transaction;
  onLinked: () => void;
  onCancel: () => void;
}

export default function LinkAsTransferModal({
  expenseLeg,
  incomeLeg,
  onLinked,
  onCancel,
}: Props) {
  const [recategorize, setRecategorize] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement;
    cancelRef.current?.focus();
    return () => {
      previousFocusRef.current?.focus();
    };
  }, []);

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

  async function handleSubmit() {
    setSubmitting(true);
    setErrorMsg(null);
    try {
      const body: TransactionPairRequest = {
        expense_id: expenseLeg.id,
        income_id: incomeLeg.id,
        recategorize,
      };
      await apiFetch("/api/v1/transactions/pair", {
        method: "POST",
        body: JSON.stringify(body),
      });
      onLinked();
    } catch (err) {
      setErrorMsg(extractErrorMessage(err, "Failed to link transfer"));
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
        aria-labelledby="link-transfer-title"
        className={`${card} w-full max-w-md p-6 shadow-xl`}
      >
        <h2
          id="link-transfer-title"
          className="mb-4 text-lg font-semibold text-text-primary"
        >
          Link as transfer
        </h2>
        <div className="mb-4 space-y-2 text-sm text-text-primary">
          <div>
            <span className="font-medium">Expense leg:</span>{" "}
            -{formatAmount(expenseLeg.amount)} on {expenseLeg.account_name} ({expenseLeg.date})
          </div>
          <div>
            <span className="font-medium">Income leg:</span>{" "}
            +{formatAmount(incomeLeg.amount)} on {incomeLeg.account_name} ({incomeLeg.date})
          </div>
        </div>
        <label className="mb-4 flex items-center gap-2 text-sm text-text-primary">
          <input
            type="checkbox"
            checked={recategorize}
            onChange={(e) => setRecategorize(e.target.checked)}
            className="rounded"
          />
          Use system Transfer category for both legs
        </label>
        {errorMsg && (
          <div role="alert" className={`${errorCls} mb-4`}>
            {errorMsg}
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
            disabled={submitting}
            className={btnPrimary}
          >
            {submitting ? "Linking..." : "Link as transfer"}
          </button>
        </div>
      </div>
    </div>
  );
}
