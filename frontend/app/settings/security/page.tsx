"use client";

import { FormEvent, useEffect, useState } from "react";
import SettingsLayout from "@/components/SettingsLayout";
import { useAuth, MfaRequiredError } from "@/components/auth/AuthProvider";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { isAdmin } from "@/lib/auth";
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

type MfaStep = "idle" | "qr" | "verify" | "codes";

export default function SecurityPage() {
  const { user, login, refreshMe } = useAuth();

  // ── Change Password ──────────────────────────────────────────────────────
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [pwdMsg, setPwdMsg] = useState("");
  const [pwdErr, setPwdErr] = useState("");
  const [savingPwd, setSavingPwd] = useState(false);

  async function handlePasswordSubmit(e: FormEvent) {
    e.preventDefault();
    setPwdMsg(""); setPwdErr("");
    if (newPassword !== confirmPassword) { setPwdErr("New passwords do not match"); return; }
    setSavingPwd(true);
    try {
      await apiFetch("/api/v1/users/me/password", {
        method: "POST",
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      });
      setPwdMsg("Password changed. Signing in with new credentials...");
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
      setCurrentPassword(""); setNewPassword(""); setConfirmPassword("");
      setPwdMsg("Password changed successfully");
    } catch (err) { setPwdErr(extractErrorMessage(err)); }
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
    setMfaErr(""); setMfaLoading(true);
    try {
      const data = await apiFetch<MfaSetupResponse>("/api/v1/auth/mfa/setup", {
        method: "POST",
      });
      setSetupData(data);
      setMfaStep("qr");
    } catch (err) { setMfaErr(extractErrorMessage(err)); }
    finally { setMfaLoading(false); }
  }

  async function handleVerifyCode(e: FormEvent) {
    e.preventDefault();
    setMfaErr(""); setMfaLoading(true);
    try {
      const data = await apiFetch<MfaEnableResponse>("/api/v1/auth/mfa/enable", {
        method: "POST",
        body: JSON.stringify({ code: totpCode }),
      });
      setRecoveryCodes(data.recovery_codes);
      setMfaStep("codes");
    } catch (err) { setMfaErr(extractErrorMessage(err)); }
    finally { setMfaLoading(false); }
  }

  function handleFinishSetup() {
    setMfaStep("idle");
    setSetupData(null);
    setTotpCode("");
    setRecoveryCodes([]);
    setCodesSaved(false);
    setMfaMsg("Two-factor authentication enabled");
    refreshMe();
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
    setDisableErr(""); setDisabling(true);
    try {
      await apiFetch("/api/v1/auth/mfa/disable", {
        method: "POST",
        body: JSON.stringify({ password: disablePassword }),
      });
      setShowDisable(false);
      setDisablePassword("");
      setMfaMsg("Two-factor authentication disabled");
      refreshMe();
    } catch (err) { setDisableErr(extractErrorMessage(err)); }
    finally { setDisabling(false); }
  }

  async function handleRegenerate(e: FormEvent) {
    e.preventDefault();
    setRegenErr(""); setRegenerating(true);
    try {
      const data = await apiFetch<MfaEnableResponse>("/api/v1/auth/mfa/recovery-codes", {
        method: "POST",
        body: JSON.stringify({ password: regenPassword }),
      });
      setRegenCodes(data.recovery_codes);
      setRegenPassword("");
    } catch (err) { setRegenErr(extractErrorMessage(err)); }
    finally { setRegenerating(false); }
  }

  const mfaEnabled = user?.mfa_enabled ?? false;

  return (
    <SettingsLayout activeTab="/settings/security">

      <div className="max-w-lg space-y-6">
        {/* ── Change Password ──────────────────────────────────────────── */}
        <div className={`${card} p-6`}>
          <h2 className={`mb-5 ${cardTitle}`}>Change Password</h2>
          <form onSubmit={handlePasswordSubmit} className="space-y-4">
            {pwdMsg && <div className={successCls}>{pwdMsg}</div>}
            {pwdErr && <div className={errorCls}>{pwdErr}</div>}
            <div>
              <label htmlFor="pwd-current" className={label}>Current Password</label>
              <input id="pwd-current" type="password" required value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} className={input} autoComplete="current-password" />
            </div>
            <div>
              <label htmlFor="pwd-new" className={label}>New Password</label>
              <input id="pwd-new" type="password" required minLength={8} value={newPassword} onChange={(e) => setNewPassword(e.target.value)} className={input} autoComplete="new-password" />
            </div>
            <div>
              <label htmlFor="pwd-confirm" className={label}>Confirm New Password</label>
              <input id="pwd-confirm" type="password" required value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} className={input} autoComplete="new-password" />
            </div>
            <button type="submit" disabled={savingPwd} className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
              {savingPwd ? "Changing..." : "Change Password"}
            </button>
          </form>
        </div>

        {/* ── Two-Factor Authentication ────────────────────────────────── */}
        <div className={`${card} p-6`}>
          <h2 className={`mb-5 ${cardTitle}`}>Two-Factor Authentication</h2>

          {mfaMsg && <div className={`mb-4 ${successCls}`}>{mfaMsg}</div>}
          {mfaErr && <div className={`mb-4 ${errorCls}`}>{mfaErr}</div>}

          {/* ── Idle: Not enabled ─────────────────────────────────── */}
          {!mfaEnabled && mfaStep === "idle" && (
            <div>
              <p className="mb-4 text-sm text-text-muted">
                Add an extra layer of security to your account by requiring a verification code when you sign in.
              </p>
              <button onClick={handleSetup} disabled={mfaLoading} className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
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
              <div className="flex justify-center rounded-lg bg-white p-4">
                <img
                  src={`data:image/png;base64,${setupData.qr_code}`}
                  alt="TOTP QR Code"
                  className="h-48 w-48 max-w-full"
                />
              </div>
              <details className="text-sm">
                <summary className="cursor-pointer text-text-muted hover:text-text-primary">
                  Can&apos;t scan? Enter this key manually
                </summary>
                <code className="mt-2 block rounded bg-surface-raised px-3 py-2 text-xs text-text-primary break-all select-all">
                  {setupData.secret}
                </code>
              </details>
              <div className="flex flex-col gap-3 sm:flex-row">
                <button onClick={() => { setMfaStep("idle"); setSetupData(null); }} className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                  Cancel
                </button>
                <button onClick={() => setMfaStep("verify")} className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                  Continue
                </button>
              </div>
            </div>
          )}

          {/* ── Step 2: Verify code ───────────────────────────────── */}
          {mfaStep === "verify" && (
            <form onSubmit={handleVerifyCode} className="space-y-4">
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
                />
              </div>
              <div className="flex flex-col gap-3 sm:flex-row">
                <button type="button" onClick={() => setMfaStep("qr")} className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                  Back
                </button>
                <button type="submit" disabled={mfaLoading || totpCode.length !== 6} className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                  {mfaLoading ? "Verifying..." : "Verify & Enable"}
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
                If you lose access to your authenticator app, you can use these codes to sign in. Each code can only be used once.
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 rounded-lg bg-surface-raised p-4">
                {recoveryCodes.map((code, i) => (
                  <code key={i} className="text-sm text-text-primary font-mono">
                    {i + 1}. {code}
                  </code>
                ))}
              </div>
              <button onClick={() => downloadCodes(recoveryCodes)} className={`w-full ${btnSecondary}`}>
                Download Codes
              </button>
              <label className="flex items-center gap-2 text-sm text-text-muted">
                <input type="checkbox" checked={codesSaved} onChange={(e) => setCodesSaved(e.target.checked)} className="rounded border-border" />
                I&apos;ve saved these recovery codes
              </label>
              <button onClick={handleFinishSetup} disabled={!codesSaved} className={`w-full ${btnPrimary}`}>
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
                <button onClick={() => setShowRegen(true)} className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                  Regenerate Recovery Codes
                </button>
              )}
              {showRegen && regenCodes.length === 0 && (
                <form onSubmit={handleRegenerate} className="space-y-3 rounded-lg border border-border p-4">
                  <p className="text-xs text-text-muted">This will invalidate your existing recovery codes.</p>
                  {regenErr && <div className={errorCls}>{regenErr}</div>}
                  <div>
                    <label htmlFor="regen-pwd" className={label}>Confirm Password</label>
                    <input id="regen-pwd" type="password" required value={regenPassword} onChange={(e) => setRegenPassword(e.target.value)} className={input} />
                  </div>
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <button type="button" onClick={() => { setShowRegen(false); setRegenPassword(""); }} className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>Cancel</button>
                    <button type="submit" disabled={regenerating} className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                      {regenerating ? "Generating..." : "Regenerate"}
                    </button>
                  </div>
                </form>
              )}
              {regenCodes.length > 0 && (
                <div className="space-y-3">
                  <p className="text-sm font-medium text-text-primary">New Recovery Codes</p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 rounded-lg bg-surface-raised p-4">
                    {regenCodes.map((code, i) => (
                      <code key={i} className="text-sm text-text-primary font-mono">
                        {i + 1}. {code}
                      </code>
                    ))}
                  </div>
                  <button onClick={() => downloadCodes(regenCodes)} className={`w-full ${btnSecondary}`}>
                    Download Codes
                  </button>
                  <button onClick={() => { setRegenCodes([]); setShowRegen(false); }} className={`w-full ${btnPrimary}`}>
                    Done
                  </button>
                </div>
              )}

              {/* Disable MFA */}
              <div className="border-t border-border pt-4">
                {!showDisable ? (
                  <button onClick={() => setShowDisable(true)} className={`${btnDanger} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                    Disable Two-Factor Authentication
                  </button>
                ) : (
                  <form onSubmit={handleDisable} className="space-y-3">
                    <p className="text-xs text-text-muted">Enter your password to disable two-factor authentication.</p>
                    {disableErr && <div className={errorCls}>{disableErr}</div>}
                    <div>
                      <label htmlFor="disable-pwd" className={label}>Password</label>
                      <input id="disable-pwd" type="password" required value={disablePassword} onChange={(e) => setDisablePassword(e.target.value)} className={input} />
                    </div>
                    <div className="flex flex-col gap-2 sm:flex-row">
                      <button type="button" onClick={() => { setShowDisable(false); setDisablePassword(""); }} className={`${btnSecondary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>Cancel</button>
                      <button type="submit" disabled={disabling} className={`${btnPrimary} !bg-danger hover:!bg-danger/80 w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                        {disabling ? "Disabling..." : "Disable MFA"}
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
            <h2 className={`mb-3 ${cardTitle}`}>Email Fallback</h2>
            <p className="text-sm text-text-muted">
              If you can&apos;t access your authenticator app or recovery codes, a verification code can be sent to <span className="font-medium text-text-primary">{user?.email}</span>.
            </p>
          </div>
        )}

        {/* ── Session Lifetime (admin only) ────────────────────────────── */}
        {admin && (
          <div className={`${card} p-6`}>
            <h2 className={`mb-5 ${cardTitle}`}>Session Lifetime</h2>
            <p className="mb-4 text-sm text-text-muted">
              Maximum number of days a user can stay signed in before being required to re-authenticate. Applies to all users in your organization.
            </p>
            <form onSubmit={handleSessionSubmit} className="space-y-4">
              {sessionMsg && <div className={successCls}>{sessionMsg}</div>}
              {sessionErr && <div className={errorCls}>{sessionErr}</div>}
              <div>
                <label htmlFor="session-days" className={label}>Maximum Session Duration (days)</label>
                <input
                  id="session-days"
                  type="number"
                  min={1}
                  max={365}
                  required
                  value={sessionDays}
                  onChange={(e) => setSessionDays(e.target.value)}
                  className={`${input} w-full sm:max-w-[200px]`}
                />
              </div>
              <button type="submit" disabled={savingSession} className={`${btnPrimary} w-full sm:w-auto min-h-[44px] sm:min-h-0`}>
                {savingSession ? "Saving..." : "Save"}
              </button>
            </form>
          </div>
        )}
      </div>
    </SettingsLayout>
  );
}
