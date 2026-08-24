"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { apiFetch, extractErrorMessage } from "@/lib/api";
import { useFocusTrap } from "@/lib/hooks/use-focus-trap";
import {
  EMAIL_RECOVERY_NOTICE,
  EMAIL_RECOVERY_REASON_MAX,
  EMAIL_RECOVERY_REASON_MIN,
  EMAIL_RECOVERY_SSO_NOTICE,
  normalizeEmail,
} from "@/lib/email-recovery";
import {
  btnPrimary,
  btnSecondary,
  card,
  error as errorCls,
  input,
  label,
} from "@/lib/styles";

// Operator email recovery (TBD-362).
//
// There is no shared field-modal component in this repo: `ConfirmModal`
// takes `message: string` only -- no children, no field slots -- and
// `ChangePlanModal`, `FeatureOverrideEditModal` and `BatchDeleteModal` each
// reimplement the same recipe independently. This follows that recipe
// (`<form role="dialog">` + `useFocusTrap` + the `lib/styles.ts` primitives)
// rather than inventing a fourth variant of the chrome.
//
// ⚠ Accessibility here is mandated, not left to the implementer: focus trap,
// initial focus on the first editable field, Escape to close, focus restored
// to the trigger. Fenced by F15.

interface Props {
  userId: number;
  /** The address currently on `users.email` -- the typo we are correcting. */
  currentEmail: string;
  emailVerified: boolean;
  /**
   * Prefill for the new-address field. Set to the live `pending_email` when
   * the operator picked "Resend confirmation link", so a resend is the same
   * POST with the same destination and a fresh, freshly-justified reason.
   */
  initialEmail?: string;
  onClose: () => void;
  onChanged: () => void;
}

export default function AdminEmailChangeModal({
  userId,
  currentEmail,
  emailVerified,
  initialEmail = "",
  onClose,
  onChanged,
}: Props) {
  const [newEmail, setNewEmail] = useState(initialEmail);
  const [confirmEmail, setConfirmEmail] = useState(initialEmail);
  const [reason, setReason] = useState("");
  const [errorMsg, setErrorMsg] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const dialogRef = useRef<HTMLFormElement>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);

  useFocusTrap({
    active: true,
    containerRef: dialogRef,
    initialFocusRef: firstFieldRef,
  });

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose]);

  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, []);

  // Client-side mirrors of the handler's own refusals, so the operator is
  // not walked through a form that earns a guaranteed 400/409. The server
  // stays authoritative. `email_already_in_use` is deliberately NOT mirrored
  // -- it needs a cross-row lookup the browser cannot do, and it is the one
  // refusal that arrives as an inline error instead.
  const nextEmail = normalizeEmail(newEmail);
  const nextConfirm = normalizeEmail(confirmEmail);
  const trimmedReason = reason.trim();

  const blockedReason = useMemo(() => {
    if (nextEmail === "") return "Enter the corrected email address.";
    if (nextEmail !== nextConfirm)
      return "The two addresses must match. Type the second one rather than pasting it.";
    if (nextEmail === normalizeEmail(currentEmail))
      return "That is already the address on this account.";
    if (trimmedReason.length < EMAIL_RECOVERY_REASON_MIN)
      return `Give a reason of at least ${EMAIL_RECOVERY_REASON_MIN} characters. It is recorded in the audit log.`;
    return null;
  }, [nextEmail, nextConfirm, currentEmail, trimmedReason]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (blockedReason !== null || submitting) return;
    setSubmitting(true);
    setErrorMsg("");
    try {
      await apiFetch(`/api/v1/admin/users/${userId}/email-change`, {
        method: "POST",
        body: JSON.stringify({
          new_email: nextEmail,
          new_email_confirm: nextConfirm,
          reason: trimmedReason,
        }),
      });
      onChanged();
      onClose();
    } catch (err) {
      // Inline, and the modal STAYS OPEN. Every failure here is correctable
      // in place (a mistyped address, a short reason, a 409 the operator can
      // act on), unlike the delete on this page whose failure ends the
      // interaction and therefore closes its modal first.
      setErrorMsg(extractErrorMessage(err, "Could not change the email address"));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-scrim p-4"
      onClick={onClose}
    >
      <form
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-email-change-title"
        onSubmit={handleSubmit}
        onClick={(e) => e.stopPropagation()}
        className={`${card} w-full max-w-[min(32rem,calc(100vw-2rem))] max-h-[90vh] overflow-y-auto p-6`}
      >
        <h2
          id="admin-email-change-title"
          className="font-display text-lg text-text-primary"
        >
          Change email address
        </h2>

        <p className="mt-2 text-sm text-text-secondary">
          {EMAIL_RECOVERY_NOTICE}
        </p>
        <p className="mt-2 text-sm text-text-secondary">
          {EMAIL_RECOVERY_SSO_NOTICE}
        </p>

        {/* Read-only context. Not focusable: it is what the operator is
            acting ON, not something they can change here. */}
        <dl className="mt-4 rounded-md border border-border-subtle bg-surface-raised px-4 py-3 text-sm">
          <div className="flex justify-between gap-4 py-1">
            <dt className="text-text-muted">Current address</dt>
            <dd className="text-text-primary">{currentEmail}</dd>
          </div>
          <div className="flex justify-between gap-4 py-1">
            <dt className="text-text-muted">Email verified</dt>
            <dd className={emailVerified ? "text-success" : "text-text-muted"}>
              {emailVerified ? "Yes" : "No"}
            </dd>
          </div>
        </dl>

        {errorMsg && (
          <div className={`${errorCls} mt-4`} role="alert">
            {errorMsg}
          </div>
        )}

        <div className="mt-4">
          <label htmlFor="admin-email-change-new" className={label}>
            New email address
          </label>
          <input
            id="admin-email-change-new"
            ref={firstFieldRef}
            type="email"
            autoComplete="off"
            value={newEmail}
            onChange={(e) => setNewEmail(e.target.value)}
            className={input}
          />
        </div>

        <div className="mt-4">
          <label htmlFor="admin-email-change-confirm" className={label}>
            Confirm new email address
          </label>
          <input
            id="admin-email-change-confirm"
            type="email"
            autoComplete="off"
            value={confirmEmail}
            onChange={(e) => setConfirmEmail(e.target.value)}
            className={input}
          />
        </div>

        <div className="mt-4">
          <label htmlFor="admin-email-change-reason" className={label}>
            Reason
          </label>
          <input
            id="admin-email-change-reason"
            type="text"
            maxLength={EMAIL_RECOVERY_REASON_MAX}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className={input}
          />
          <p className="mt-1.5 text-xs text-text-muted">
            Recorded in the audit log. The user gave no consent to this change,
            so this note is the only account of why it happened.
          </p>
        </div>

        <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className={`${btnSecondary} w-full min-h-[44px] sm:w-auto`}
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={blockedReason !== null || submitting}
            className={`${btnPrimary} w-full sm:w-auto`}
          >
            {submitting ? "Sending…" : "Send confirmation link"}
          </button>
        </div>
        {/* ⚠ A disabled control is paired with a VISIBLE reason, never a
            `title` alone: a tooltip is not reliably accessible, and this is
            the convention the danger zone on the same page already sets. */}
        {blockedReason && (
          <p
            data-testid="submit-blocked-reason"
            className="mt-2 text-right text-xs text-text-muted"
          >
            {blockedReason}
          </p>
        )}
      </form>
    </div>
  );
}
