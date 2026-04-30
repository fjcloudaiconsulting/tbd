"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useAuth, MfaRequiredError } from "@/components/auth/AuthProvider";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { ApiResponseError, apiFetch } from "@/lib/api";
import { input, label, btnPrimary, btnSecondary, error as errorCls } from "@/lib/styles";

type ResendState = "idle" | "sending" | "sent" | "failed";

export default function LoginPageBody() {
  const { user, login, loading, needsSetup } = useAuth();
  const router = useRouter();
  const [loginId, setLoginId] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // Snapshotted at the moment the email-not-verified error fires so the
  // resend button targets the same login the user actually tried, even
  // if they edit the input afterward.
  const [unverifiedLogin, setUnverifiedLogin] = useState<string | null>(null);
  const [resendState, setResendState] = useState<ResendState>("idle");

  useEffect(() => {
    if (!loading && needsSetup) router.replace("/setup");
    if (!loading && user) router.replace("/dashboard");
  }, [loading, needsSetup, user, router]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setUnverifiedLogin(null);
    setResendState("idle");
    setSubmitting(true);
    try {
      await login(loginId, password);
      router.push("/dashboard");
    } catch (err) {
      if (err instanceof MfaRequiredError) {
        sessionStorage.setItem("mfa_token", err.mfaToken);
        router.push("/mfa-verify");
        return;
      }
      if (err instanceof ApiResponseError && err.code === "email_not_verified") {
        setUnverifiedLogin(loginId);
      }
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleResendVerification() {
    if (!unverifiedLogin) return;
    setResendState("sending");
    try {
      await apiFetch("/api/v1/auth/resend-verification-public", {
        method: "POST",
        body: JSON.stringify({ login: unverifiedLogin }),
      });
      setResendState("sent");
    } catch {
      setResendState("failed");
    }
  }

  async function handleGoogleLogin() {
    try {
      const data = await apiFetch<{ redirect_url: string }>("/api/v1/auth/google");
      window.location.href = data.redirect_url;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Google sign-in is not available");
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />

      <div className="w-full max-w-sm">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">The Better Decision</h1>
          <p className="mt-1.5 text-sm text-text-muted">Sign in</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-5">
          {error && (
            <div className={errorCls} role="alert">
              <p>{error}</p>
              {unverifiedLogin && (
                <div className="mt-2">
                  <p className="text-xs text-text-muted">
                    For{" "}
                    <span className="font-medium text-text-secondary">
                      {unverifiedLogin}
                    </span>
                  </p>
                  {resendState === "sent" ? (
                    <p className="text-xs text-text-muted">
                      Verification email sent. Check your inbox.
                    </p>
                  ) : (
                    <button
                      type="button"
                      onClick={handleResendVerification}
                      disabled={resendState === "sending"}
                      className="text-xs font-medium text-accent hover:text-accent-hover disabled:opacity-50"
                    >
                      {resendState === "sending"
                        ? "Sending..."
                        : resendState === "failed"
                          ? "Send failed — try again"
                          : "Resend verification email"}
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
          <div>
            <label htmlFor="login-id" className={label}>Email or Username</label>
            <input id="login-id" type="text" required value={loginId} onChange={(e) => setLoginId(e.target.value)} className={input} autoComplete="username" placeholder="you@example.com" />
          </div>
          <div>
            <label htmlFor="login-password" className={label}>Password</label>
            <input id="login-password" type="password" required value={password} onChange={(e) => setPassword(e.target.value)} className={input} autoComplete="current-password" />
          </div>
          <div className="text-right">
            <Link href="/forgot-password" className="text-xs text-accent hover:text-accent-hover">Forgot your password?</Link>
          </div>
          <button type="submit" disabled={submitting} className={`w-full ${btnPrimary}`}>
            {submitting ? "Signing in..." : "Sign In"}
          </button>
          <div className="flex items-center gap-3 my-4">
            <div className="flex-1 border-t border-border" />
            <span className="text-xs text-text-muted">or</span>
            <div className="flex-1 border-t border-border" />
          </div>
          <button onClick={handleGoogleLogin} className={btnSecondary + " w-full"} type="button">
            Sign in with Google
          </button>
        </form>
        <p className="mt-6 text-center text-sm text-text-muted">
          Don&apos;t have an account?{" "}
          <Link href="/register" className="text-accent hover:text-accent-hover">Register</Link>
        </p>
      </div>
    </div>
  );
}
