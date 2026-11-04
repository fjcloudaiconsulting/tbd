"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth/AuthProvider";
import PasswordInput from "@/components/ui/PasswordInput";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { ApiResponseError, apiFetch, signInWithAccessToken } from "@/lib/api";
import { useOneTimeQueryParam } from "@/lib/hooks/use-one-time-query-param";
import { input, label, btnPrimary, error as errorCls } from "@/lib/styles";
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

/**
 * Email-first sign-up, step 2 (TBD-654). The emailed link carries a signup
 * token; loading the page never redeems it, only the POST does. The address
 * is encrypted inside the token, so it is not shown.
 */
export default function SignupCompleteBody() {
  const router = useRouter();
  const token = useOneTimeQueryParam("token");
  const { refreshMe } = useAuth();

  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [username, setUsername] = useState("");
  const [usernameStatus, setUsernameStatus] = useState<"" | "checking" | "available" | "taken">("");
  const [usernameSuggestion, setUsernameSuggestion] = useState("");
  const [usernameError, setUsernameError] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [orgName, setOrgName] = useState("");
  const [orgError, setOrgError] = useState("");
  const [error, setError] = useState("");
  const [mismatch, setMismatch] = useState(false);
  // "missing": the URL had no token, e.g. a reload after the page stripped it.
  const [blocked, setBlocked] = useState<"missing" | "expired" | "account_exists" | null>(token ? null : "missing");
  const [submitting, setSubmitting] = useState(false);
  const usernameRef = useRef<HTMLInputElement>(null);
  const orgRef = useRef<HTMLInputElement>(null);
  const password2Ref = useRef<HTMLInputElement>(null);
  const blockedRef = useRef<HTMLDivElement>(null);

  // Only a submit answer replaces the form; move focus to what replaced it.
  useEffect(() => {
    if (blocked === "expired" || blocked === "account_exists") blockedRef.current?.focus();
  }, [blocked]);

  // Debounced availability hint; cancels stale requests. Only values that
  // satisfy the server rules are checked, otherwise the endpoint answers 422.
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
      setUsernameStatus(result.available ? "available" : "taken");
      setUsernameSuggestion(result.available ? "" : (result.suggestion ?? ""));
    } catch {
      if (id === checkRef.current) setUsernameStatus("");
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reset the availability status when the username is cleared, before the debounced check
    if (!username) { setUsernameStatus(""); return; }
    const timer = setTimeout(() => checkUsername(username), 400);
    return () => clearTimeout(timer);
  }, [username, checkUsername]);

  function chooseUsername(name: string) {
    setUsername(name);
    setUsernameError("");
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setUsernameError("");
    setOrgError("");
    if (password !== password2) {
      setMismatch(true);
      password2Ref.current?.focus();
      return;
    }
    setSubmitting(true);
    let accessToken: string;
    try {
      const data = await apiFetch<{ access_token: string }>("/api/v1/auth/signup/complete", {
        method: "POST",
        body: JSON.stringify({
          token,
          username,
          password,
          first_name: firstName || undefined,
          last_name: lastName || undefined,
          org_name: orgName || undefined,
        }),
      });
      accessToken = data.access_token;
    } catch (err) {
      setSubmitting(false);
      const code = err instanceof ApiResponseError ? err.code : undefined;
      if (code === "invalid_signup_token" || code === "account_exists") {
        setBlocked(code === "account_exists" ? "account_exists" : "expired");
      } else if (code === "username_taken") {
        const suggestion = (err as ApiResponseError).detail as { suggestion?: string } | undefined;
        setUsernameStatus("taken");
        setUsernameSuggestion(suggestion?.suggestion ?? "");
        setUsernameError((err as Error).message);
        usernameRef.current?.focus();
      } else if (code === "org_name_taken") {
        setOrgError((err as Error).message);
        orgRef.current?.focus();
      } else {
        setError(err instanceof Error ? err.message : "Could not create your account.");
      }
      return;
    }
    // The account exists and the link is spent from here on, so a failed
    // /me load must not strand the person: the session cookie is already set.
    signInWithAccessToken(accessToken);
    try {
      await refreshMe();
    } catch {
      // AppShell restores the session from the refresh cookie.
    }
    router.push("/dashboard");
  }

  const usernameHintId = "signup-username-hint";
  const orgErrorId = "signup-org-error";

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />
      <div className="w-full max-w-sm">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">Finish Signing Up</h1>
          <p className="mt-1.5 text-sm text-text-muted">Choose how you&apos;ll sign in</p>
        </div>

        {blocked ? (
          <div ref={blockedRef} tabIndex={-1} className={errorCls} role="alert">
            {blocked === "account_exists" ? (
              <>
                <p>This email already has an account.</p>
                <p className="mt-2 text-xs">
                  <Link href="/login" className="text-accent hover:text-accent-hover">
                    Sign in instead
                  </Link>
                </p>
              </>
            ) : (
              <>
                <p>
                  {blocked === "expired"
                    ? "This link has expired or was already used."
                    : "Open the link from your email again."}
                </p>
                <p className="mt-2 text-xs">
                  <Link href="/register" className="text-accent hover:text-accent-hover">
                    Start signing up again
                  </Link>
                </p>
              </>
            )}
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-5">
            {error && <div className={errorCls} role="alert">{error}</div>}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="signup-firstname" className={label}>First Name</label>
                <input id="signup-firstname" type="text" maxLength={100} value={firstName} onChange={(e) => setFirstName(e.target.value)} className={input} autoComplete="given-name" />
              </div>
              <div>
                <label htmlFor="signup-lastname" className={label}>Last Name</label>
                <input id="signup-lastname" type="text" maxLength={100} value={lastName} onChange={(e) => setLastName(e.target.value)} className={input} autoComplete="family-name" />
              </div>
            </div>
            <div>
              <label htmlFor="signup-username" className={label}>Username</label>
              <input
                ref={usernameRef}
                id="signup-username"
                type="text"
                required
                minLength={USERNAME_MIN_LENGTH}
                maxLength={USERNAME_MAX_LENGTH}
                pattern={USERNAME_PATTERN}
                title={USERNAME_RULE_HINT}
                value={username}
                onChange={(e) => chooseUsername(e.target.value)}
                className={input}
                autoComplete="username"
                aria-invalid={usernameError ? true : undefined}
                aria-describedby={usernameHintId}
              />
              <div id={usernameHintId} aria-live="polite">
                {usernameError ? (
                  <p className="mt-1 text-xs text-danger">
                    {usernameError}
                    {usernameSuggestion && (
                      <> Try <button type="button" onClick={() => chooseUsername(usernameSuggestion)} className="text-accent underline">{usernameSuggestion}</button></>
                    )}
                  </p>
                ) : usernameStatus === "checking" ? (
                  <p className="mt-1 text-xs text-text-muted">Checking...</p>
                ) : usernameStatus === "available" ? (
                  <p className="mt-1 text-xs text-success">Available</p>
                ) : usernameStatus === "taken" ? (
                  <p className="mt-1 text-xs text-danger">
                    Taken{usernameSuggestion && (
                      <>, try <button type="button" onClick={() => chooseUsername(usernameSuggestion)} className="text-accent underline">{usernameSuggestion}</button></>
                    )}
                  </p>
                ) : (
                  <p className="mt-1 text-xs text-text-muted">{USERNAME_RULE_HINT}</p>
                )}
              </div>
            </div>
            <div>
              <label htmlFor="signup-org" className={label}>Organization <span className="normal-case tracking-normal">(optional)</span></label>
              <input
                ref={orgRef}
                id="signup-org"
                type="text"
                maxLength={200}
                value={orgName}
                onChange={(e) => { setOrgName(e.target.value); setOrgError(""); }}
                placeholder="My Household"
                className={input}
                aria-invalid={orgError ? true : undefined}
                aria-describedby={orgError ? orgErrorId : undefined}
              />
              {orgError && <p id={orgErrorId} className="mt-1 text-xs text-danger" role="alert">{orgError}</p>}
            </div>
            <div>
              <label htmlFor="signup-password" className={label}>Password</label>
              <PasswordInput id="signup-password" required minLength={8} maxLength={128} value={password} onChange={(e) => { setPassword(e.target.value); setMismatch(false); }} className={input} autoComplete="new-password" />
            </div>
            <div>
              <label htmlFor="signup-password2" className={label}>Confirm Password</label>
              <PasswordInput
                ref={password2Ref}
                id="signup-password2"
                required
                value={password2}
                onChange={(e) => { setPassword2(e.target.value); setMismatch(false); }}
                className={input}
                autoComplete="new-password"
                aria-invalid={mismatch ? true : undefined}
                aria-describedby={mismatch ? "signup-password2-error" : undefined}
              />
              {mismatch && <p id="signup-password2-error" className="mt-1 text-xs text-danger" role="alert">Passwords do not match</p>}
            </div>
            <button type="submit" disabled={submitting || usernameStatus === "taken"} className={`w-full ${btnPrimary}`}>
              {submitting ? "Creating account..." : "Create Account"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
