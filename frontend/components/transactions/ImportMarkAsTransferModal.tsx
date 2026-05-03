"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { btnPrimary, btnSecondary, card, error as errorCls, input, label } from "@/lib/styles";
import type { Account } from "@/lib/types";

interface Props {
  rowNumber: number;
  rowDescription: string;
  rowAmount: number;
  rowDate: string;
  rowType: "income" | "expense";
  importAccountId: number;
  importAccountName: string;
  importAccountCurrency: string;
  accounts: Account[];
  initialDestAccountId: number | null;
  onConfirm: (destAccountId: number) => void;
  onCancel: () => void;
}

/**
 * Modal shown on the /import preview when a user clicks "Mark as transfer..."
 * on a row that has no detector hit. Stores the user's destination-account
 * choice in parent state; the actual create_transfer_pair payload is built at
 * confirm time. Does not call the backend.
 */
export default function ImportMarkAsTransferModal({
  rowNumber,
  rowDescription,
  rowAmount,
  rowDate,
  rowType,
  importAccountId,
  importAccountName,
  importAccountCurrency,
  accounts,
  initialDestAccountId,
  onConfirm,
  onCancel,
}: Props) {
  const [destAccountId, setDestAccountId] = useState<number | "">(
    initialDestAccountId ?? "",
  );
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  const eligibleAccounts = useMemo(
    () =>
      accounts.filter(
        (a) =>
          a.id !== importAccountId &&
          a.currency === importAccountCurrency &&
          a.is_active,
      ),
    [accounts, importAccountId, importAccountCurrency],
  );

  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement;
    cancelRef.current?.focus();
    return () => {
      previousFocusRef.current?.focus();
    };
  }, []);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
        return;
      }
      if (e.key === "Tab") {
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
  }, [onCancel]);

  const selectedAccount = useMemo(
    () =>
      destAccountId === ""
        ? null
        : eligibleAccounts.find((a) => a.id === Number(destAccountId)) ?? null,
    [destAccountId, eligibleAccounts],
  );

  const directionPreview = selectedAccount
    ? rowType === "expense"
      ? `${importAccountName} → ${selectedAccount.name}`
      : `${selectedAccount.name} → ${importAccountName}`
    : null;

  function handleSubmit() {
    if (destAccountId === "") {
      setErrorMsg("Pick an account.");
      return;
    }
    onConfirm(Number(destAccountId));
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="import-mark-transfer-title"
        className={`${card} w-full max-w-md p-6 shadow-xl`}
        data-testid={`import-mark-transfer-modal-${rowNumber}`}
      >
        <h2
          id="import-mark-transfer-title"
          className="mb-4 text-lg font-semibold text-text-primary"
        >
          Mark as transfer
        </h2>

        <div className="mb-4 space-y-1 text-sm text-text-primary">
          <div>
            <span className="font-medium">Date:</span> {rowDate}
          </div>
          <div>
            <span className="font-medium">Amount:</span>{" "}
            <span className={rowType === "income" ? "text-success" : "text-danger"}>
              {rowType === "income" ? "+" : "-"}
              {Number(rowAmount).toFixed(2)} {importAccountCurrency}
            </span>
          </div>
          <div className="truncate">
            <span className="font-medium">Description:</span> {rowDescription}
          </div>
        </div>

        <div className="mb-4">
          <label className={label} htmlFor={`import-mark-transfer-dest-${rowNumber}`}>
            Other account
          </label>
          <select
            id={`import-mark-transfer-dest-${rowNumber}`}
            value={destAccountId}
            onChange={(e) =>
              setDestAccountId(e.target.value === "" ? "" : Number(e.target.value))
            }
            className={input}
            data-testid={`import-mark-transfer-dest-select-${rowNumber}`}
          >
            <option value="">Select account...</option>
            {eligibleAccounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} ({a.currency})
              </option>
            ))}
          </select>
          {eligibleAccounts.length === 0 && (
            <p className="mt-2 text-xs text-text-muted">
              No other same-currency account available.
            </p>
          )}
          {directionPreview && (
            <p
              className="mt-2 text-xs text-text-muted"
              data-testid={`import-mark-transfer-direction-${rowNumber}`}
            >
              Direction: {directionPreview}
            </p>
          )}
        </div>

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
            className={btnSecondary}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={destAccountId === ""}
            className={btnPrimary}
            data-testid={`import-mark-transfer-confirm-${rowNumber}`}
          >
            Mark as transfer
          </button>
        </div>
      </div>
    </div>
  );
}
