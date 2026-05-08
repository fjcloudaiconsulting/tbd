"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import {
  btnPrimary,
  btnSecondary,
  error as errorCls,
  input,
  label,
} from "@/lib/styles";
import { formatAmount } from "@/lib/format";
import type { Account } from "@/lib/types";

interface Props {
  account: Account;
  onClose: () => void;
  onAdjusted: () => void;
}

interface AdjustResponse {
  account_id: number;
  old_balance: number;
  new_balance: number;
  delta: number;
  transaction_id: number;
}

/**
 * Track E: modal that lets an org admin set an account's balance to an
 * absolute target. The server generates a synthetic transaction equal
 * to the delta (income for positive, expense for negative) so the
 * audit trail stays honest.
 *
 * Two-step UX: the admin types the target; the modal computes the
 * delta in-form and the Submit button reads "Apply adjustment of
 * <delta>". Same flow handles positive, negative, and zero deltas
 * (the zero case surfaces the server's 409 inline rather than
 * disabling the button, so the user sees why).
 */
export default function AdjustBalanceModal({ account, onClose, onAdjusted }: Props) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  const [target, setTarget] = useState<string>(String(account.balance));
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");

  // Lock body scroll + manage focus, mirroring ConfirmModal's pattern.
  useEffect(() => {
    previousFocusRef.current = document.activeElement as HTMLElement;
    inputRef.current?.focus();
    inputRef.current?.select();
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
      previousFocusRef.current?.focus();
    };
  }, []);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) onClose();
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose, submitting]);

  const parsedTarget = useMemo(() => {
    const n = Number(target);
    return Number.isFinite(n) ? n : null;
  }, [target]);

  const delta = useMemo(() => {
    if (parsedTarget === null) return null;
    return parsedTarget - Number(account.balance);
  }, [parsedTarget, account.balance]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (parsedTarget === null) {
      setErrorMsg("Enter a valid number");
      return;
    }
    setErrorMsg("");
    setSubmitting(true);
    try {
      await apiFetch<AdjustResponse>(
        `/api/v1/accounts/${account.id}/adjust-balance`,
        {
          method: "POST",
          body: JSON.stringify({
            target_balance: parsedTarget,
            reason: reason.trim() || null,
          }),
        }
      );
      onAdjusted();
    } catch (err) {
      setErrorMsg(extractErrorMessage(err, "Failed to adjust balance"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={() => { if (!submitting) onClose(); }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="adjust-balance-title"
        className="w-full max-w-[min(32rem,calc(100vw-2rem))] max-h-[90vh] overflow-y-auto rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="adjust-balance-title" className="text-lg font-semibold text-text-primary">
          Adjust balance: {account.name}
        </h3>
        <p className="mt-2 text-sm text-text-secondary">
          A real transaction will be recorded for the difference between
          the current balance and the target. The transaction is marked as
          a manual adjustment and cannot be edited or deleted later.
        </p>

        <form onSubmit={handleSubmit} className="mt-5 space-y-4">
          <div>
            <p className="text-sm text-text-muted">Current balance</p>
            <p className="text-base tabular-nums text-text-primary">
              {formatAmount(account.balance)} {account.currency}
            </p>
          </div>

          <div>
            <label className={label} htmlFor="adjust-balance-target">
              Target balance
            </label>
            <input
              id="adjust-balance-target"
              ref={inputRef}
              type="number"
              step="0.01"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              className={input}
              disabled={submitting}
              required
              aria-describedby="adjust-balance-delta"
            />
          </div>

          <div id="adjust-balance-delta" className="text-sm">
            {delta !== null && (
              <p className="text-text-secondary">
                Delta:{" "}
                <span
                  className={
                    delta > 0
                      ? "text-success tabular-nums"
                      : delta < 0
                      ? "text-danger tabular-nums"
                      : "text-text-muted tabular-nums"
                  }
                >
                  {delta > 0 ? "+" : ""}
                  {formatAmount(delta)} {account.currency}
                </span>
              </p>
            )}
          </div>

          <div>
            <label className={label} htmlFor="adjust-balance-reason">
              Reason (optional)
            </label>
            <textarea
              id="adjust-balance-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={200}
              rows={2}
              className={`${input} resize-none`}
              disabled={submitting}
              placeholder="e.g. Reconciled to bank statement"
            />
          </div>

          {errorMsg && (
            <p className={errorCls} role="alert">
              {errorMsg}
            </p>
          )}

          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className={`${btnSecondary} w-full sm:w-auto min-h-[44px]`}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || parsedTarget === null}
              className={`${btnPrimary} w-full sm:w-auto min-h-[44px]`}
            >
              {submitting ? (
                <span className="inline-flex items-center gap-2">
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                  Applying...
                </span>
              ) : (
                "Apply adjustment"
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
