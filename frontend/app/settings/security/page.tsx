"use client";

import { FormEvent, useEffect, useState } from "react";
import { Loader2, Copy, Check } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import SettingsLayout from "@/components/SettingsLayout";
import PasswordInput from "@/components/ui/PasswordInput";
import { useAuth, MfaRequiredError } from "@/components/auth/AuthProvider";
import SsoStepupErrorBanner from "@/components/auth/SsoStepupErrorBanner";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isAdmin } from "@/lib/auth";
import {
  mapMfaSetupError,
  mapMfaDisableError,
  mapMfaRegenerateError,
} from "@/lib/formErrors";
import {
  input,
  label,
  btnPrimary,
  btnSecondary,
  btnDanger,
  card,
  cardTitle,
  error as errorCls,
  success as successCls,
} from "@/lib/styles";
import type { MfaSetupResponse, MfaEnableResponse } from "@/lib/types";

// Mirror of the backend's `_STEPUP_RETURN_TARGETS` allowlist key.
// Initiate POST sends this so the callback knows to redirect back to
// /settings/security (here) instead of the email-change page.
const STEPUP_RETURN_TO = "security";

/**
 * Friendly copy keyed by the `?sso_stepup_error=<code>` value that
 * /api/v1/auth/sso-stepup/callback redirects back with on failure.
 * The security page is reached via `return_to: "security"`, so any
 * step-up flow started here (first-time password set) lands back here
 * on failure. Without this banner the user sees no feedback at all.
 *
 * Wording adjusted from the email-change variant on /settings to
 * reflect that step-up from this page is about verifying for a
 * password-set / change, not an email change.
 */
const SSO_STEPUP_ERROR_COPY: Record<string, string> = {
  state: "Your Google verification attempt expired. Try again to update your password.",
  token: "Google verification didn't complete. Try again.",
  userinfo: "Google verification didn't complete. Try again.",
  unverified:
    "Your Google account isn't verified, so we can't use it to confirm this change.",
  email_mismatch:
    "The Google account you signed in with doesn't match this profile. Use the same Google account.",
  cancelled:
    "You cancelled the Google verification. Try again whenever you're ready.",
  provider_error:
    "Google returned an error during verification. Try again.",
};
const SSO_STEPUP_ERROR_FALLBACK = "Google verification didn't complete. Try again.";

type MfaStep = "idle" | "qr" | "verify" | "codes";

