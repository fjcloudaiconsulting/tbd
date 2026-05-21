"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Turnstile, type TurnstileInstance } from "@marsidev/react-turnstile";
import { useAuth } from "@/components/auth/AuthProvider";
import GoogleSSOButton from "@/components/auth/GoogleSSOButton";
import { apiFetch } from "@/lib/api";
import PasswordInput from "@/components/ui/PasswordInput";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { input, label, btnPrimary, error as errorCls, success } from "@/lib/styles";
import {
  USERNAME_MAX_LENGTH,
  USERNAME_MIN_LENGTH,
  USERNAME_PATTERN,
  USERNAME_PATTERN_RE,
  USERNAME_RULE_HINT,
} from "@/lib/validation";

interface UsernameCheck {
  available: boolean;
  suggestion: string | null;
}

interface AuthStatus {
  needs_setup: boolean;
  captcha_required: boolean;
}

interface RegisterPageBodyProps {
  cspNonce: string;
}

export default function RegisterPageBody({ cspNonce }: RegisterPageBodyProps) {
  const { user, register, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && user) router.replace("/dashboard");
  }, [loading, user, router]);

  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [username, setUsername] = useState("");
  const [usernameManual, setUsernameManual] = useState(false);
  const [usernameStatus, setUsernameStatus] = useState<"" | "checking" | "available" | "taken">("");
  const [usernameSuggestion, setUsernameSuggestion] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [orgName, setOrgName] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [registered, setRegistered] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(false);
  // Backend-driven captcha gate. Read once on mount from /auth/status so
  // a backend CAPTCHA_REQUIRED flip is also a real frontend rollback on
  // the next page load (architect correction #2).
  const [captchaRequired, setCaptchaRequired] = useState(false);
  const [captchaToken, setCaptchaToken] = useState("");
  const turnstileRef = useRef<TurnstileInstance>(null);
  const captchaSiteKey = process.env.NEXT_PUBLIC_CAPTCHA_SITE_KEY ?? "";

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

  // Auto-suggest username from name
  useEffect(() => {
    if (usernameManual) return;
    const parts = [firstName, lastName].filter(Boolean).join(" ");
    if (!parts.trim()) return;
    const slug = parts.toLowerCase().trim().replace(/[^a-z0-9]+/g, ".").replace(/^\.+|\.+$/g, "");
    if (slug) setUsername(slug);
  }, [firstName, lastName, usernameManual]);

  // Check username availability (debounced, cancels stale requests).
  // We only call the endpoint when the value satisfies the server rules —
  // otherwise /check-username returns 422 and confuses the UI.
  const checkRef = useRef(0);
  const checkUsername = useCallback(async (name: string) => {
    if (name.length < USERNAME_MIN_LENGTH || !USERNAME_PATTERN_RE.test(name)) {
      setUsernameStatus("");
      return;
    }
    const id = ++checkRef.current;
    setUsernameStatus("checking");
    try {
      const result = await apiFetch<UsernameCheck>(`/api/v1/auth/check-username?username=${encodeURIComponent(name)}`);
      if (id !== checkRef.current) return; // stale response
      if (result.available) {
        setUsernameStatus("available");
        setUsernameSuggestion("");
      } else {
        setUsernameStatus("taken");
        setUsernameSuggestion(result.suggestion ?? "");
      }
    } catch {
      if (id === checkRef.current) setUsernameStatus("");
    }
  }, []);

  useEffect(() => {
    if (!username) { setUsernameStatus(""); return; }
    const timer = setTimeout(() => checkUsername(username), 400);
    return () => clearTimeout(timer);
  }, [username, checkUsername]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== password2) { setError("Passwords do not match"); return; }
    setSubmitting(true);
    try {
      await register(
        username,
        email,
        password,
        orgName || undefined,
        firstName || undefined,
        lastName || undefined,
        captchaToken || undefined,
      );
      setRegistered(true);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Registration failed";
      // The Cloudflare Turnstile token is single-use AND short-lived
      // (5 min). Any backend rejection that surfaces the captcha gate
      // must reset the widget so the user can complete it again
      // without a page reload.
      if (message.toLowerCase().includes("captcha")) {
        turnstileRef.current?.reset();
        setCaptchaToken("");
      }
      setError(message);
    } finally {
      setSubmitting(false);
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

  if (registered) {
    return (
      <div className="relative flex min-h-screen items-center justify-center px-4">
        <ThemeToggle className="absolute right-6 top-6" />
        <div className="w-full max-w-sm">
          <div className="mb-10 text-center">
            <h1 className="font-display text-3xl font-semibold text-text-primary">Check Your Email</h1>
          </div>
          <div className="space-y-5">
            <div className={success}>
              Account created! Check your email to verify your account.
            </div>
            <p className="text-center text-sm text-text-muted">
              <Link href="/login" className="text-accent hover:text-accent-hover">
                Go to login
              </Link>
            </p>
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
          {error && <div className={errorCls}>{error}</div>}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label htmlFor="reg-firstname" className={label}>First Name</label>
              <input id="reg-firstname" type="text" value={firstName} onChange={(e) => setFirstName(e.target.value)} className={input} autoComplete="given-name" placeholder="John" />
            </div>
            <div>
              <label htmlFor="reg-lastname" className={label}>Last Name</label>
              <input id="reg-lastname" type="text" value={lastName} onChange={(e) => setLastName(e.target.value)} className={input} autoComplete="family-name" placeholder="Doe" />
            </div>
          </div>
          <div>
            <label htmlFor="reg-username" className={label}>Username</label>
            <input
              id="reg-username"
              type="text"
              required
              minLength={USERNAME_MIN_LENGTH}
              maxLength={USERNAME_MAX_LENGTH}
              pattern={USERNAME_PATTERN}
              title={USERNAME_RULE_HINT}
              value={username}
              onChange={(e) => { setUsername(e.target.value); setUsernameManual(true); }}
              className={input}
              autoComplete="username"
            />
            {usernameStatus === "checking" && <p className="mt-1 text-xs text-text-muted">Checking...</p>}
            {usernameStatus === "available" && <p className="mt-1 text-xs text-success">Available</p>}
            {usernameStatus === "taken" && (
              <p className="mt-1 text-xs text-danger">
                Taken{usernameSuggestion && (
                  <>, try <button type="button" onClick={() => { setUsername(usernameSuggestion); setUsernameManual(true); }} className="text-accent underline">{usernameSuggestion}</button></>
                )}
              </p>
            )}
            {!usernameStatus && (
              <p className="mt-1 text-xs text-text-muted">{USERNAME_RULE_HINT}</p>
            )}
          </div>
          <div>
            <label htmlFor="reg-email" className={label}>Email</label>
            <input id="reg-email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={input} autoComplete="email" placeholder="you@example.com" />
          </div>
          <div>
            <label htmlFor="reg-org" className={label}>Organization <span className="normal-case tracking-normal">(optional)</span></label>
            <input id="reg-org" type="text" value={orgName} onChange={(e) => setOrgName(e.target.value)} placeholder="My Household" className={input} />
          </div>
          <div>
            <label htmlFor="reg-password" className={label}>Password</label>
            <PasswordInput id="reg-password" required minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} className={input} autoComplete="new-password" />
          </div>
          <div>
            <label htmlFor="reg-password2" className={label}>Confirm Password</label>
            <PasswordInput id="reg-password2" required value={password2} onChange={(e) => setPassword2(e.target.value)} className={input} autoComplete="new-password" />
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
                options={{ action: "register", appearance: "interaction-only" }}
                scriptOptions={{ nonce: cspNonce }}
                onSuccess={(token) => setCaptchaToken(token)}
                onExpire={() => setCaptchaToken("")}
                onError={() => setCaptchaToken("")}
              />
            </div>
          )}
          <button type="submit" disabled={submitting || usernameStatus === "taken"} className={`w-full ${btnPrimary}`}>
            {submitting ? "Creating account..." : "Create Account"}
          </button>
          {process.env.NEXT_PUBLIC_GOOGLE_SSO_ENABLED === "true" && (
            <>
              <div className="flex items-center gap-3 my-4">
                <div className="flex-1 border-t border-border" />
                <span className="text-xs text-text-muted">or</span>
                <div className="flex-1 border-t border-border" />
              </div>
              <GoogleSSOButton
                mode="signup"
                loading={googleLoading}
                onClick={handleGoogleLogin}
              />
            </>
          )}
        </form>
        <p className="mt-6 text-center text-sm text-text-muted">
          Already have an account?{" "}
          <Link href="/login" className="text-accent hover:text-accent-hover">Sign In</Link>
        </p>
      </div>
    </div>
  );
}
