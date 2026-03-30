"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/auth/AuthProvider";
import { useTheme } from "@/components/ThemeProvider";

export default function SetupPage() {
  const { needsSetup, loading, register, login } = useAuth();
  const { theme, toggle } = useTheme();
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [orgName, setOrgName] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!loading && !needsSetup) router.replace("/");
  }, [loading, needsSetup, router]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (password !== password2) {
      setError("Passwords do not match");
      return;
    }
    setSubmitting(true);
    try {
      await register(username, email, password, orgName || undefined);
      await login(username, password);
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Setup failed");
    } finally {
      setSubmitting(false);
    }
  }

  if (loading || !needsSetup) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-accent" />
      </div>
    );
  }

  const inputClass =
    "w-full rounded-lg border border-border bg-surface-raised px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:border-accent focus:outline-none";

  return (
    <div className="relative flex min-h-screen items-center justify-center px-4">
      <button
        onClick={toggle}
        className="absolute right-6 top-6 rounded-md p-2 text-text-muted hover:bg-surface hover:text-text-secondary"
      >
        {theme === "light" ? (
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M21.752 15.002A9.72 9.72 0 0 1 18 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 0 0 3 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 0 0 9.002-5.998Z" />
          </svg>
        ) : (
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v2.25m6.364.386-1.591 1.591M21 12h-2.25m-.386 6.364-1.591-1.591M12 18.75V21m-4.773-4.227-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0Z" />
          </svg>
        )}
      </button>

      <div className="w-full max-w-md">
        <div className="mb-10 text-center">
          <h1 className="font-display text-3xl font-semibold text-text-primary">Welcome to PFV2</h1>
          <p className="mt-2 text-sm text-text-secondary">
            Create your administrator account to get started.
          </p>
        </div>

        <div className="rounded-lg border border-border bg-surface p-6">
          <form onSubmit={handleSubmit} className="space-y-5">
            {error && (
              <div className="rounded-lg bg-danger-dim px-4 py-3 text-sm text-danger">{error}</div>
            )}
            <div>
              <label htmlFor="username" className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">Username</label>
              <input id="username" type="text" required value={username} onChange={(e) => setUsername(e.target.value)} className={inputClass} autoComplete="username" autoFocus />
            </div>
            <div>
              <label htmlFor="email" className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">Email</label>
              <input id="email" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={inputClass} autoComplete="email" />
            </div>
            <div>
              <label htmlFor="orgName" className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">Organization Name</label>
              <input id="orgName" type="text" value={orgName} onChange={(e) => setOrgName(e.target.value)} placeholder="My Household" className={inputClass} />
              <p className="mt-1.5 text-xs text-text-muted">Optional. Defaults to your username.</p>
            </div>
            <div>
              <label htmlFor="password" className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">Password</label>
              <input id="password" type="password" required value={password} onChange={(e) => setPassword(e.target.value)} className={inputClass} autoComplete="new-password" />
            </div>
            <div>
              <label htmlFor="password2" className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.08em] text-text-muted">Confirm Password</label>
              <input id="password2" type="password" required value={password2} onChange={(e) => setPassword2(e.target.value)} className={inputClass} autoComplete="new-password" />
            </div>
            <button type="submit" disabled={submitting} className="w-full rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-accent-text hover:bg-accent-hover disabled:opacity-50">
              {submitting ? "Setting up..." : "Create Admin Account"}
            </button>
          </form>
        </div>
        <p className="mt-4 text-center text-xs text-text-muted">
          This account will have full administrator privileges.
        </p>
      </div>
    </div>
  );
}
