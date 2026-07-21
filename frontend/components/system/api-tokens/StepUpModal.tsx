"use client";

// Step-up confirmation before a mint (spec §8/§9). Collects whichever proofs
// the operator's auth shape requires: a `current_password` for a
// password-set superadmin, and/or a fresh TOTP `mfa_code` when MFA is on.
// The backend re-verifies against the live user row and rejects a bad/missing
// proof with a generic 401 — this modal just gathers the inputs.

import { FormEvent, useEffect, useRef, useState } from "react";

import { btnPrimary, btnSecondary, input, label } from "@/lib/styles";

export interface StepUpProof {
  current_password?: string;
  mfa_code?: string;
}

interface Props {
  open: boolean;
  passwordRequired: boolean;
  mfaRequired: boolean;
  submitting: boolean;
  errorMessage: string | null;
  onSubmit: (proof: StepUpProof) => void;
  onCancel: () => void;
}

export default function StepUpModal({
  open,
  passwordRequired,
  mfaRequired,
  submitting,
  errorMessage,
  onSubmit,
  onCancel,
}: Props) {
  const [password, setPassword] = useState("");
  const [mfaCode, setMfaCode] = useState("");
  const firstFieldRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setPassword("");
      setMfaCode("");
      firstFieldRef.current?.focus();
    }
  }, [open]);

  if (!open) return null;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const proof: StepUpProof = {};
    if (passwordRequired) proof.current_password = password;
    if (mfaRequired) proof.mfa_code = mfaCode;
    onSubmit(proof);
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-bg/80 p-4"
      onClick={onCancel}
      data-testid="stepup-modal"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="stepup-title"
        className="w-full max-w-[min(28rem,calc(100vw-2rem))] max-h-[90vh] overflow-y-auto rounded-lg border border-border bg-surface p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="stepup-title" className="text-lg font-semibold text-text-primary">
          Confirm it&apos;s you
        </h3>
        <p className="mt-2 text-sm text-text-secondary">
          Minting a token is a sensitive action. Re-verify to continue.
        </p>

        <form onSubmit={handleSubmit} className="mt-4 grid grid-cols-1 gap-4">
          {passwordRequired && (
            <div>
              <label htmlFor="stepup-password-input" className={label}>
                Current password
              </label>
              <input
                ref={firstFieldRef}
                id="stepup-password-input"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={input}
                autoComplete="current-password"
                data-testid="stepup-password"
              />
            </div>
          )}

          {mfaRequired && (
            <div>
              <label htmlFor="stepup-mfa-input" className={label}>
                Authenticator code
              </label>
              <input
                ref={passwordRequired ? undefined : firstFieldRef}
                id="stepup-mfa-input"
                inputMode="numeric"
                value={mfaCode}
                onChange={(e) => setMfaCode(e.target.value)}
                className={input}
                autoComplete="one-time-code"
                data-testid="stepup-mfa"
              />
            </div>
          )}

          {!passwordRequired && !mfaRequired && (
            <p className="text-sm text-text-secondary" data-testid="stepup-sso-note">
              Your account signs in with Google. Complete the Google
              verification prompt to continue.
            </p>
          )}

          {errorMessage && (
            <p className="text-sm text-danger" data-testid="stepup-error">
              {errorMessage}
            </p>
          )}

          <div className="mt-2 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <button
              type="button"
              onClick={onCancel}
              className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
              data-testid="stepup-cancel"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}
              data-testid="stepup-submit"
            >
              {submitting ? "Verifying…" : "Verify & mint"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
