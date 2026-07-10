"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { useAuth, MfaRequiredError } from "@/components/auth/AuthProvider";
import GoogleSSOButton from "@/components/auth/GoogleSSOButton";
import PasswordInput from "@/components/ui/PasswordInput";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { ApiResponseError, apiFetch } from "@/lib/api";
import { input, label, btnPrimary, btnSecondary, error as errorCls } from "@/lib/styles";
import { sanitizeReturnTo } from "@/lib/returnTo";

type ResendState = "idle" | "sending" | "sent" | "failed";

// Banner copy for the ``?reason=`` the AppShell re-auth redirect carries.
// "expired" = involuntary (session died mid-use); "logout" = voluntary
// sign-out. Any other / absent value shows no banner. Copy avoids
// em-dashes per the project copy rule.
const REASON_COPY: Record<string, string> = {
  expired: "Your session expired. Please sign in to continue.",
  logout: "You've been signed out.",
};

/**
 * Friendly copy keyed by the `?sso_error=<code>` value the backend
 * redirects with on a /google/callback failure. Keep entries in sync
 * with `backend/app/routers/auth.py:google_callback` — every code the
 * backend emits must have a copy entry here, with a default fallback
 * for any future code that ships before the frontend catches up.
 */
const SSO_ERROR_COPY: Record<string, string> = {
  state: "Your Google sign-in attempt expired. Try again.",
  token: "Google sign-in failed. Try again, or sign in with a password.",
  userinfo: "Google sign-in failed. Try again, or sign in with a password.",
  unverified:
    "Your Google account isn't verified. Verify it with Google or sign in with a password.",
  deactivated:
    "This account is deactivated. Contact support if this is unexpected.",
  no_email: "Google didn't return an email for this account.",
  cancelled:
    "You cancelled the Google sign-in. Try again whenever you're ready.",
  provider_error:
    "Google returned an error during sign-in. Try again, or sign in with a password.",
};
const SSO_ERROR_FALLBACK = "Google sign-in didn't complete. Try again.";

export default function LoginPageBody() {
  const { user, login, loading, needsSetup } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [loginId, setLoginId] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  // Snapshotted at the moment the email-not-verified error fires so the
  // resend button targets the same login the user actually tried, even
  // if they edit the input afterward.
  const [unverifiedLogin, setUnverifiedLogin] = useState<string | null>(null);
  const [resendState, setResendState] = useState<ResendState>("idle");
  const [googleLoading, setGoogleLoading] = useState(false);
  // `?sso_error=<code>` arrives via the 307 from /api/v1/auth/google/callback
  // when the Google round-trip fails (expired state cookie, token-exchange
  // error, etc.). We surface it as a dismissable banner and clear the
  // query string after the user dismisses or retries so a refresh
  // doesn't reshow it.
  const ssoErrorCode = searchParams?.get("sso_error");
  const [ssoErrorVisible, setSsoErrorVisible] = useState<boolean>(false);
  useEffect(() => {
    setSsoErrorVisible(Boolean(ssoErrorCode));
  }, [ssoErrorCode]);

  // Where to land after a successful login (or the already-authenticated
  // bounce). Open-redirect-guarded: only a same-origin relative path is
  // honored, everything else falls back to /dashboard. Single-shot — read
  // from the URL, never persisted.
  const returnTo = sanitizeReturnTo(searchParams?.get("returnTo"));
  // Why the user was sent here (session expired vs signed out). Selects the
  // banner copy; unknown/absent → no banner.
  const reasonCode = searchParams?.get("reason") ?? "";
  const reasonMessage = REASON_COPY[reasonCode];

  function clearSsoErrorFromUrl() {
    setSsoErrorVisible(false);
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    url.searchParams.delete("sso_error");
    router.replace(url.pathname + (url.search || "") + url.hash);
  }

  useEffect(() => {
    if (!loading && needsSetup) router.replace("/setup");
    if (!loading && user) router.replace(returnTo);
  }, [loading, needsSetup, user, router, returnTo]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setUnverifiedLogin(null);
    setResendState("idle");
    setSubmitting(true);
    try {
      await login(loginId, password);
      router.push(returnTo);
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
    setGoogleLoading(true);
    try {
      const data = await apiFetch<{ redirect_url: string }>("/api/v1/auth/google");
      window.location.href = data.redirect_url;
    } catch (err) {
      setGoogleLoading(false);
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
        {reasonMessage && (
          <div
            className="mb-5 rounded-md bg-info-dim px-4 py-3 text-sm text-info"
            role="status"
            data-testid="auth-reason-banner"
          >
            <p>{reasonMessage}</p>
          </div>
        )}
        {ssoErrorVisible && ssoErrorCode && (
          <div
            className={`${errorCls} mb-5`}
            role="alert"
            data-testid="sso-error-banner"
          >
            <p>{SSO_ERROR_COPY[ssoErrorCode] ?? SSO_ERROR_FALLBACK}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => {
                  clearSsoErrorFromUrl();
                  handleGoogleLogin();
                }}
                disabled={googleLoading}
                className={`${btnPrimary} text-xs`}
              >
                {googleLoading ? "Redirecting..." : "Try again with Google"}
              </button>
              <button
                type="button"
                onClick={clearSsoErrorFromUrl}
                className={`${btnSecondary} text-xs`}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}
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
                          ? "Send failed. Try again"
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
            <PasswordInput id="login-password" required value={password} onChange={(e) => setPassword(e.target.value)} className={input} autoComplete="current-password" />
          </div>
          <div className="text-right">
            <Link href="/forgot-password" className="text-xs text-accent hover:text-accent-hover">Forgot your password?</Link>
          </div>
          <button type="submit" disabled={submitting} className={`w-full ${btnPrimary}`}>
            {submitting ? "Signing in..." : "Sign In"}
          </button>
          {process.env.NEXT_PUBLIC_GOOGLE_SSO_ENABLED === "true" && (
            <>
              <div className="flex items-center gap-3 my-4">
                <div className="flex-1 border-t border-border" />
                <span className="text-xs text-text-muted">or</span>
                <div className="flex-1 border-t border-border" />
              </div>
              <GoogleSSOButton
                mode="signin"
                loading={googleLoading}
                onClick={handleGoogleLogin}
              />
            </>
          )}
        </form>
        <p className="mt-6 text-center text-sm text-text-muted">
          Don&apos;t have an account?{" "}
          <Link href="/register" className="text-accent hover:text-accent-hover">Register</Link>
        </p>
      </div>
    </div>
  );
}
