"use client";

// Confirm dialog for both single-token revoke and the revoke-all panic button
// (spec §9). Its own testids keep the page tests stable; visually it mirrors
// the shared ConfirmModal (danger variant), reusing the same design tokens.

import { useEffect, useRef } from "react";

import { btnDangerSolid, btnSecondary } from "@/lib/styles";

interface Props {
  open: boolean;
  title: string;
  message: string;
  confirmLabel: string;
  submitting?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function RevokeConfirm({
  open,
  title,
  message,
  confirmLabel,
  submitting = false,
  onConfirm,
  onCancel,
}: Props) {
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) confirmRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={onCancel}
      data-testid="revoke-confirm"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="revoke-confirm-title"
        className="w-full max-w-[min(28rem,calc(100vw-2rem))] rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3
          id="revoke-confirm-title"
          className="text-lg font-semibold text-text-primary"
        >
          {title}
        </h3>
        <p className="mt-2 whitespace-pre-line text-sm text-text-secondary">
          {message}
        </p>
        <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onCancel}
            className={`${btnSecondary} w-full sm:w-auto min-h-[44px]`}
            data-testid="revoke-confirm-cancel"
          >
            Cancel
          </button>
          <button
            ref={confirmRef}
            type="button"
            onClick={onConfirm}
            disabled={submitting}
            className={`${btnDangerSolid} w-full sm:w-auto min-h-[44px]`}
            data-testid="revoke-confirm-submit"
          >
            {submitting ? "Revoking…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
