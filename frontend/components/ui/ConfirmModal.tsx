"use client";

import { useEffect, useRef } from "react";
import { btnPrimary, btnSecondary } from "@/lib/styles";

interface Props {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "default" | "warning" | "danger";
  onConfirm: () => void;
  onCancel: () => void;
}

const variantClasses: Record<string, string> = {
  default: btnPrimary,
  warning: "rounded-md bg-amber-500 px-4 py-2 text-sm font-medium text-white hover:bg-amber-600",
  danger: "rounded-md bg-danger px-4 py-2 text-sm font-medium text-white hover:bg-red-600",
};

export default function ConfirmModal({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "default",
  onConfirm,
  onCancel,
}: Props) {
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) confirmRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open, onCancel]);

  useEffect(() => {
    if (open) document.body.style.overflow = "hidden";
    else document.body.style.overflow = "";
    return () => { document.body.style.overflow = ""; };
  }, [open]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onCancel}
    >
      <div
        className="mx-4 w-full max-w-md rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold text-text-primary">{title}</h3>
        <p className="mt-2 whitespace-pre-line text-sm text-text-secondary">{message}</p>
        <div className="mt-6 flex justify-end gap-3">
          <button onClick={onCancel} className={btnSecondary}>
            {cancelLabel}
          </button>
          <button ref={confirmRef} onClick={onConfirm} className={variantClasses[variant]}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
