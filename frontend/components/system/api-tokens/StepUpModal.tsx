"use client";

// Step-up confirmation before a mint (spec §8/§9). Collects whichever proofs
// the operator's auth shape requires: a `current_password` for a
// password-set superadmin, and/or a fresh TOTP `mfa_code` when MFA is on.
// The backend re-verifies against the live user row and rejects a bad/missing
// proof with a generic 401 — this modal just gathers the inputs.
//
// SSO operators (`passwordRequired === false`, i.e. `password_set === false`)
// are a known v1 gap: the backend's step-up path for them requires a fresh
// `stepup_token` (spec §8) that nothing in this UI obtains, so any submit for
// that account shape is unconditionally rejected with a 401 regardless of
// MFA state. Rather than let the operator click a doomed "Verify & mint",
// this modal is honest about the gap and points them at setting a password
// instead of collecting proofs it cannot use.

import { FormEvent, useEffect, useRef, useState } from "react";
import Link from "next/link";

import { btnPrimary, btnSecondary, input, label } from "@/lib/styles";

export interface StepUpProof {
  current_password?: string;
  mfa_code?: string;
  stepup_token?: string;
}

// Opt-in Google step-up for a password-less account (TBD-581, agent tokens):
// `token` is the proof the SSO round trip brought back, `onVerify` starts it.
// Without it the modal keeps its honest "set a password" note.
export interface SsoStepUp {
  token: string | null;
  onVerify: () => void;
  busy?: boolean;
}

interface Props {
  open: boolean;
  passwordRequired: boolean;
  mfaRequired: boolean;
  submitting: boolean;
  errorMessage: string | null;
  onSubmit: (proof: StepUpProof) => void;
  onCancel: () => void;
  sso?: SsoStepUp;
}

export default function StepUpModal({
  open,
  passwordRequired,
  mfaRequired,
  submitting,
  errorMessage,
  onSubmit,
  onCancel,
  sso,
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

  // A password-less account can only prove itself with a fresh SSO token.
  const ssoReady = !passwordRequired && !!sso?.token;
  const canSubmit = passwordRequired || ssoReady;

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return; // no honest proof to send — see banner note above
    const proof: StepUpProof = {};
    if (passwordRequired) proof.current_password = password;
    else proof.stepup_token = sso!.token!;
    if (mfaRequired) proof.mfa_code = mfaCode;
    onSubmit(proof);
  }

  const mfaField = mfaRequired && (
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
  );

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
          {passwordRequired ? (
            <>
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

              {mfaField}
            </>
          ) : sso ? (
            ssoReady ? (
              <>
                <p className="text-sm text-text-secondary" data-testid="stepup-sso-verified">
                  Google confirmed it&apos;s you.
                </p>
                {mfaField}
              </>
            ) : (
              <div className="text-sm text-text-secondary">
                <p>Your account signs in with Google. Confirm with Google to continue.</p>
                <button
                  type="button"
                  onClick={sso.onVerify}
                  disabled={sso.busy}
                  className={`${btnSecondary} mt-3 min-h-[44px] sm:min-h-0`}
                  data-testid="stepup-sso-verify"
                >
                  {sso.busy ? "Opening Google…" : "Verify with Google"}
                </button>
              </div>
            )
          ) : (
            // Known v1 gap: an SSO account (no password set) has no way to
            // supply the fresh `stepup_token` the backend requires here, so
            // any mint attempt would 401 unconditionally. Say so plainly
            // instead of collecting proofs that can't work.
            <div
              className="rounded-md border border-border bg-surface-raised p-4 text-sm text-text-secondary"
              data-testid="stepup-no-password-note"
            >
              <p>
                Creating API tokens in this version requires a password on
                your account. Set one in Security settings, then come back
                to mint a token.
              </p>
              <Link
                href="/settings/security"
                className="mt-2 inline-block text-accent hover:underline"
                data-testid="stepup-set-password-link"
              >
                Go to Security settings
              </Link>
            </div>
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
              {canSubmit ? "Cancel" : "Close"}
            </button>
            {canSubmit && (
              <button
                type="submit"
                disabled={submitting}
                className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}
                data-testid="stepup-submit"
              >
                {submitting ? "Verifying…" : "Verify & mint"}
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
}