export default function SecurityPage() {
  const { user, login, refreshMe } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();

  // ── Change Password ──────────────────────────────────────────────────────
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [pwdMsg, setPwdMsg] = useState("");
  const [pwdErr, setPwdErr] = useState("");
  const [savingPwd, setSavingPwd] = useState(false);

  // SSO users (Google) start with `password_set=false`. They cannot
  // type a current password, so the first-time set requires the same
  // Google step-up proof the email-change flow uses. The token rides
  // back from the OAuth callback in `#stepup_token=…`, is read on
  // mount, then sent to /me/password. After the first set the flag
  // flips true and this branch collapses to the standard form.
  const passwordSet = user?.password_set ?? true;
  const [stepupToken, setStepupToken] = useState("");
  const [stepupBusy, setStepupBusy] = useState(false);

  // `?sso_stepup_error=<code>` arrives via the 307 from
  // /api/v1/auth/sso-stepup/callback when the Google round-trip
  // fails AND the state slot encoded `return_to: "security"` (any
  // step-up initiated from this page). Without this banner the
  // user lands here with no visible feedback.
  const stepupErrorCode = searchParams?.get("sso_stepup_error");
  const [stepupErrorVisible, setStepupErrorVisible] = useState<boolean>(false);
  useEffect(() => {
    setStepupErrorVisible(Boolean(stepupErrorCode));
  }, [stepupErrorCode]);
  function clearStepupErrorFromUrl() {
    setStepupErrorVisible(false);
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    url.searchParams.delete("sso_stepup_error");
    router.replace(url.pathname + (url.search || "") + url.hash);
  }

  // Pull `#stepup_token=…` off the URL on mount and immediately strip
  // it from history. Fragments are never sent to the server, so this
  // is the safe channel the backend redirects to.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const hash = window.location.hash;
    if (hash.startsWith("#stepup_token=")) {
      const token = hash.slice("#stepup_token=".length);
      if (token) setStepupToken(token);
      window.history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  }, []);

  async function handleVerifyWithGoogle() {
    setPwdErr(""); setStepupBusy(true);
    try {
      const data = await apiFetch<{ redirect_url: string }>(
        "/api/v1/auth/sso-stepup/initiate",
        {
          method: "POST",
          body: JSON.stringify({ return_to: STEPUP_RETURN_TO }),
        },
      );
      // Full navigation, not router.push — Google must own the next page.
      window.location.href = data.redirect_url;
    } catch (err) {
      setPwdErr(extractErrorMessage(err));
      setStepupBusy(false);
    }
  }

  async function handlePasswordSubmit(e: FormEvent) {
    e.preventDefault();
    setPwdMsg(""); setPwdErr("");
    if (newPassword !== confirmPassword) { setPwdErr("New passwords do not match"); return; }
    if (!passwordSet && !stepupToken) {
      setPwdErr("Verify with Google before setting your password. Click 'Verify with Google' below.");
      return;
    }
    setSavingPwd(true);
    try {
      const payload: { new_password: string; current_password?: string; stepup_token?: string } = {
        new_password: newPassword,
      };
      if (passwordSet) {
        payload.current_password = currentPassword;
      } else {
        payload.stepup_token = stepupToken;
      }
      await apiFetch("/api/v1/users/me/password", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setPwdMsg(
        passwordSet
          ? "Password changed. Signing in with new credentials..."
          : "Password set. Signing in with new credentials...",
      );
      try {
        await login(user!.username, newPassword);
      } catch (loginErr) {
        // MFA users will get MfaRequiredError on re-login — password still changed
        if (loginErr instanceof MfaRequiredError) {
          // Password changed successfully, but re-login needs MFA.
          // Just show success — the existing session is still valid.
        } else {
          throw loginErr;
        }
      }
      // Refresh the user so `password_set` flips and the card
      // re-renders in standard mode after a first-time set.
      await refreshMe();
      setCurrentPassword(""); setNewPassword(""); setConfirmPassword("");
      setStepupToken("");
      setPwdMsg(
        passwordSet
          ? "Password changed successfully"
          : "Password set. You can now sign in with your email and password.",
      );
    } catch (err) {
      // The step-up token is single-use on the backend and is consumed
      // on the row before any error path that returns 4xx. Clearing
      // local state forces the user to re-verify with Google rather
      // than retry against an already-spent token. Keeps the UI honest.
      if (!passwordSet) setStepupToken("");
      setPwdErr(extractErrorMessage(err));
    }
    finally { setSavingPwd(false); }
  }

  // ── MFA Setup ────────────────────────────────────────────────────────────
  const [mfaStep, setMfaStep] = useState<MfaStep>("idle");
  const [setupData, setSetupData] = useState<MfaSetupResponse | null>(null);
  const [totpCode, setTotpCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  const [codesSaved, setCodesSaved] = useState(false);
  const [mfaMsg, setMfaMsg] = useState("");
  const [mfaErr, setMfaErr] = useState("");
  const [mfaLoading, setMfaLoading] = useState(false);

  // ── MFA Disable ──────────────────────────────────────────────────────────
  const [disablePassword, setDisablePassword] = useState("");
  const [disableErr, setDisableErr] = useState("");
  const [disabling, setDisabling] = useState(false);
  const [showDisable, setShowDisable] = useState(false);

  // ── Regenerate Codes ─────────────────────────────────────────────────────
  const [regenPassword, setRegenPassword] = useState("");
  const [regenErr, setRegenErr] = useState("");
  const [regenerating, setRegenerating] = useState(false);
  const [showRegen, setShowRegen] = useState(false);
  const [regenCodes, setRegenCodes] = useState<string[]>([]);

  // ── Copy-to-clipboard ────────────────────────────────────────────────────
  // One small bit of UI state tracks which payload was most recently
  // copied so the button label can swap to "Copied" for a beat without
  // pulling in a toast library. Cleared automatically after ~2s.
  const [copiedKey, setCopiedKey] = useState<"secret" | "codes" | "regen-codes" | null>(null);
  async function copyToClipboard(text: string, key: "secret" | "codes" | "regen-codes") {
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // No clipboard API (older browsers, insecure contexts). Fall back
        // to a hidden textarea + execCommand so the affordance still works.
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "absolute";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopiedKey(key);
      setTimeout(() => {
        setCopiedKey((current) => (current === key ? null : current));
      }, 2000);
    } catch {
      // Silently ignore copy failures (denied permission, etc.) — the
      // secret stays select-all so the user can copy manually.
    }
  }

  // ── Session Lifetime ──────────────────────────────────────────────────
  const [sessionDays, setSessionDays] = useState("30");
  const [sessionMsg, setSessionMsg] = useState("");
  const [sessionErr, setSessionErr] = useState("");
  const [savingSession, setSavingSession] = useState(false);
  const admin = user ? isAdmin(user) : false;

  useEffect(() => {
    if (!admin) return;
    apiFetch<{ key: string; value: string }[]>("/api/v1/settings")
      .then((settings) => {
        const s = settings.find((s) => s.key === "session_lifetime_days");
        if (s) setSessionDays(s.value);
      })
      .catch(() => {});
  }, [admin]);

  async function handleSessionSubmit(e: FormEvent) {
    e.preventDefault();
    setSessionMsg(""); setSessionErr(""); setSavingSession(true);
    const days = parseInt(sessionDays, 10);
    if (isNaN(days) || days < 1 || days > 365) {
      setSessionErr("Must be between 1 and 365 days"); setSavingSession(false); return;
    }
    try {
      await apiFetch("/api/v1/settings", {
        method: "PUT",
        body: JSON.stringify({ key: "session_lifetime_days", value: String(days) }),
      });
      setSessionMsg("Session lifetime updated");
    } catch (err) { setSessionErr(extractErrorMessage(err)); }
    finally { setSavingSession(false); }
  }

  async function handleSetup() {
    if (mfaLoading) return; // guard against double-submit on rapid clicks
    setMfaErr(""); setMfaLoading(true);
    try {
      const data = await apiFetch<MfaSetupResponse>("/api/v1/auth/mfa/setup", {
        method: "POST",
      });
      setSetupData(data);
      setMfaStep("qr");
    } catch (err) { setMfaErr(mapMfaSetupError(err)); }
    finally { setMfaLoading(false); }
  }

  async function handleVerifyCode(e: FormEvent) {
    e.preventDefault();
    if (mfaLoading) return;
    if (totpCode.length !== 6) {
      // Should be unreachable because the submit button is disabled, but
      // belt-and-suspenders for the keyboard-Enter path.
      setMfaErr("Code must be 6 digits.");
      return;
    }
    setMfaErr(""); setMfaLoading(true);
    try {
      const data = await apiFetch<MfaEnableResponse>("/api/v1/auth/mfa/enable", {
        method: "POST",
        body: JSON.stringify({ code: totpCode }),
      });
      setRecoveryCodes(data.recovery_codes);
      setMfaStep("codes");
      setTotpCode(""); // wipe the code now that it's been accepted
    } catch (err) {
      setMfaErr(mapMfaSetupError(err));
      // Keep the input filled so the user can correct it; the code may
      // simply be one tick stale.
    }
    finally { setMfaLoading(false); }
  }

  function handleFinishSetup() {
    setMfaStep("idle");
    setSetupData(null);
    setTotpCode("");
    setRecoveryCodes([]);
    setCodesSaved(false);
    setMfaMsg(
      "Two-factor authentication is on. You will need your authenticator app the next time you sign in.",
    );
    // refreshMe() rejects on /auth/me failure (post-2026-05-18 P2 fix).
    // The MFA-enable apiFetch already succeeded server-side, so the
    // setting is saved; we just couldn't pull the fresh user payload.
    // Surface a "saved, reload to refresh" hint instead of letting the
    // unhandled rejection bubble up.
    refreshMe().catch(() => {
      setMfaMsg(
        "Two-factor authentication is on. Reload the page to refresh your profile state.",
      );
    });
  }

  function downloadCodes(codes: string[]) {
    const text = "The Better Decision: Recovery Codes\n" +
      "===================\n\n" +
      "Store these codes in a safe place.\n" +
      "Each code can only be used once.\n\n" +
      codes.map((c, i) => `${i + 1}. ${c}`).join("\n") + "\n";
    const blob = new Blob([text], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "the-better-decision-recovery-codes.txt";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function handleDisable(e: FormEvent) {
    e.preventDefault();
    if (disabling) return;
    if (!disablePassword) {
      setDisableErr("Enter your password to confirm.");
      return;
    }
    setDisableErr(""); setDisabling(true);
    try {
      await apiFetch("/api/v1/auth/mfa/disable", {
        method: "POST",
        body: JSON.stringify({ password: disablePassword }),
      });
      setShowDisable(false);
      setDisablePassword("");
      setMfaMsg(
        "Two-factor authentication is off. Your account is now protected by your password only.",
      );
      // refreshMe() rejects on /auth/me failure (post-2026-05-18 P2 fix).
      // MFA was disabled server-side; if the profile refresh fails the
      // local user object is stale (mfa_enabled still true) until the
      // user reloads. Surface a clear hint instead of letting an
      // unhandled rejection escape.
      await refreshMe().catch(() => {
        setMfaMsg(
          "Two-factor authentication is off. Reload the page to refresh your profile state.",
        );
      });
    } catch (err) {
      // Keep the password field filled so the user can retry. Only the
      // server can decide whether the password was wrong.
      setDisableErr(mapMfaDisableError(err));
    }
    finally { setDisabling(false); }
  }

  async function handleRegenerate(e: FormEvent) {
    e.preventDefault();
    if (regenerating) return;
    if (!regenPassword) {
      setRegenErr("Enter your password to confirm.");
      return;
    }
    setRegenErr(""); setRegenerating(true);
    try {
      const data = await apiFetch<MfaEnableResponse>("/api/v1/auth/mfa/recovery-codes", {
        method: "POST",
        body: JSON.stringify({ password: regenPassword }),
      });
      setRegenCodes(data.recovery_codes);
      setRegenPassword("");
    } catch (err) {
      // Preserve the typed password on error so the user can correct
      // and resubmit without re-typing.
      setRegenErr(mapMfaRegenerateError(err));
    }
    finally { setRegenerating(false); }
  }

  const mfaEnabled = user?.mfa_enabled ?? false;

  return (
    <SettingsLayout activeTab="/settings/security">

      <div className="max-w-lg space-y-6">
        {stepupErrorVisible && stepupErrorCode && (
          <SsoStepupErrorBanner
            errorCode={stepupErrorCode}
            copyByCode={SSO_STEPUP_ERROR_COPY}
            fallbackCopy={SSO_STEPUP_ERROR_FALLBACK}
            busy={stepupBusy}
            onRetry={() => {
              clearStepupErrorFromUrl();
              handleVerifyWithGoogle();
            }}
            onDismiss={clearStepupErrorFromUrl}
          />
        )}
        {/* ── Change Password ──────────────────────────────────────────── */}
        <div className={`${card} p-6`}>
          <h2 className={`mb-5 ${cardTitle}`}>{passwordSet ? "Change Password" : "Set a Password"}</h2>
          {!passwordSet && (
            <p className="mb-4 text-sm text-text-muted">
              Your account was created with Google. Set a password to also sign in with your email address.
            </p>
          )}
          <form onSubmit={handlePasswordSubmit} className="space-y-4">
            {pwdMsg && <div className={successCls}>{pwdMsg}</div>}
            {pwdErr && <div className={errorCls}>{pwdErr}</div>}
            {passwordSet && (
              <div>
                <label htmlFor="pwd-current" className={label}>Current Password</label>
                <PasswordInput id="pwd-current" required value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} className={input} autoComplete="current-password" />
              </div>
            )}
            {!passwordSet && (
              <div className="rounded-lg border border-border p-4 space-y-3">
                <p className="text-sm text-text-primary font-medium">
                  Verify with Google to set a password
                </p>
                <p className="text-xs text-text-muted">
                  Confirm this is you by signing in with Google before we save a password to your account.
                </p>
                {stepupToken ? (
                  <p className="text-xs text-success">
                    Google verified. Enter your new password below to finish.
                  </p>
                ) : (
                  <button
                    type="button"
                    onClick={handleVerifyWithGoogle}
                    disabled={stepupBusy}
                    className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
                  >
                    {stepupBusy ? "Redirecting..." : "Verify with Google"}
                  </button>
                )}
              </div>
            )}
            <div>
              <label htmlFor="pwd-new" className={label}>New Password</label>
              <PasswordInput id="pwd-new" required minLength={8} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} className={input} autoComplete="new-password" />
            </div>
            <div>
              <label htmlFor="pwd-confirm" className={label}>Confirm New Password</label>
              <PasswordInput id="pwd-confirm" required value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} className={input} autoComplete="new-password" />
            </div>
            <button
              type="submit"
              disabled={savingPwd || (!passwordSet && !stepupToken)}
              className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}
            >
              {savingPwd
                ? (passwordSet ? "Changing..." : "Setting...")
                : (passwordSet ? "Change Password" : "Set Password")}
            </button>
          </form>
        </div>

        {/* ── Two-Factor Authentication ────────────────────────────────── */}
        <div className={`${card} p-6`}>
          <h2 className={`mb-5 ${cardTitle}`}>Two-Factor Authentication</h2>

          {mfaMsg && (
            <div role="status" aria-live="polite" className={`mb-4 ${successCls}`}>
              {mfaMsg}
            </div>
          )}
          {mfaErr && (
            <div role="alert" aria-live="polite" className={`mb-4 ${errorCls}`}>
              {mfaErr}
            </div>
          )}

          {/* ── Idle: Not enabled ─────────────────────────────────── */}
          {!mfaEnabled && mfaStep === "idle" && (
            <div>
              <p className="mb-4 text-sm text-text-muted">
                Add an extra layer of security to your account by requiring a verification code when you sign in.
              </p>
              <button
                onClick={handleSetup}
                disabled={mfaLoading}
                aria-busy={mfaLoading}
                className={`${btnPrimary} w-full sm:w-auto sm:min-h-0 inline-flex items-center justify-center gap-2`}
              >
                {mfaLoading && (
                  <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                )}
                {mfaLoading ? "Setting up..." : "Set Up Two-Factor Authentication"}
              </button>
            </div>
          )}

          {/* ── Step 1: QR Code ───────────────────────────────────── */}
          {mfaStep === "qr" && setupData && (
            <div className="space-y-4">
              <p className="text-sm text-text-muted">
                Scan this QR code with your authenticator app (Google Authenticator, Authy, 1Password, etc.)
              </p>
              <figure className="space-y-2">
                <div className="flex justify-center rounded-lg bg-white p-4">
                  <img
                    src={`data:image/png;base64,${setupData.qr_code}`}
                    alt="QR code for two-factor authentication. Scan with your authenticator app, or expand the manual key below."
                    className="h-48 w-48 max-w-full"
                  />
                </div>
                <figcaption className="text-center text-xs text-text-muted">
                  Scan with your authenticator app
                </figcaption>
              </figure>
              <details className="text-sm">
                <summary className="cursor-pointer text-text-muted hover:text-text-primary">
                  Can&apos;t scan? Enter this key manually
                </summary>
                <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-start">
                  <code
                    id="mfa-setup-secret"
                    className="flex-1 block rounded bg-surface-raised px-3 py-2 text-xs text-text-primary break-all select-all"
                  >
                    {setupData.secret}
                  </code>
                  <button
                    type="button"
                    onClick={() => copyToClipboard(setupData.secret, "secret")}
                    aria-describedby="mfa-setup-secret"
                    className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0 inline-flex items-center justify-center gap-2`}
                  >
                    {copiedKey === "secret" ? (
                      <>
                        <Check className="h-4 w-4" aria-hidden="true" />
                        Copied
                      </>
                    ) : (
                      <>
                        <Copy className="h-4 w-4" aria-hidden="true" />
                        Copy key
                      </>
                    )}
                  </button>
                </div>
                <p
                  role="status"
                  aria-live="polite"
                  className="mt-1.5 min-h-[1rem] text-xs text-success"
                >
                  {copiedKey === "secret" ? "Setup key copied to clipboard." : ""}
                </p>
              </details>
              <div className="flex flex-col gap-3 sm:flex-row">
                <button onClick={() => { setMfaStep("idle"); setSetupData(null); }} className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                  Cancel
                </button>
                <button onClick={() => setMfaStep("verify")} className={`${btnPrimary} w-full sm:w-auto sm:min-h-0`}>
                  Continue
                </button>
              </div>
            </div>
          )}

          {/* ── Step 2: Verify code ───────────────────────────────── */}
          {mfaStep === "verify" && (
            <form onSubmit={handleVerifyCode} className="space-y-4" aria-busy={mfaLoading}>
              <p className="text-sm text-text-muted">
                Enter the 6-digit code from your authenticator app to confirm it&apos;s working.
              </p>
              <div>
                <label htmlFor="mfa-setup-code" className={label}>Verification Code</label>
                <input
                  id="mfa-setup-code"
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  required
                  maxLength={6}
                  value={totpCode}
                  onChange={(e) => setTotpCode(e.target.value.replace(/\D/g, ""))}
                  className={`${input} text-center text-lg tracking-[0.3em]`}
                  placeholder="000000"
                  autoFocus
                  aria-describedby="mfa-setup-code-hint"
                  aria-invalid={mfaErr ? true : undefined}
                />
                <p id="mfa-setup-code-hint" className="mt-1.5 text-xs text-text-muted">
                  Codes refresh every 30 seconds. If one fails, wait for the next one.
                </p>
              </div>
              <div className="flex flex-col gap-3 sm:flex-row">
                <button
                  type="button"
                  onClick={() => setMfaStep("qr")}
                  disabled={mfaLoading}
                  className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
                >
                  Back
                </button>
                <button
                  type="submit"
                  disabled={mfaLoading || totpCode.length !== 6}
                  aria-busy={mfaLoading}
                  aria-describedby={
                    totpCode.length !== 6 && !mfaLoading ? "mfa-setup-code-hint" : undefined
                  }
                  className={`${btnPrimary} w-full sm:w-auto sm:min-h-0 inline-flex items-center justify-center gap-2`}
                >
                  {mfaLoading && (
                    <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                  )}
                  {mfaLoading ? "Verifying..." : "Verify and Enable"}
                </button>
              </div>
            </form>
          )}

          {/* ── Step 3: Recovery codes ────────────────────────────── */}
          {mfaStep === "codes" && recoveryCodes.length > 0 && (
            <div className="space-y-4">
              <p className="text-sm text-text-primary font-medium">
                Save your recovery codes
              </p>
              <p className="text-sm text-text-muted">
                If you lose access to your authenticator app, you can use these codes to sign in. Each code can only be used once. We will not show them again.
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 rounded-lg bg-surface-raised p-4">
                {recoveryCodes.map((code, i) => (
                  <code key={i} className="text-sm text-text-primary font-mono">
                    {i + 1}. {code}
                  </code>
                ))}
              </div>
              <div className="flex flex-col gap-2 sm:flex-row">
                <button
                  type="button"
                  onClick={() => copyToClipboard(recoveryCodes.join("\n"), "codes")}
                  className={`${btnSecondary} w-full sm:flex-1 min-h-[44px] sm:min-h-0 inline-flex items-center justify-center gap-2`}
                >
                  {copiedKey === "codes" ? (
                    <>
                      <Check className="h-4 w-4" aria-hidden="true" />
                      Copied
                    </>
                  ) : (
                    <>
                      <Copy className="h-4 w-4" aria-hidden="true" />
                      Copy codes
                    </>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => downloadCodes(recoveryCodes)}
                  className={`${btnSecondary} w-full sm:flex-1 min-h-[44px] sm:min-h-0`}
                >
                  Download codes
                </button>
              </div>
              <p
                role="status"
                aria-live="polite"
                className="min-h-[1rem] text-xs text-success"
              >
                {copiedKey === "codes" ? "Recovery codes copied to clipboard." : ""}
              </p>
              <label className="flex items-center gap-2 text-sm text-text-muted">
                <input
                  type="checkbox"
                  checked={codesSaved}
                  onChange={(e) => setCodesSaved(e.target.checked)}
                  className="rounded border-border"
                  aria-describedby="codes-saved-hint"
                />
                I have saved these recovery codes
              </label>
              <p id="codes-saved-hint" className="text-xs text-text-muted">
                Check the box to confirm. The Done button stays disabled until you do.
              </p>
              <button
                type="button"
                onClick={handleFinishSetup}
                disabled={!codesSaved}
                aria-describedby={!codesSaved ? "codes-saved-hint" : undefined}
                className={`w-full ${btnPrimary}`}
              >
                Done
              </button>
            </div>
          )}

          {/* ── Enabled state ─────────────────────────────────────── */}
          {mfaEnabled && mfaStep === "idle" && (
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <div className="h-2 w-2 rounded-full bg-success" />
                <span className="text-sm font-medium text-text-primary">Enabled</span>
              </div>
              <p className="text-sm text-text-muted">
                Your account is protected with two-factor authentication via an authenticator app.
              </p>

              {/* Regenerate recovery codes */}
              {!showRegen && regenCodes.length === 0 && (
                <button
                  type="button"
                  onClick={() => setShowRegen(true)}
                  className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
                >
                  Regenerate recovery codes
                </button>
              )}
              {showRegen && regenCodes.length === 0 && (
                <form
                  onSubmit={handleRegenerate}
                  className="space-y-3 rounded-lg border border-border p-4"
                  aria-busy={regenerating}
                >
                  <p className="text-sm text-text-primary font-medium">
                    Regenerating will invalidate your existing recovery codes.
                  </p>
                  <p className="text-xs text-text-muted">
                    Any codes you saved before will stop working as soon as new ones are generated. Save the new set before leaving this page.
                  </p>
                  {regenErr && (
                    <div id="regen-err" role="alert" aria-live="polite" className={errorCls}>
                      {regenErr}
                    </div>
                  )}
                  <div>
                    <label htmlFor="regen-pwd" className={label}>Confirm password</label>
                    <PasswordInput
                      id="regen-pwd"
                      required
                      value={regenPassword}
                      onChange={(e) => setRegenPassword(e.target.value)}
                      className={input}
                      aria-describedby={regenErr ? "regen-err" : undefined}
                      aria-invalid={regenErr ? true : undefined}
                    />
                  </div>
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <button
                      type="button"
                      onClick={() => { setShowRegen(false); setRegenPassword(""); setRegenErr(""); }}
                      disabled={regenerating}
                      className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
                    >
                      Cancel
                    </button>
                    <button
                      type="submit"
                      disabled={regenerating || !regenPassword}
                      aria-busy={regenerating}
                      className={`${btnPrimary} w-full sm:w-auto sm:min-h-0 inline-flex items-center justify-center gap-2`}
                    >
                      {regenerating && (
                        <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                      )}
                      {regenerating ? "Generating..." : "Regenerate"}
                    </button>
                  </div>
                </form>
              )}
              {regenCodes.length > 0 && (
                <div className="space-y-3">
                  <p className="text-sm font-medium text-text-primary">New recovery codes</p>
                  <p className="text-xs text-text-muted">
                    These replace any codes you saved before. Old codes will not work anymore.
                  </p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 rounded-lg bg-surface-raised p-4">
                    {regenCodes.map((code, i) => (
                      <code key={i} className="text-sm text-text-primary font-mono">
                        {i + 1}. {code}
                      </code>
                    ))}
                  </div>
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <button
                      type="button"
                      onClick={() => copyToClipboard(regenCodes.join("\n"), "regen-codes")}
                      className={`${btnSecondary} w-full sm:flex-1 min-h-[44px] sm:min-h-0 inline-flex items-center justify-center gap-2`}
                    >
                      {copiedKey === "regen-codes" ? (
                        <>
                          <Check className="h-4 w-4" aria-hidden="true" />
                          Copied
                        </>
                      ) : (
                        <>
                          <Copy className="h-4 w-4" aria-hidden="true" />
                          Copy codes
                        </>
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={() => downloadCodes(regenCodes)}
                      className={`${btnSecondary} w-full sm:flex-1 min-h-[44px] sm:min-h-0`}
                    >
                      Download codes
                    </button>
                  </div>
                  <p
                    role="status"
                    aria-live="polite"
                    className="min-h-[1rem] text-xs text-success"
                  >
                    {copiedKey === "regen-codes" ? "Recovery codes copied to clipboard." : ""}
                  </p>
                  <button
                    type="button"
                    onClick={() => { setRegenCodes([]); setShowRegen(false); }}
                    className={`w-full ${btnPrimary}`}
                  >
                    Done
                  </button>
                </div>
              )}

              {/* Disable MFA */}
              <div className="border-t border-border pt-4">
                {!showDisable ? (
                  <button
                    type="button"
                    onClick={() => setShowDisable(true)}
                    className={`${btnDanger} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
                  >
                    Disable two-factor authentication
                  </button>
                ) : (
                  <form onSubmit={handleDisable} className="space-y-3" aria-busy={disabling}>
                    <p className="text-sm text-text-primary font-medium">
                      Confirm with your password to turn off two-factor.
                    </p>
                    <p className="text-xs text-text-muted">
                      Your account will be protected by your password only. You can turn it back on at any time.
                    </p>
                    {disableErr && (
                      <div id="disable-err" role="alert" aria-live="polite" className={errorCls}>
                        {disableErr}
                      </div>
                    )}
                    <div>
                      <label htmlFor="disable-pwd" className={label}>Password</label>
                      <PasswordInput
                        id="disable-pwd"
                        required
                        value={disablePassword}
                        onChange={(e) => setDisablePassword(e.target.value)}
                        className={input}
                        aria-describedby={disableErr ? "disable-err" : undefined}
                        aria-invalid={disableErr ? true : undefined}
                      />
                    </div>
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <button
                        type="button"
                        onClick={() => { setShowDisable(false); setDisablePassword(""); setDisableErr(""); }}
                        disabled={disabling}
                        className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}
                      >
                        Cancel
                      </button>
                      <button
                        type="submit"
                        disabled={disabling || !disablePassword}
                        aria-busy={disabling}
                        className={`${btnPrimary} !bg-danger hover:!bg-danger/80 w-full sm:w-auto sm:min-h-0 inline-flex items-center justify-center gap-2`}
                      >
                        {disabling && (
                          <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                        )}
                        {disabling ? "Disabling..." : "Disable two-factor"}
                      </button>
                    </div>
                  </form>
                )}
              </div>
            </div>
          )}
        </div>

        {/* ── Email Fallback Info ──────────────────────────────────────── */}
        {mfaEnabled && mfaStep === "idle" && (
          <div className={`${card} p-6`}>
            <h2 className={`mb-3 ${cardTitle}`}>Email fallback</h2>
            <p className="text-sm text-text-muted">
              If you cannot access your authenticator app or recovery codes, a verification code can be sent to <span className="font-medium text-text-primary">{user?.email}</span>.
            </p>
          </div>
        )}

        {/* ── Session Lifetime (admin only) ────────────────────────────── */}
        {admin && (
          <div className={`${card} p-6`}>
            <h2 className={`mb-5 ${cardTitle}`}>Session lifetime</h2>
            <p className="mb-4 text-sm text-text-muted">
              Maximum number of days a user can stay signed in before being required to re-authenticate. Applies to all users in your organization.
            </p>
            <form onSubmit={handleSessionSubmit} className="space-y-4" aria-busy={savingSession}>
              {sessionMsg && (
                <div role="status" aria-live="polite" className={successCls}>
                  {sessionMsg}
                </div>
              )}
              {sessionErr && (
                <div id="session-err" role="alert" aria-live="polite" className={errorCls}>
                  {sessionErr}
                </div>
              )}
              <div>
                <label htmlFor="session-days" className={label}>Maximum session duration (days)</label>
                <input
                  id="session-days"
                  type="number"
                  min={1}
                  max={365}
                  required
                  value={sessionDays}
                  onChange={(e) => setSessionDays(e.target.value)}
                  className={`${input} w-full sm:max-w-[200px]`}
                  aria-describedby={sessionErr ? "session-err session-hint" : "session-hint"}
                  aria-invalid={sessionErr ? true : undefined}
                />
                <p id="session-hint" className="mt-1.5 text-xs text-text-muted">
                  Between 1 and 365 days.
                </p>
              </div>
              <button
                type="submit"
                disabled={savingSession}
                aria-busy={savingSession}
                className={`${btnPrimary} w-full sm:w-auto sm:min-h-0 inline-flex items-center justify-center gap-2`}
              >
                {savingSession && (
                  <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                )}
                {savingSession ? "Saving..." : "Save"}
              </button>
            </form>
          </div>
        )}
      </div>
    </SettingsLayout>
  );
}
