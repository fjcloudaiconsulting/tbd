"use client";

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import ThemeToggle from "@/components/ui/ThemeToggle";
import PasswordInput from "@/components/ui/PasswordInput";
import { ApiResponseError, apiFetch, decodeJwtPayload } from "@/lib/api";
import { useAuth } from "@/components/auth/AuthProvider";
import { useOneTimeQueryParam } from "@/lib/hooks/use-one-time-query-param";
import { btnPrimary, error as errorCls, input, label, success } from "@/lib/styles";

const CONTACT = "hello@thebetterdecision.com";

function VerifyEmailHandler() {
  const token = useOneTimeQueryParam("token");
  const { user } = useAuth();

  const [status, setStatus] = useState<"loading" | "password" | "success" | "error">("loading");
  // TBD-657. Verifying an unverified account needs its password; the backend
  // asks for it with `password_required`. Display-only decode of the token.
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [fieldError, setFieldError] = useState("");
  const claims = useMemo(() => (token ? decodeJwtPayload(token) : null), [token]);
  const tokenEmail = typeof claims?.email === "string" ? claims.email : "";
  const adminInitiated = claims?.admin_initiated === true;
  // TBD-361. A PROMOTION (a confirmed email change) invalidates every
  // session, so the cached `user` here is stale and "Go to dashboard" would
  // walk straight into a 401. A first-time verification changes no identity
  // and leaves the session alone. The backend distinguishes them.
  const [emailChanged, setEmailChanged] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");
  const calledRef = useRef(false);

  function submit(pw?: string) {
    return apiFetch("/api/v1/auth/verify-email", {
      method: "POST",
      body: JSON.stringify(pw === undefined ? { token } : { token, password: pw }),
    }).then((res) => {
      setEmailChanged(Boolean((res as { email_changed?: boolean })?.email_changed));
      setStatus("success");
    });
  }

  useEffect(() => {
    if (!token || calledRef.current) return;
    calledRef.current = true;

    submit()
      .catch((err) => {
        if (err instanceof ApiResponseError && err.code === "password_required") {
          setStatus("password");
          return;
        }
        setStatus("error");
        setErrorMsg(err instanceof Error ? err.message : "Verification failed");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setFieldError("");
    try {
      await submit(password);
    } catch (err) {
      const e2 = err instanceof ApiResponseError ? err : null;
      if (e2?.code === "password_incorrect") {
        setFieldError("That password is incorrect.");
      } else if (e2?.status === 429) {
        setFieldError("Too many attempts. Please wait a minute and try again.");
      } else if (e2 && (e2.status === 409 || e2.status === 400)) {
        setStatus("error");
        setErrorMsg(e2.message);
      } else {
        setFieldError("Could not verify right now. Please try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  if (!token) {
    return (
      <div className="space-y-5">
        <div className={errorCls}>Invalid verification link.</div>
        <p className="text-center text-sm text-text-muted">
          <Link href="/login" className="text-accent hover:text-accent-hover">
            Go to login
          </Link>
        </p>
      </div>
    );
  }

  if (status === "loading") {
    return <p className="text-center text-sm text-text-muted">Verifying your email...</p>;
  }

  if (status === "password") {
    return (
      <form onSubmit={handleSubmit} className="space-y-5">
        <p className="text-center text-sm text-text-muted">
          Enter your password to confirm this email address.
        </p>
        {fieldError && (
          <div className={errorCls} role="alert" id="verify-pw-error">
            {fieldError}
          </div>
        )}
        <input
          type="text"
          readOnly
          className="sr-only"
          tabIndex={-1}
          name="username"
          autoComplete="username"
          value={tokenEmail}
          aria-hidden="true"
        />
        <div>
          <label htmlFor="verify-pw" className={label}>Password</label>
          <PasswordInput
            id="verify-pw"
            required
            autoFocus
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={input}
            autoComplete="current-password"
            aria-invalid={fieldError ? true : undefined}
            aria-describedby={fieldError ? "verify-pw-error" : undefined}
          />
        </div>
        <button type="submit" disabled={submitting} className={`w-full ${btnPrimary}`}>
          {submitting ? "Verifying..." : "Verify"}
        </button>
        <p className="text-center text-xs text-text-muted">
          Didn&apos;t sign up? You can close this page.
        </p>
        <p className="text-center text-xs text-text-muted">
          {adminInitiated ? (
            <>
              Forgot your password? Contact us at{" "}
              <a href={`mailto:${CONTACT}`} className="text-accent hover:text-accent-hover">
                {CONTACT}
              </a>
              .
            </>
          ) : (
            <Link href="/forgot-password" className="text-accent hover:text-accent-hover">
              Forgot password?
            </Link>
          )}
        </p>
      </form>
    );
  }

  if (status === "error") {
    return (
      <div className="space-y-5">
        <div className={errorCls}>
          {errorMsg || "Invalid or expired verification link."}
        </div>
        <p className="text-center text-sm text-text-muted">
          <Link href="/login" className="text-accent hover:text-accent-hover">
            Go to login
          </Link>
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className={success}>
        {emailChanged ? "Email address confirmed!" : "Email verified!"}
      </div>
      {emailChanged && (
        <p className="text-center text-sm text-text-muted">
          This address is now your sign-in email. For security, confirming a
          new address signs you out everywhere, so please sign in again.
        </p>
      )}
      <p className="text-center text-sm text-text-muted">
        <Link
          href={!emailChanged && user ? "/dashboard" : "/login"}
          className="text-accent hover:text-accent-hover"
        >
          {!emailChanged && user ? "Go to dashboard" : "Sign in"}
        </Link>
      </p>
    </div>
  );
}

export default function VerifyEmailPage() {
  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />

      <div className="w-full max-w-sm">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">Email Verification</h1>
        </div>
        <Suspense fallback={<p className="text-center text-sm text-text-muted">Loading...</p>}>
          <VerifyEmailHandler />
        </Suspense>
      </div>
    </div>
  );
}
