"use client";

import { runtimeConfig } from "@/lib/runtime-config";
import { FormEvent, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Turnstile, type TurnstileInstance } from "@marsidev/react-turnstile";
import { useAuth } from "@/components/auth/AuthProvider";
import GoogleSSOButton from "@/components/auth/GoogleSSOButton";
import { apiFetch, ApiResponseError } from "@/lib/api";
import { startGoogleSignIn } from "@/lib/google-sso";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { input, label, btnPrimary, btnLink, error as errorCls, success } from "@/lib/styles";

interface AuthStatus {
  needs_setup: boolean;
  captcha_required: boolean;
}

interface RegisterPageBodyProps {
  cspNonce: string;
}

/**
 * Email-first sign-up, step 1 (TBD-654). Only the address is asked for; the
 * emailed link leads to /signup/complete. The backend answers the same 202
 * for every address, so this page shows the same "Check your email" panel
 * whatever the address's state, and nothing here may tell those states apart.
 */
export default function RegisterPageBody({ cspNonce }: RegisterPageBodyProps) {
  const { user, needsSetup, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && needsSetup) router.replace("/setup");
    if (!loading && user) router.replace("/dashboard");
  }, [loading, needsSetup, user, router]);

  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  // A 422 is about the address itself; other errors are not the field's fault.
  const [emailInvalid, setEmailInvalid] = useState(false);
  // The per-IP limit on /signup/start (429). It depends on the caller, never
  // on the address, so naming it reveals nothing about any account.
  const [limited, setLimited] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [sent, setSent] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(false);
  // Backend-driven captcha gate. Read once on mount from /auth/status so
  // a backend CAPTCHA_REQUIRED flip is also a real frontend rollback on
  // the next page load (architect correction #2).
  const [captchaRequired, setCaptchaRequired] = useState(false);
  const [captchaToken, setCaptchaToken] = useState("");
  const turnstileRef = useRef<TurnstileInstance>(null);
  const sentHeadingRef = useRef<HTMLHeadingElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const returnedRef = useRef(false);
  const captchaSiteKey = runtimeConfig().captchaSiteKey;
  const googleEnabled = runtimeConfig().googleSsoEnabled;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await apiFetch<AuthStatus>("/api/v1/auth/status");
        if (!cancelled) setCaptchaRequired(Boolean(status.captcha_required));
      } catch {
        // Treat a status fetch failure as "captcha not required" so the
        // form remains usable; the backend is the real enforcement.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Move focus with the screen change so a screen reader announces it.
  useEffect(() => {
    if (sent) sentHeadingRef.current?.focus();
    else if (returnedRef.current) emailRef.current?.focus();
  }, [sent]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setEmailInvalid(false);
    setLimited(false);
    setSubmitting(true);
    try {
      await apiFetch("/api/v1/auth/signup/start", {
        method: "POST",
        body: JSON.stringify({ email, captcha_token: captchaToken || undefined }),
      });
      // The widget unmounts with the form; its token is single-use anyway.
      setCaptchaToken("");
      setSent(true);
    } catch (err) {
      if (err instanceof ApiResponseError && err.code === "setup_required") {
        router.replace("/setup");
        return;
      }
      if (err instanceof ApiResponseError && err.status === 429) {
        setLimited(true);
        return;
      }
      // The Cloudflare Turnstile token is single-use AND short-lived
      // (5 min). Match the backend's structured ``code=captcha_failed``
      // (NOT the human-readable message) so a copy edit to the message
      // never silently breaks the widget reset. A 5xx comes after the
      // captcha check, so the token is spent then too.
      if (err instanceof ApiResponseError && (err.code === "captcha_failed" || err.status >= 500)) {
        turnstileRef.current?.reset();
        setCaptchaToken("");
      }
      setEmailInvalid(err instanceof ApiResponseError && err.status === 422);
      setError(err instanceof Error ? err.message : "Sign-up failed");
    } finally {
      setSubmitting(false);
    }
  }

  function startOver() {
    returnedRef.current = true;
    setSent(false);
  }

  async function handleGoogleLogin() {
    setGoogleLoading(true);
    try {
      // FE-02: also stores the nonce the callback page checks.
      await startGoogleSignIn();
    } catch (err) {
      setGoogleLoading(false);
      setError(err instanceof Error ? err.message : "Google sign-in is not available");
    }
  }

  const googleButton = googleEnabled && (
    <>
      <div className="flex items-center gap-3 my-4">
        <div className="flex-1 border-t border-border" />
        <span className="text-xs text-text-muted">or</span>
        <div className="flex-1 border-t border-border" />
      </div>
      <GoogleSSOButton mode="signup" loading={googleLoading} onClick={handleGoogleLogin} />
    </>
  );

  if (sent) {
    return (
      <div className="relative flex min-h-screen items-center justify-center px-4">
        <ThemeToggle className="absolute right-6 top-6" />
        <div className="w-full max-w-sm">
          <div className="mb-10 text-center">
            <h1
              ref={sentHeadingRef}
              tabIndex={-1}
              className="font-display text-3xl font-semibold text-text-primary"
            >
              Check Your Email
            </h1>
          </div>
          <div className="space-y-5">
            <div className={success} role="status">
              We sent a message to <span className="font-medium">{email}</span>. Follow the link in
              it to continue signing up. The link expires in 1 hour.
            </div>
            {/* Same help for every address: a capped address also gets a 202. */}
            <p className="text-sm text-text-muted">
              No email after a few minutes? Check your spam folder. We send at most one email to an
              address every 10 minutes, so try again later
              {googleEnabled ? ", or sign up with Google instead." : "."}
            </p>
            <button type="button" onClick={startOver} className={`w-full ${btnLink}`}>
              Use a different email
            </button>
            {error && <div className={errorCls} role="alert">{error}</div>}
            {googleButton}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />

      <div className="w-full max-w-sm">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">Create Account</h1>
          <p className="mt-1.5 text-sm text-text-muted">Join The Better Decision</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-5">
          {error && <div id="reg-error" className={errorCls} role="alert">{error}</div>}
          {limited && (
            <div className={errorCls} role="alert">
              Too many sign-up attempts from this connection. Try again later
              {googleEnabled ? ", or sign up with Google instead." : "."}
            </div>
          )}
          <div>
            <label htmlFor="reg-email" className={label}>Email</label>
            <input
              ref={emailRef}
              id="reg-email"
              type="email"
              required
              maxLength={120}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className={input}
              autoComplete="email"
              placeholder="you@example.com"
              aria-invalid={emailInvalid ? true : undefined}
              aria-describedby={error ? "reg-email-hint reg-error" : "reg-email-hint"}
            />
            <p id="reg-email-hint" className="mt-1 text-xs text-text-muted">
              We&apos;ll email you a link to choose your username and password.
            </p>
          </div>
          <p className="text-xs text-text-muted">
            By creating an account you agree to our{" "}
            <Link href="/terms" className="underline hover:text-text-primary">
              Terms of Service
            </Link>{" "}
            and{" "}
            <Link href="/privacy" className="underline hover:text-text-primary">
              Privacy Policy
            </Link>
            .
          </p>
          {captchaRequired && captchaSiteKey && (
            <div data-testid="captcha-widget">
              <Turnstile
                ref={turnstileRef}
                siteKey={captchaSiteKey}
                options={{ action: "register", appearance: "always" }}
                scriptOptions={{ nonce: cspNonce }}
                onSuccess={(token) => setCaptchaToken(token)}
                onExpire={() => setCaptchaToken("")}
                onError={() => setCaptchaToken("")}
              />
            </div>
          )}
          <button type="submit" disabled={submitting} className={`w-full ${btnPrimary}`}>
            {submitting ? "Sending..." : "Continue"}
          </button>
          {googleButton}
        </form>
        <p className="mt-6 text-center text-sm text-text-muted">
          Already have an account?{" "}
          <Link href="/login" className="text-accent hover:text-accent-hover">Sign In</Link>
        </p>
      </div>
    </div>
  );
}
