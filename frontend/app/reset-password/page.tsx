"use client";

import { FormEvent, Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { input, label, btnPrimary, error as errorCls, success } from "@/lib/styles";

function ResetPasswordForm() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token");

  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  if (!token) {
    return (
      <div className="space-y-5">
        <div className={errorCls}>Invalid reset link.</div>
        <p className="text-center text-sm text-text-muted">
          <Link href="/forgot-password" className="text-accent hover:text-accent-hover">
            Request a new link
          </Link>
        </p>
      </div>
    );
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");

    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    if (password !== password2) {
      setError("Passwords do not match.");
      return;
    }

    setSubmitting(true);
    try {
      await apiFetch("/api/v1/auth/reset-password", {
        method: "POST",
        body: JSON.stringify({ token, new_password: password }),
      });
      setDone(true);
    } catch (err) {
      setError(extractErrorMessage(err, "Reset failed. The link may have expired."));
    } finally {
      setSubmitting(false);
    }
  }

  if (done) {
    return (
      <div className="space-y-5">
        <div className={success}>Password reset successfully!</div>
        <p className="text-center text-sm text-text-muted">
          <Link href="/login" className="text-accent hover:text-accent-hover">
            Sign in with your new password
          </Link>
        </p>
      </div>
    );
  }

  return (
    <>
      <form onSubmit={handleSubmit} className="space-y-5">
        {error && <div className={errorCls}>{error}</div>}
        <div>
          <label htmlFor="reset-password" className={label}>New Password</label>
          <input
            id="reset-password"
            type="password"
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={input}
            autoComplete="new-password"
          />
        </div>
        <div>
          <label htmlFor="reset-password2" className={label}>Confirm Password</label>
          <input
            id="reset-password2"
            type="password"
            required
            value={password2}
            onChange={(e) => setPassword2(e.target.value)}
            className={input}
            autoComplete="new-password"
          />
        </div>
        <button type="submit" disabled={submitting} className={`w-full ${btnPrimary}`}>
          {submitting ? "Resetting..." : "Reset Password"}
        </button>
      </form>
      <p className="mt-6 text-center text-sm text-text-muted">
        <Link href="/login" className="text-accent hover:text-accent-hover">
          Back to login
        </Link>
      </p>
    </>
  );
}

export default function ResetPasswordPage() {
  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />

      <div className="w-full max-w-sm">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">New Password</h1>
          <p className="mt-1.5 text-sm text-text-muted">Choose a new password for your account</p>
        </div>
        <Suspense fallback={<p className="text-center text-sm text-text-muted">Loading...</p>}>
          <ResetPasswordForm />
        </Suspense>
      </div>
    </div>
  );
}
