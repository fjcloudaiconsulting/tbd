"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import ThemeToggle from "@/components/ui/ThemeToggle";
import { apiFetch, extractErrorMessage } from "@/lib/api";
import { input, label, btnPrimary, error as errorCls, success } from "@/lib/styles";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      await apiFetch("/api/v1/auth/forgot-password", {
        method: "POST",
        body: JSON.stringify({ email }),
      });
      setSent(true);
    } catch (err) {
      setError(extractErrorMessage(err, "Something went wrong. Please try again."));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <ThemeToggle className="absolute right-6 top-6" />

      <div className="w-full max-w-sm">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">Reset Password</h1>
          <p className="mt-1.5 text-sm text-text-muted">
            Enter your email and we&apos;ll send you a reset link
          </p>
        </div>

        {sent ? (
          <div className="space-y-5">
            <div className={success}>
              If that email exists, a reset link has been sent.
            </div>
            <p className="text-center text-sm text-text-muted">
              <Link href="/login" className="text-accent hover:text-accent-hover">
                Back to login
              </Link>
            </p>
          </div>
        ) : (
          <>
            <form onSubmit={handleSubmit} className="space-y-5">
              {error && <div className={errorCls}>{error}</div>}
              <div>
                <label htmlFor="forgot-email" className={label}>Email</label>
                <input
                  id="forgot-email"
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  className={input}
                  autoComplete="email"
                  placeholder="you@example.com"
                />
              </div>
              <button type="submit" disabled={submitting} className={`w-full ${btnPrimary}`}>
                {submitting ? "Sending..." : "Send Reset Link"}
              </button>
            </form>
            <p className="mt-6 text-center text-sm text-text-muted">
              <Link href="/login" className="text-accent hover:text-accent-hover">
                Back to login
              </Link>
            </p>
          </>
        )}
      </div>
    </div>
  );
}
