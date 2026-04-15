"use client";

import { FormEvent, Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { apiFetch, setAccessToken } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { input, label, btnPrimary, error as errorCls, success as successCls } from "@/lib/styles";
import type { TokenResponse } from "@/lib/types";

type Mode = "totp" | "recovery" | "email";

export default function MfaVerifyPage() {
  return (
    <Suspense fallback={
      <div className="flex min-h-screen items-center justify-center">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
      </div>
    }>
      <MfaVerifyContent />
    </Suspense>
  );
}

function MfaVerifyContent() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("totp");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [emailToken, setEmailToken] = useState<string | null>(null);
  const [emailSending, setEmailSending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const searchParams = useSearchParams();

  // Support mfa_token from sessionStorage (password login) or query param (Google SSO redirect)
  const mfaToken = typeof window !== "undefined"
    ? (sessionStorage.getItem("mfa_token") || searchParams.get("mfa_token"))
    : null;

  useEffect(() => {
    if (!loading && user) { router.replace("/dashboard"); return; }
    if (!mfaToken) { router.replace("/login"); }
  }, [loading, user, mfaToken, router]);

  useEffect(() => {
    setCode("");
    setError("");
    setMessage("");
    inputRef.current?.focus();
  }, [mode]);

  async function completeLogin(data: TokenResponse) {
    setAccessToken(data.access_token);
    sessionStorage.removeItem("mfa_token");
    // Full page load so AuthProvider picks up the new token via refresh
    window.location.href = "/dashboard";
  }

  async function handleTotpSubmit(e: FormEvent) {
    e.preventDefault();
    if (!mfaToken) return;
    setError(""); setSubmitting(true);
    try {
      const data = await apiFetch<TokenResponse>("/api/v1/auth/mfa/verify", {
        method: "POST",
        body: JSON.stringify({ mfa_token: mfaToken, code }),
      });
      await completeLogin(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Verification failed");
    } finally { setSubmitting(false); }
  }

  async function handleRecoverySubmit(e: FormEvent) {
    e.preventDefault();
    if (!mfaToken) return;
    setError(""); setSubmitting(true);
    try {
      const data = await apiFetch<TokenResponse>("/api/v1/auth/mfa/recovery", {
        method: "POST",
        body: JSON.stringify({ mfa_token: mfaToken, code }),
      });
      await completeLogin(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Invalid recovery code");
    } finally { setSubmitting(false); }
  }

  async function handleSendEmail() {
    if (!mfaToken) return;
    setEmailSending(true); setError(""); setMessage("");
    try {
      const data = await apiFetch<{ detail: string; email_token: string }>("/api/v1/auth/mfa/email-code", {
        method: "POST",
        body: JSON.stringify({ mfa_token: mfaToken }),
      });
      setEmailToken(data.email_token);
      setMode("email");
      setMessage("Code sent to your email");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to send code");
    } finally { setEmailSending(false); }
  }

  async function handleEmailSubmit(e: FormEvent) {
    e.preventDefault();
    if (!mfaToken || !emailToken) return;
    setError(""); setSubmitting(true);
    try {
      const data = await apiFetch<TokenResponse>("/api/v1/auth/mfa/email-verify", {
        method: "POST",
        body: JSON.stringify({ mfa_token: mfaToken, email_token: emailToken, code }),
      });
      await completeLogin(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Invalid code");
    } finally { setSubmitting(false); }
  }

  if (!mfaToken) return null;

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />

      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <h1 className="font-display text-2xl font-semibold text-text-primary">Two-Factor Authentication</h1>
          <p className="mt-1.5 text-sm text-text-muted">
            {mode === "totp" && "Enter the code from your authenticator app"}
            {mode === "recovery" && "Enter one of your recovery codes"}
            {mode === "email" && "Enter the code sent to your email"}
          </p>
        </div>

        {error && <div className={`mb-4 ${errorCls}`}>{error}</div>}
        {message && <div className={`mb-4 ${successCls}`}>{message}</div>}

        {mode === "totp" && (
          <form onSubmit={handleTotpSubmit} className="space-y-5">
            <div>
              <label htmlFor="totp-code" className={label}>Verification Code</label>
              <input
                ref={inputRef}
                id="totp-code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                required
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                className={`${input} text-center text-lg tracking-[0.3em]`}
                placeholder="000000"
              />
            </div>
            <button type="submit" disabled={submitting || code.length !== 6} className={`w-full ${btnPrimary}`}>
              {submitting ? "Verifying..." : "Verify"}
            </button>
          </form>
        )}

        {mode === "recovery" && (
          <form onSubmit={handleRecoverySubmit} className="space-y-5">
            <div>
              <label htmlFor="recovery-code" className={label}>Recovery Code</label>
              <input
                ref={inputRef}
                id="recovery-code"
                type="text"
                required
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className={`${input} text-center tracking-wider`}
                placeholder="xxxx-xxxx-xxxx-xxxx"
              />
            </div>
            <button type="submit" disabled={submitting || !code.trim()} className={`w-full ${btnPrimary}`}>
              {submitting ? "Verifying..." : "Verify"}
            </button>
          </form>
        )}

        {mode === "email" && (
          <form onSubmit={handleEmailSubmit} className="space-y-5">
            <div>
              <label htmlFor="email-code" className={label}>Email Code</label>
              <input
                ref={inputRef}
                id="email-code"
                type="text"
                inputMode="numeric"
                required
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                className={`${input} text-center text-lg tracking-[0.3em]`}
                placeholder="000000"
              />
            </div>
            <button type="submit" disabled={submitting || code.length !== 6} className={`w-full ${btnPrimary}`}>
              {submitting ? "Verifying..." : "Verify"}
            </button>
          </form>
        )}

        <div className="mt-6 space-y-2 text-center text-sm">
          {mode !== "totp" && (
            <button onClick={() => setMode("totp")} className="block w-full text-accent hover:text-accent-hover">
              Use authenticator app
            </button>
          )}
          {mode !== "recovery" && (
            <button onClick={() => setMode("recovery")} className="block w-full text-text-muted hover:text-accent">
              Use a recovery code
            </button>
          )}
          {mode !== "email" && (
            <button onClick={handleSendEmail} disabled={emailSending} className="block w-full text-text-muted hover:text-accent">
              {emailSending ? "Sending..." : "Send a code to my email"}
            </button>
          )}
          <button onClick={() => { sessionStorage.removeItem("mfa_token"); router.push("/login"); }} className="block w-full text-xs text-text-muted hover:text-text-primary mt-4">
            Back to login
          </button>
        </div>
      </div>
    </div>
  );
}
