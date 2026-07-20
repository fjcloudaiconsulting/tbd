"use client";

// Type-the-count send confirmation (spec `specs/2026-07-20-broadcast-admin-ui-design.md`,
// Ruling R4). The final Send action stays disabled until the operator types
// the exact recipient count they were shown AND a dry-run has already gone
// out AND they've ticked the "queues a real email" acknowledgement -
// defense-in-depth against a stale tab firing a real send.

import { useEffect, useRef, useState } from "react";
import { btnPrimary, btnSecondary, input, label } from "@/lib/styles";

interface Props {
  open: boolean;
  subject: string;
  recipientCount: number;
  dryRunDone: boolean;
  sending: boolean;
  errorMessage: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function SendConfirmModal({
  open,
  subject,
  recipientCount,
  dryRunDone,
  sending,
  errorMessage,
  onConfirm,
  onCancel,
}: Props) {
  const [typedCount, setTypedCount] = useState("");
  const [agreed, setAgreed] = useState(false);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) {
      setTypedCount("");
      setAgreed(false);
      confirmRef.current?.focus();
    }
  }, [open]);

  if (!open) return null;

  const countMatches =
    typedCount.trim() !== "" && Number(typedCount) === recipientCount;
  const canConfirm = countMatches && dryRunDone && agreed && !sending;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={onCancel}
      data-testid="broadcast-send-modal"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="send-confirm-title"
        className="w-full max-w-[min(28rem,calc(100vw-2rem))] max-h-[90vh] overflow-y-auto rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="send-confirm-title" className="text-lg font-semibold text-text-primary">
          Send broadcast
        </h3>

        <div className="mt-4">
          <span className={label}>Subject</span>
          <p className="text-sm text-text-primary" data-testid="broadcast-send-confirm-subject">
            {subject}
          </p>
        </div>

        <div className="mt-4">
          <p className="text-sm text-text-secondary">
            This will queue a real email to{" "}
            <span className="font-semibold text-text-primary">{recipientCount}</span>{" "}
            recipients.
          </p>
        </div>

        <div className="mt-4">
          <label htmlFor="broadcast-send-confirm-count" className={label}>
            Type {recipientCount} to confirm
          </label>
          <input
            id="broadcast-send-confirm-count"
            className={input}
            value={typedCount}
            onChange={(e) => setTypedCount(e.target.value)}
            inputMode="numeric"
            data-testid="broadcast-send-confirm-count-input"
          />
        </div>

        <div className="mt-4 flex items-start gap-2 text-sm text-text-primary">
          <input
            id="broadcast-send-confirm-checkbox"
            type="checkbox"
            checked={agreed}
            onChange={(e) => setAgreed(e.target.checked)}
            data-testid="broadcast-send-confirm-checkbox"
          />
          <label htmlFor="broadcast-send-confirm-checkbox">
            This queues a real email to {recipientCount} customers.
          </label>
        </div>

        {!dryRunDone && (
          <p className="mt-3 text-xs text-warning">
            Send a test to yourself first - this stays locked until a dry run has gone out.
          </p>
        )}

        {errorMessage && (
          <p className="mt-3 text-sm text-danger" data-testid="broadcast-send-confirm-error">
            {errorMessage}
          </p>
        )}

        <div className="mt-6 flex flex-col-reverse sm:flex-row sm:justify-end gap-2">
          <button
            onClick={onCancel}
            className={`${btnSecondary} w-full sm:w-auto min-h-[44px]`}
            data-testid="broadcast-send-confirm-cancel"
          >
            Cancel
          </button>
          <button
            ref={confirmRef}
            onClick={onConfirm}
            disabled={!canConfirm}
            className={`${btnPrimary} w-full sm:w-auto min-h-[44px]`}
            data-testid="broadcast-send-confirm-submit"
          >
            {sending ? "Sending..." : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}
